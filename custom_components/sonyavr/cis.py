"""CIS-IP framing and a single connection for STR-DA5800ES control and events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

_LOGGER = logging.getLogger(__name__)
ACK = b"\xfd"
NACK = b"\xfe"


class CommandRejected(ValueError):
    """The receiver rejected a command without losing its connection."""


def command(category: int, opcode: int, *data: int) -> bytes:
    """Build an IP request; ES Remote sends a zero trailer instead of serial CS."""
    return bytes((2, len(data) + 2, category, opcode, *data, 0))


class FrameDecoder:
    """Decode fragmented/coalesced TCP data, including ACK and error exceptions."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            if self.buffer[0] in (0xFD, 0xFE):
                frames.append(bytes(self.buffer[:1]))
                del self.buffer[:1]
                continue
            if self.buffer[0] != 2:
                del self.buffer[:1]
                continue
            if len(self.buffer) < 4:
                break
            # NO-EXECUTABLE/NO-FUNCTION have no checksum byte.
            error = self.buffer[1] == 2 and self.buffer[3] in (0x0E, 0x0F)
            length = 4 if error else self.buffer[1] + 3
            if self.buffer[1] < 2:
                del self.buffer[:1]
                continue
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            # Captured ES replies carry the serial checksum, unlike requests.
            if not error and sum(frame[1:]) % 256:
                _LOGGER.debug("Discarding invalid CIS frame: %s", frame.hex())
                del self.buffer[:1]
                continue
            del self.buffer[:length]
            frames.append(frame)
        return frames


