"""Client entry point: ``python -m client.main [--profile NAME] [--dark]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from client import paths
from common.protocol import UDP_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LANvas client")
    parser.add_argument("--profile", default="", help="separate identity on this machine")
    parser.add_argument("--dark", action="store_true", help="dark theme")
    parser.add_argument("--udp-port", type=int, default=UDP_PORT)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--smoke",
        nargs=3,
        metavar=("HOST", "PORT", "OUTDIR"),
        help="self-test a packaged build against a running server, then exit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.profile:
        paths.set_profile(args.profile)
    from client.app import AppWindow

    if args.smoke:
        from client import smoke

        host, port, out_dir = args.smoke
        return smoke.run(host, int(port), Path(out_dir), args.udp_port)
    window = AppWindow(args.udp_port, dark=args.dark)
    window.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
