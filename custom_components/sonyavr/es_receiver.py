"""Passive state initialization and reconnection for the STR-DA5800ES."""

from __future__ import annotations

import math

from .cis import CISConnection, command
from .sonyavr import (
    CMD_SOUND_FIELD_MAP,
    CMD_SOURCE_MAP,
    SOUND_FIELD_MENU_MAP,
    SOURCE_MENU_MAP,
    CommandService,
    SonyAVR,
)


def is_str_da5800es(model: str) -> bool:
    """Accept the model spellings already documented by this integration."""
    return "".join(c for c in model.upper() if c.isalnum()) == "STRDA5800ES"


class ESCommandService(CommandService):
    """Keep the existing service interface while confirming state through reads."""

    def __init__(self, receiver):
        super().__init__(receiver.device_service, receiver.state_service, receiver.port)
        self.receiver = receiver
        self.initialized = True
        self.scroll_step_volume = 0.5

    async def async_connect(self):
        """The notifier owns the single connection and its reconnect loop."""

    async def async_disconnect(self):
        await self.receiver.client.stop()

    async def async_send_command(self, packet):
        await self.receiver.client.send(bytes(packet))
        if packet[3] < 0x80:
            try:
                await self.receiver.client.refresh()
            except (OSError, TimeoutError):
                if packet[2:6] != bytes((0xA0, 0x60, 0, 0)):
                    raise
                # ACK confirmed power-off; Network Standby may then remove Ethernet.

    async def async_set_volume(self, volume):
        volume = float(volume)
        if not math.isfinite(volume):
            raise ValueError("Volume must be finite")
        volume = max(
            self.state_service.volume_min, min(volume, self.state_service.volume_max)
        )
        fixed = round(volume * 2) * 128
        await self.async_send_command(
            command(0xA0, 0x52, 0, 1, (fixed >> 8) & 255, fixed & 255)
        )

    async def async_volume_up(self):
        if self.state_service.volume is None:
            await self.receiver.client.refresh()
        if (
            self.state_service.volume is not None
            and self.state_service.volume < self.state_service.volume_max
        ):
            await self.async_set_volume(self.state_service.volume + 0.5)

    async def async_volume_down(self):
        if self.state_service.volume is None:
            await self.receiver.client.refresh()
        if self.state_service.volume is not None:
            await self.async_set_volume(self.state_service.volume - 0.5)

    async def async_mute(self):
        await self.async_send_command(command(0xA0, 0x53, 0, 1))

    async def async_unmute(self):
        await self.async_send_command(command(0xA0, 0x53, 0, 0))

    async def async_select_source(self, source):
        await self.async_send_command(command(0xA0, 0x42, 0, source))

    async def async_select_sound_field(self, mode):
        await self.async_send_command(command(0xA3, 0x42, mode))

    async def _async_move_source(self, step):
        codes = list(self.receiver._sources)
        current = self.state_service.source
        index = codes.index(current) if current in codes else 0
        await self.async_select_source(codes[(index + step) % len(codes)])

    async def async_source_up(self):
        await self._async_move_source(1)

    async def async_source_down(self):
        await self._async_move_source(-1)