class CISConnection:
    """Serialize requests while receiving notifications on the same TCP socket."""

    def __init__(
        self,
        host: str,
        port: int,
        on_frame: Callable[[bytes], None],
        on_connection: Callable[[bool], None],
        *,
        timeout: float = 3,
        refresh_interval: float = 15,
        retry_interval: float = 2,
        retry_max: float = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.on_connection = on_connection
        self.timeout = timeout
        self.refresh_interval = refresh_interval
        self.retry_interval = retry_interval
        self.retry_max = retry_max
        self.writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._lost = asyncio.Event()
        self._pending: (
            tuple[Callable[[bytes], bool], asyncio.Future[bytes], int] | None
        ) = None

    @property
    def connected(self) -> bool:
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and not self._lost.is_set()
        )

    async def request(self, packet: bytes, matches: Callable[[bytes], bool]) -> bytes:
        """Wait for a matching reply, allowing unrelated events to pass through."""
        async with self._lock:
            if not self.connected:
                raise ConnectionError("Sony AVR is unavailable")
            writer = self.writer
            future = asyncio.get_running_loop().create_future()
            self._pending = (matches, future, packet[2] + 8)
            try:
                async with asyncio.timeout(self.timeout):
                    _LOGGER.debug("CIS TX: %s", packet.hex())
                    writer.write(packet)
                    await writer.drain()
                    return await future
            except (OSError, TimeoutError, asyncio.CancelledError):
                # Discard this session so a late ACK cannot complete a later command.
                writer.close()
                self._lost.set()
                raise
            finally:
                self._pending = None
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    # A concurrent EOF may fail the future before drain fails.
                    future.exception()

    async def send(self, packet: bytes) -> bytes:
        """Send a framed command; queries require a response predicate."""
        if len(packet) < 5 or packet[0] != 2 or len(packet) != packet[1] + 3:
            raise ValueError("Invalid CIS command frame")
        packet = packet[:-1] + b"\x00"
        if packet[3] >= 0x80:
            return await self.request(
                packet,
                lambda frame: (
                    len(frame) >= 5 and frame[2:4] == bytes((packet[2] + 8, packet[3]))
                ),
            )
        return await self.request(packet, lambda frame: frame == ACK)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        decoder = FrameDecoder()
        try:
            while data := await reader.read(4096):
                for frame in decoder.feed(data):
                    _LOGGER.debug("CIS RX: %s", frame.hex())
                    self.on_frame(frame)
                    if self._pending is None:
                        continue
                    matches, future, category = self._pending
                    if future.done():
                        continue
                    if frame == NACK or (
                        len(frame) == 4
                        and frame[2] == category
                        and frame[3] in (0x0E, 0x0F)
                    ):
                        future.set_exception(
                            CommandRejected(f"Sony AVR rejected command: {frame.hex()}")
                        )
                    elif matches(frame):
                        future.set_result(frame)
        except OSError:
            _LOGGER.debug("CIS connection closed", exc_info=True)
        finally:
            self._lost.set()
            if self._pending is not None and not self._pending[1].done():
                self._pending[1].set_exception(ConnectionError("Sony AVR disconnected"))

    async def status(self) -> bytes:
        return await self.request(
            command(0xA0, 0x82, 0),
            lambda frame: len(frame) == 10 and frame[2:5] == b"\xa8\x82\x00",
        )

    async def refresh(self) -> None:
        """Read state without changing power, source, mute or volume."""
        status = await self.status()
        if not status[7] & 1:
            return  # Only power/status requests are supported in standby.
        for packet, prefix, length in (
            (command(0xA0, 0x92, 0, 1), b"\xa8\x92\x00\x01", 9),
            (command(0xA3, 0x82), b"\xab\x82", 7),
            (command(0xA3, 0x92), b"\xab\x92", 7),
        ):
            try:
                await self.request(
                    packet,
                    lambda frame, prefix=prefix, length=length: (
                        len(frame) == length and frame[2 : 2 + len(prefix)] == prefix
                    ),
                )
            except CommandRejected:
                # These may not yet be executable while the receiver boots.
                _LOGGER.debug("State request is not executable yet: %s", packet.hex())

    async def read_sources(self) -> None:
        """Read the paginated main-zone input list captured in ES Remote traffic."""
        for page in range(8):
            frame = await self.request(
                command(0xA0, 0x8B, 0, page),
                lambda frame, page=page: (
                    len(frame) == 218
                    and frame[2:5] == b"\xa8\x8b\x00"
                    and frame[5] in (page * 10, 0xFF)
                ),
            )
            # Byte 5 is the first record's index, also used as the page offset.
            if any(frame[i] == 0xFF for i in range(5, 215, 21)):
                return

    async def run(self) -> None:
        """Reconnect indefinitely with bounded backoff, including cold startup."""
        retry = self.retry_interval
        while not self._stop.is_set():
            read_task = None
            try:
                async with asyncio.timeout(self.timeout):
                    reader, self.writer = await asyncio.open_connection(
                        self.host, self.port
                    )
                self._lost.clear()
                self.on_connection(True)
                read_task = asyncio.create_task(self._read(reader))
                await self.refresh()
                retry = self.retry_interval
                sources_loaded = False
                while not self._stop.is_set():
                    if not sources_loaded:
                        status = await self.status()
                        if status[7] & 1:
                            try:
                                await self.read_sources()
                                sources_loaded = True
                            except CommandRejected:
                                _LOGGER.debug("Input list is not executable yet")
                    try:
                        await asyncio.wait_for(self._lost.wait(), self.refresh_interval)
                        break
                    except TimeoutError:
                        await self.refresh()
            except (OSError, TimeoutError, CommandRejected) as err:
                _LOGGER.debug("CIS unavailable at %s:%s: %s", self.host, self.port, err)
            finally:
                self.on_connection(False)
                if self.writer is not None:
                    self.writer.close()
                    with suppress(OSError, TimeoutError):
                        async with asyncio.timeout(self.timeout):
                            await self.writer.wait_closed()
                    self.writer = None
                if read_task is not None:
                    read_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await read_task
            try:
                await asyncio.wait_for(self._stop.wait(), retry)
            except TimeoutError:
                retry = min(retry * 2, self.retry_max)

    async def stop(self) -> None:
        """Wake all waits; the owner awaits the run task to finish cleanup."""
        self._stop.set()
        self._lost.set()
        if self.writer is not None:
            self.writer.close()
