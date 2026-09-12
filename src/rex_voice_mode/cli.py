from __future__ import annotations

import argparse
import signal
from pathlib import Path

from .boundary import BoundaryServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the safe Voice Chat control-plane boundary")
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    server = BoundaryServer(args.socket)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: server.close())
    try:
        server.serve()
    except KeyboardInterrupt:
        server.close()


if __name__ == "__main__":
    main()
