"""Server command-line entry: ``python -m server.main --host 0.0.0.0 --port 47801``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from common import netutil
from common.logging_setup import configure_logging
from common.protocol import TCP_PORT, UDP_PORT
from server.loop import Server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LANvas LAN whiteboard server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=TCP_PORT)
    parser.add_argument("--udp-port", type=int, default=UDP_PORT)
    parser.add_argument("--data", type=Path, default=Path("data"), help="data directory")
    parser.add_argument("--no-discovery", action="store_true", help="do not announce on the LAN")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(
        args.data / "logs" / "lanvas.log",
        console_level=logging.DEBUG if args.verbose else logging.INFO,
        file_level=logging.DEBUG if args.verbose else logging.INFO,
        datefmt="%H:%M:%S",
    )
    startup = logging.getLogger("lanvas.summary")
    startup.info("integrity scan starting, data in %s", args.data.resolve())
    server = Server(
        args.data,
        host=args.host,
        port=args.port,
        discovery=not args.no_discovery,
        discovery_port=args.udp_port,
    )
    try:
        server.start()
    except OSError as exc:
        startup.error("cannot start: %s", exc)
        return 1
    addresses = ", ".join(i.address for i in netutil.list_interfaces()) or "127.0.0.1"
    startup.info(
        "listening on TCP %d (UDP %d). LAN address: %s", server.port, args.udp_port, addresses
    )
    startup.info("%d room(s) loaded. Press Ctrl+C to stop.", len(server.rooms))

    def stop(*_: object) -> None:
        server.request_stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    server.run()
    startup.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
