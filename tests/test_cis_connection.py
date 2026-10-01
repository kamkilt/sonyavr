"""Exercise reconnection, push events and transaction failures over real TCP."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from custom_components.sonyavr.cis import CommandRejected

from .helpers import FakeAmp, receiver, reply, until


@asynccontextmanager
async def running(amp):
    await amp.start()
    avr = receiver(amp.port)
    avr.client.timeout = 0.15
    avr.client.retry_interval = 0.02
    avr.client.retry_max = 0.05
    avr.client.refresh_interval = 0.08
    task = asyncio.create_task(avr.run_notifier())
    try:
        await until(
            lambda: (
                avr.available and avr.volume is not None and "Streamer" in avr.sources
            )
        )
        yield avr
    finally:
        await avr.stop_notifier()
        await asyncio.wait_for(task, 1)
        await amp.close()


async def test_startup_fragmented_replies_and_unsolicited_push():
    amp = FakeAmp()
    amp.fragment = True
    async with running(amp) as avr:
        assert avr.volume == -39.5
        assert avr.mute is True
        assert avr.mode == "Multi Stereo"
        assert avr.state_service.sound_optimizer == "normal"
        assert all(packet[3] >= 0x80 and packet[-1] == 0 for packet in amp.commands)
        for writer in amp.writers:
            writer.write(reply(0xA8, 0x92, 0, 1, 0xD8, 0))
            await writer.drain()
        await until(lambda: avr.volume == -40)
        assert avr.mute is True


async def test_reconnect_clears_stale_state_and_reads_changed_snapshot():
    amp = FakeAmp()
    observations = []
    async with running(amp) as avr:
        avr.set_update_cb(
            lambda: observations.append((avr.available, avr.volume, avr.source))
        )
        connections = amp.connections
        amp.source = 0x1D
        amp.volume = -50
        amp.mode = 0x21
        amp.optimizer = 2
        amp.flags = 0x21
        await amp.disconnect()
        await until(
            lambda: (
                amp.connections > connections
                and avr.volume == -50
                and avr.source == "PC"
            )
        )
        await until(lambda: sum(packet[3] == 0x8B for packet in amp.commands) >= 2)
        assert (False, None, None) in observations
        assert avr.available
        assert avr.mute is False
        assert avr.state_service.sound_optimizer == "low"
        assert all(packet[3] >= 0x80 for packet in amp.commands)
        assert sum(packet[3] == 0x8B for packet in amp.commands) >= 2


async def test_unreachable_at_start_then_receiver_becomes_available(caplog):
    amp = FakeAmp()
    await amp.start()
    port = amp.port
    await amp.close()
    avr = receiver(port)
    avr.client.timeout = 0.1
    avr.client.retry_interval = 0.02
    avr.client.retry_max = 0.04
    task = asyncio.create_task(avr.run_notifier())
    try:
        await asyncio.sleep(0.07)
        assert not avr.available
        assert avr.volume is None
        assert not task.done()
        await amp.start(port)
        await until(lambda: avr.available and avr.volume == -39.5)
        assert not [record for record in caplog.records if record.levelno >= 30]
    finally:
        await avr.stop_notifier()
        await asyncio.wait_for(task, 1)
        await amp.close()


async def test_offline_command_fails_without_optimistic_state():
    avr = receiver()
    with pytest.raises(ConnectionError):
        await avr.async_turn_on()
    assert avr.state_service.power is None
    assert not avr.available


async def test_standby_reads_only_status_and_wakes_without_power_cycle():
    amp = FakeAmp()
    amp.flags = 0x20
    await amp.start()
    avr = receiver(amp.port)
    avr.client.timeout = 0.1
    avr.client.refresh_interval = 0.03
    task = asyncio.create_task(avr.run_notifier())
    try:
        await until(lambda: avr.available)
        assert avr.state_service.power is False
        assert avr.volume is None
        assert all(packet[3] == 0x82 and packet[2] == 0xA0 for packet in amp.commands)
        amp.flags = 0x21
        await until(lambda: avr.volume is not None and "Streamer" in avr.sources)
        assert all(packet[3] >= 0x80 for packet in amp.commands)
    finally:
        await avr.stop_notifier()
        await asyncio.wait_for(task, 1)
        await amp.close()


async def test_acknowledged_power_off_can_drop_ethernet():
    amp = FakeAmp()
    amp.close_after_off = True
    async with running(amp) as avr:
        await avr.async_turn_off()
        await until(lambda: not avr.available)
        assert avr.volume is None


async def test_rejected_command_does_not_poison_following_transaction():
    amp = FakeAmp()
    async with running(amp) as avr:
        amp.rejected.add((0xA0, 0x53))
        with pytest.raises(CommandRejected):
            await avr.async_mute_off()
        assert avr.client.connected
        amp.rejected.clear()
        await avr.async_mute_off()
        assert avr.mute is False


async def test_optional_state_not_executable_during_boot():
    amp = FakeAmp()
    amp.rejected.update({(0xA3, 0x82), (0xA3, 0x92)})
    async with running(amp) as avr:
        assert avr.available
        assert avr.mode is None
        amp.rejected.clear()
        await until(
            lambda: (
                avr.mode is not None and avr.state_service.sound_optimizer == "normal"
            )
        )


async def test_timeout_discards_session_and_recovers():
    amp = FakeAmp()
    async with running(amp) as avr:
        initial = amp.connections
        amp.silent.add((0xA0, 0x53))
        with pytest.raises(TimeoutError):
            await avr.async_mute_off()
        assert not avr.client.connected
        amp.silent.clear()
        await until(
            lambda: (
                amp.connections > initial and avr.available and avr.volume is not None
            )
        )
        await avr.async_mute_off()
        assert avr.mute is False


async def test_cancelling_command_discards_pending_ack():
    amp = FakeAmp()
    async with running(amp) as avr:
        amp.silent.add((0xA0, 0x53))
        task = asyncio.create_task(avr.async_mute_off())
        await until(lambda: any(packet[2:4] == b"\xa0\x53" for packet in amp.commands))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not avr.client.connected


async def test_eof_fails_pending_request_and_reconnects():
    amp = FakeAmp()
    async with running(amp) as avr:
        amp.silent.add((0xA0, 0x53))
        task = asyncio.create_task(avr.async_mute_off())
        await until(lambda: any(packet[2:4] == b"\xa0\x53" for packet in amp.commands))
        await amp.disconnect()
        with pytest.raises(ConnectionError):
            await task
        amp.silent.clear()
        await until(lambda: avr.available and avr.volume is not None)


async def test_notifications_and_ack_before_control_refresh():
    amp = FakeAmp()
    async with running(amp) as avr:
        await avr.async_set_source("PC")
        assert avr.source == "PC"
        await avr.async_volume_set(-25.5)
        assert avr.volume == -25.5
        assert avr.mute is True
        await avr.async_send_command("Set Sound Optimizer", "Off")
        assert avr.state_service.sound_optimizer == "off"


async def test_raw_frame_validation_and_checksum_normalization():
    amp = FakeAmp()
    async with running(amp) as avr:
        for packet in (b"", b"\x02\x06\xa0\x52", b"\x02\x03\xa0\x53\x00"):
            with pytest.raises(ValueError):
                await avr.client.send(packet)
        await avr.client.send(bytes.fromhex("0204a053000009"))
        assert bytes.fromhex("0204a053000000") in amp.commands
