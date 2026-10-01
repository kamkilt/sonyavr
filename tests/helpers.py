"""Synthetic CIS-IP receiver; no personal device data or captures are required."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

from custom_components.sonyavr.es_receiver import SonyESReceiver


def reply(category, opcode, *data):
    body = bytes((len(data) + 2, category, opcode, *data))
    return b"\x02" + body + bytes((-sum(body) & 255,))


def sources_page(offset=0, entries=((0x10, "VIDEO1", "Streamer"), (0x1D, "PC", ""))):
    data = bytearray((0,))
    for index in range(10):
        record = bytearray(21)
        if index < len(entries):
            code, name, custom = entries[index]
            record[:4] = bytes((offset + index, code, code, 0))
            record[4:12] = name.encode().ljust(8, b" ")[:8]
            record[12:20] = custom.encode().ljust(8, b" ")[:8]
        else:
            record[0] = 255
        data.extend(record)
    data.extend(b"\x00\x00")
    return reply(0xA8, 0x8B, *data)


def receiver(port=33335):
    entry = SimpleNamespace(options={}, entry_id="test")
    return SonyESReceiver(
        SimpleNamespace(), entry, "127.0.0.1", "Test AVR", "STR-DA5800ES", port
    )


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class FakeAmp:
    """Handle real TCP streams, including delayed replies and dropped sessions."""

    def __init__(self):
        self.server = None
        self.writers = set()
        self.tasks = set()
        self.commands = []
        self.connections = 0
        self.flags = 0x23
        self.volume = -39.5
        self.source = 0x10
        self.mode = 0x27
        self.optimizer = 1
        self.fragment = False
        self.silent = set()
        self.rejected = set()
        self.close_after_off = False

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1]

    async def start(self, port=0):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", port)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        self.connections += 1
        try:
            while True:
                header = await reader.readexactly(2)
                packet = header + await reader.readexactly(header[1] + 1)
                self.commands.append(packet)
                key = tuple(packet[2:4])
                if key in self.silent:
                    continue
                if key in self.rejected:
                    response = bytes((2, 2, packet[2] + 8, 0x0E))
                elif key == (0xA0, 0x82):
                    response = reply(
                        0xA8, 0x82, 0, self.source, self.source, self.flags, 1
                    )
                elif key == (0xA0, 0x92):
                    fixed = int(self.volume * 256)
                    response = reply(0xA8, 0x92, 0, 1, (fixed >> 8) & 255, fixed & 255)
                elif key == (0xA3, 0x82):
                    response = reply(0xAB, 0x82, self.mode, 0)
                elif key == (0xA3, 0x92):
                    response = reply(0xAB, 0x92, 0, self.optimizer)
                elif key == (0xA0, 0x8B):
                    response = sources_page()
                else:
                    if key == (0xA0, 0x52):
                        self.volume = (
                            int.from_bytes(packet[6:8], "big", signed=True) / 256
                        )
                    elif key == (0xA0, 0x53):
                        self.flags = (self.flags & ~2) | (packet[5] * 2)
                    elif key == (0xA0, 0x42):
                        self.source = packet[5]
                    elif key == (0xA0, 0x60):
                        self.flags = (self.flags & ~1) | packet[5]
                    elif key == (0xA3, 0x42):
                        self.mode = packet[4]
                    elif key == (0xA3, 0x52):
                        self.optimizer = packet[4]
                    response = b"\xfd"
                if self.fragment:
                    for byte in response:
                        writer.write(bytes((byte,)))
                        await writer.drain()
                        await asyncio.sleep(0)
                else:
                    writer.write(response)
                    await writer.drain()
                if key == (0xA0, 0x60) and packet[5] == 0 and self.close_after_off:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def disconnect(self):
        for writer in tuple(self.writers):
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await self.disconnect()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks))
