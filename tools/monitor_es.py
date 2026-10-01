"""Read-only CIS-IP monitor, runnable without Home Assistant or dependencies."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
from datetime import datetime, timezone
from pathlib import Path


def emit(**fields):
    print(
        json.dumps({"time": datetime.now(timezone.utc).isoformat(), **fields}),
        flush=True,
    )


class FrameLog(logging.Handler):
    def emit(self, record):
        emit(event="debug", message=record.getMessage())


async def monitor(args):
    path = Path(__file__).resolve().parents[1] / "custom_components/sonyavr/cis.py"
    spec = importlib.util.spec_from_file_location("sonyavr_cis_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logger = logging.getLogger(spec.name)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(FrameLog())
    client = module.CISConnection(
        args.host,
        args.port,
        lambda frame: emit(event="frame", hex=frame.hex()),
        lambda connected: emit(event="connection", connected=connected),
    )
    emit(event="start", port=args.port, duration=args.seconds)
    task = asyncio.create_task(client.run())
    try:
        done, _ = await asyncio.wait({task}, timeout=args.seconds)
        if done:
            await task
    finally:
        await client.stop()
        await task
        emit(event="stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Receiver IP address or hostname")
    parser.add_argument("--port", type=int, default=33335)
    parser.add_argument("--seconds", type=float, default=180)
    args = parser.parse_args()
    if args.seconds <= 0 or not 0 < args.port <= 65535:
        parser.error("seconds must be positive and port must be between 1 and 65535")
    try:
        asyncio.run(monitor(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