class SonyESReceiver(SonyAVR):
    """Model-specific adapter, preserving the public interface of SonyAVR."""

    passive_initialization = True

    def __init__(self, hass, config_entry, ip, name, model, port=33335):
        super().__init__(hass, config_entry, ip, name, model, port)
        self._connected = False
        state = self.state_service
        state.volume_model = 1
        # Include the -90 dB commands used by STR-DA5800ES installations.
        state.volume_min = -90.0
        state.volume_max = 20.0
        state.volume_range = state.volume_max - state.volume_min
        state.volume = None
        self._sources = {
            CMD_SOURCE_MAP[key][5]: value for key, value in SOURCE_MENU_MAP.items()
        }
        self._sources.update({0x19: "DVD", 0x1B: "Blu-ray", 0x1D: "PC"})
        self._sources.update({0x20 + i: f"HDMI {i}" for i in range(1, 7)})
        self._modes = {
            CMD_SOUND_FIELD_MAP[key][4]: value
            for key, value in SOUND_FIELD_MENU_MAP.items()
        }
        self._modes.update(
            {
                0x1E: "Concertgebouw",
                0x1F: "Musikverein Vienna",
                0x38: "Berlin Philharmonic Hall",
                0x2B: "PL-IIx Movie",
                0x2C: "PL-IIx Music",
            }
        )
        self.last_requested_hdmi_output = None
        self.client = CISConnection(
            ip, port, self._handle_frame, self._connection_changed
        )
        self.command_service = ESCommandService(self)

    @property
    def available(self):
        return (
            self._connected
            and self.client.connected
            and self.state_service.power is not None
        )

    @property
    def volume_max(self):
        return self.state_service.volume_max

    @volume_max.setter
    def volume_max(self, value):
        value = float(value)
        if not math.isfinite(value) or not self.volume_min < value <= 20:
            raise ValueError("Maximum volume must be above -90 and at most +20 dB")
        self.state_service.volume_max = value
        self.state_service.volume_range = value - self.volume_min

    def _notify(self):
        for cb in (self._update_cb, self._remote_update_cb, self._sensor_update_cb):
            if cb is not None:
                cb()

    def _connection_changed(self, connected):
        changed = connected != self._connected
        self._connected = connected
        if not connected:
            state = self.state_service
            for field in (
                "power",
                "muted",
                "source",
                "volume",
                "sound_field",
                "sound_optimizer",
            ):
                setattr(state, field, None)
            self.last_requested_hdmi_output = None
        if changed:
            self._notify()

    def _handle_frame(self, frame):
        state = self.state_service
        if len(frame) == 10 and frame[2:5] == b"\xa8\x82\x00":
            state.source = frame[5]
            self._sources.setdefault(frame[5], f"Input 0x{frame[5]:02X}")
            state.power = bool(frame[7] & 1)
            state.muted = bool(frame[7] & 2)
            if not state.power:
                state.volume = state.sound_field = state.sound_optimizer = None
        elif len(frame) == 9 and frame[2:6] == b"\xa8\x92\x00\x01":
            state.volume = int.from_bytes(frame[6:8], "big", signed=True) / 256
        elif len(frame) == 7 and frame[2:4] == b"\xab\x82":
            state.sound_field = frame[4]
        elif len(frame) == 7 and frame[2:5] == b"\xab\x92\x00":
            state.sound_optimizer = {0: "off", 1: "normal", 2: "low"}.get(frame[5])
        elif len(frame) == 218 and frame[2:5] == b"\xa8\x8b\x00":
            if frame[5] == 0xFF:
                return
            if frame[5] == 0:
                self._sources.clear()
            for offset in range(5, 215, 21):
                item = frame[offset : offset + 21]
                if item[0] == 0xFF:
                    break
                default = item[4:12].decode("ascii", "replace").strip(" \x00")
                custom = item[12:20].decode("ascii", "replace").strip(" \x00")
                self._sources[item[1]] = custom or default or f"Input 0x{item[1]:02X}"
        else:
            return
        self._notify()

    def _source_labels(self):
        """Keep repeated factory names individually selectable."""
        labels = list(self._sources.values())
        return {
            code: name if labels.count(name) == 1 else f"{name} (0x{code:02X})"
            for code, name in self._sources.items()
        }

    @property
    def sources(self):
        return tuple(self._source_labels().values())

    @property
    def source(self):
        return self._source_labels().get(self.state_service.source)

    @property
    def modes(self):
        return tuple(self._modes.values())

    @property
    def mode(self):
        return self._modes.get(self.state_service.sound_field)

    async def async_set_source(self, source):
        for code, label in self._source_labels().items():
            if label == source:
                await self.command_service.async_select_source(code)
                return
        raise ValueError(f"Unknown source: {source}")

    async def async_set_mode(self, mode):
        for code, label in self._modes.items():
            if label == mode:
                await self.command_service.async_select_sound_field(code)
                return
        raise ValueError(f"Unknown sound mode: {mode}")

    async def async_update_status(self):
        await self.client.refresh()

    async def async_poll_state(self):
        await self.client.refresh()

    async def async_get_power_state(self):
        await self.client.status()
        return self.state_service.power

    async def run_notifier(self):
        await self.client.run()

    async def stop_notifier(self):
        await self.client.stop()

    async def run_ping_watcher(self):
        """Protocol reads detect loss; ICMP reachability is not a power state."""

    async def stop_ping_watcher(self):
        """No separate ping watcher is started for this model."""

    async def async_send_command(self, name, value):
        if name == "Set Source":
            await self.async_set_source(value)
        elif name == "Volume Up":
            await self.async_volume_up()
        elif name == "Volume Down":
            await self.async_volume_down()
        elif name == "Set HDMI Output":
            modes = {"A": 0, "B": 1, "A+B": 2, "Off": 3}
            if value not in modes:
                raise ValueError("HDMI output must be A, B, A+B or Off")
            await self.command_service.async_send_command(
                command(0xA0, 0x45, modes[value])
            )
            # Captures do not establish a getter/notification for HDMI output.
            self.last_requested_hdmi_output = value
            self._notify()
        elif name == "Set Sound Optimizer":
            modes = {"Off": 0, "Normal": 1, "Low": 2}
            if value not in modes:
                raise ValueError("Sound Optimizer must be Off, Normal or Low")
            await self.command_service.async_send_command(
                command(0xA3, 0x52, modes[value])
            )
        else:
            await super().async_send_command(name, value)
