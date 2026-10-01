"""Regression coverage for captured ES framing, state and Home Assistant APIs."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.sonyavr import _update_max_volume, async_setup_entry
from custom_components.sonyavr.cis import FrameDecoder, command
from custom_components.sonyavr.es_receiver import SonyESReceiver, is_str_da5800es
from custom_components.sonyavr.media_player import SonyAVRDevice
from custom_components.sonyavr.remote import SonyAVRDevice as Remote
from custom_components.sonyavr.sensor import SonyAVRDevice as SonyAVRVolume
from custom_components.sonyavr.sonyavr import SonyAVR

from .helpers import receiver, reply, sources_page


@pytest.mark.parametrize(
    "hex_frame",
    [
        "0207a882002e2e210151",
        "0206a8920001d800e7",
        "0206a8920001d88067",
        "0204ab822700a8",
        "0204ab920001be",
        "fd",
        "fe",
        "0202a80e",
        "0202ab0f",
    ],
)
def test_captured_frames_at_every_split(hex_frame):
    frame = bytes.fromhex(hex_frame)
    for split in range(1, len(frame)):
        decoder = FrameDecoder()
        assert decoder.feed(frame[:split]) == []
        assert decoder.feed(frame[split:]) == [frame]
    assert FrameDecoder().feed(frame) == [frame]


def test_decoder_coalescing_corrupt_checksum_and_garbage():
    frame = reply(0xA8, 0x92, 0, 1, 0xD8, 0x80)
    corrupt = frame[:-1] + b"\x00"
    decoder = FrameDecoder()
    assert decoder.feed(b"\x99" + corrupt + b"\xfd\xfe\x02\x02\xa8\x0e" + frame) == [
        b"\xfd",
        b"\xfe",
        b"\x02\x02\xa8\x0e",
        frame,
    ]


def test_main_zone_bitmask_and_volume_do_not_implicitly_unmute():
    avr = receiver()
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x1D, 0x10, 0x33, 7))
    assert avr.source == "PC"
    assert avr.mute is True
    assert avr.state_service.power is True
    avr._handle_frame(bytes.fromhex("0206a8920001d88067"))
    assert avr.volume == -39.5
    assert avr.mute is True
    avr._handle_frame(reply(0xA8, 0x82, 1, 0x2E, 0, 0, 0))
    avr._handle_frame(reply(0xA8, 0x92, 1, 1, 0, 0))
    assert avr.state_service.power is True
    assert avr.volume == -39.5


def test_input_names_duplicate_labels_and_end_page():
    avr = receiver()
    avr._handle_frame(
        sources_page(entries=((0x10, "VIDEO1", "Streamer"), (0x1D, "PC", "Streamer")))
    )
    assert avr.sources == ("Streamer (0x10)", "Streamer (0x1D)")
    avr._handle_frame(sources_page(offset=255, entries=()))
    assert len(avr.sources) == 2


def test_input_record_starts_at_page_offset_byte():
    # Real ES layout: zone, then index/audio/video/icon/name/custom/flags.
    first_record = bytes.fromhex("001b1b014244202020202020424420202020202010")
    data = b"\x00" + first_record + (b"\xff" + bytes(20)) * 9 + bytes(2)
    avr = receiver()
    avr._handle_frame(reply(0xA8, 0x8B, *data))
    assert avr.sources == ("BD",)
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x1B, 0x1B, 0x21, 1))
    assert avr.source == "BD"


def test_unknown_and_off_entities_clear_stale_values():
    avr = receiver()
    entity = SonyAVRDevice(avr, avr._config_entry)
    assert entity.available is False
    assert entity.state is None
    assert entity.volume_level is None
    assert entity.extra_state_attributes["sound_optimizer"] is None
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x10, 0x10, 0x21, 1))
    avr._handle_frame(reply(0xA8, 0x92, 0, 1, 0xD8, 0x80))
    avr._handle_frame(reply(0xAB, 0x82, 0x27, 0))
    avr._handle_frame(reply(0xAB, 0x92, 0, 1))
    assert entity.state == MediaPlayerState.ON
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x10, 0x10, 0x20, 1))
    assert entity.state == MediaPlayerState.OFF
    assert entity.volume_level is None
    assert entity.sound_mode is None
    avr._connection_changed(False)
    assert entity.state is None
    assert avr.source is None
    assert Remote(avr, None).available is False
    assert SonyAVRVolume(avr, None).available is False


@pytest.mark.parametrize(
    "volume,encoded",
    [
        (-90, "a600"),
        (-39.5, "d880"),
        (0, "0000"),
        (0.5, "0080"),
        (20, "1400"),
        (-100, "a600"),
        (30, "1400"),
    ],
)
async def test_absolute_volume_encoding_and_clamping(volume, encoded):
    avr = receiver()
    send = avr.command_service.async_send_command = AsyncMock()
    await avr.async_volume_set(volume)
    send.assert_awaited_once_with(command(0xA0, 0x52, 0, 1, *bytes.fromhex(encoded)))


@pytest.mark.parametrize("volume", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_volume_rejected(volume):
    avr = receiver()
    avr.client.send = AsyncMock()
    with pytest.raises(ValueError):
        await avr.async_volume_set(volume)
    avr.client.send.assert_not_awaited()


async def test_service_volume_uses_half_db_and_respects_configured_zero_limit():
    avr = receiver()
    _update_max_volume(avr, 0)
    assert avr.volume_max == 0
    send = avr.command_service.async_send_command = AsyncMock()
    avr.state_service.volume = -39.5
    await avr.async_send_command("Volume Up", None)
    send.assert_awaited_with(command(0xA0, 0x52, 0, 1, 0xD9, 0))
    avr.state_service.volume = 0
    send.reset_mock()
    await avr.async_volume_up()
    send.assert_not_awaited()
    await avr.async_volume_down()
    send.assert_awaited_with(command(0xA0, 0x52, 0, 1, 0xFF, 0x80))
    _update_max_volume(avr, -100)
    assert avr.volume_range == 90


async def test_named_source_sound_optimizer_and_hdmi_services():
    avr = receiver()
    avr._handle_frame(sources_page())
    send = avr.command_service.async_send_command = AsyncMock()
    await avr.async_set_source("Streamer")
    send.assert_awaited_with(command(0xA0, 0x42, 0, 0x10))
    await avr.async_send_command("Set Sound Optimizer", "Low")
    send.assert_awaited_with(command(0xA3, 0x52, 2))
    await avr.async_send_command("Set HDMI Output", "A+B")
    send.assert_awaited_with(command(0xA0, 0x45, 2))
    assert avr.last_requested_hdmi_output == "A+B"
    assert avr.state_service.sound_optimizer is None  # Requires receiver confirmation.
    avr._connection_changed(False)
    assert avr.last_requested_hdmi_output is None


@pytest.mark.parametrize(
    "model,expected",
    [
        ("STR-DA5800ES", True),
        ("STR_DA5800ES", True),
        ("str da5800es", True),
        ("STR-DN1040", False),
    ],
)
async def test_model_factory_preserves_legacy_path(model, expected):
    assert is_str_da5800es(model) is expected
    hass = SimpleNamespace(
        data={}, config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock())
    )
    entry = SimpleNamespace(
        entry_id="test",
        data={"host": "127.0.0.1", "name": "AVR", "model": model},
        options={},
        add_update_listener=lambda cb: lambda: None,
    )
    assert await async_setup_entry(hass, entry)
    assert type(hass.data["sonyavr"]["test"]["sonyavr"]) is (
        SonyESReceiver if expected else SonyAVR
    )


async def test_es_startup_never_power_cycles_or_mutates_settings():
    avr = receiver()
    avr.run_notifier = AsyncMock()
    avr.command_service.async_connect = AsyncMock()
    avr.run_ping_watcher = AsyncMock()
    avr.async_get_power_state = AsyncMock()
    avr.async_update_status = AsyncMock()
    avr.async_turn_on = AsyncMock()
    entity = SonyAVRDevice(avr, avr._config_entry)
    entity.hass = SimpleNamespace(
        async_create_background_task=lambda coro, name: asyncio.create_task(coro)
    )
    await entity._async_startup(None)
    await entity._notifier_task
    avr.run_notifier.assert_awaited_once()
    for call in (
        avr.command_service.async_connect,
        avr.run_ping_watcher,
        avr.async_get_power_state,
        avr.async_update_status,
        avr.async_turn_on,
    ):
        call.assert_not_awaited()


async def test_ha_2026_state_serialization_and_unload(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    avr = receiver()
    media = SonyAVRDevice(avr, avr._config_entry)
    component = EntityComponent(logging.getLogger(__name__), "media_player", hass)
    # This standalone entity platform has no configuration entry/device registry link.
    with patch.object(SonyAVRDevice, "device_info", property(lambda self: None)):
        await component.async_add_entities([media])
    assert hass.states.get(media.entity_id).state == "unavailable"
    avr._connected = True
    avr.client.writer = SimpleNamespace(is_closing=lambda: False)
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x10, 0x10, 0x23, 1))
    avr._handle_frame(reply(0xA8, 0x92, 0, 1, 0xD8, 0x80))
    media.async_write_ha_state()
    state = hass.states.get(media.entity_id)
    assert state.state == "on"
    assert state.attributes["volume"] == -39.5
    assert state.attributes["is_volume_muted"] is True
    assert 0 <= state.attributes["volume_level"] <= 1
    avr._handle_frame(reply(0xA8, 0x82, 0, 0x10, 0x10, 0x20, 1))
    media.async_write_ha_state()
    assert hass.states.get(media.entity_id).state == "off"
    avr._connection_changed(False)
    media.async_write_ha_state()
    assert hass.states.get(media.entity_id).state == "unavailable"
    avr.client.writer = None
    avr.stop_notifier = AsyncMock()
    avr.command_service.async_disconnect = AsyncMock()
    await media.async_remove()
    avr.stop_notifier.assert_awaited_once()
    await hass.async_stop()
