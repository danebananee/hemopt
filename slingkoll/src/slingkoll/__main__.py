"""Entry point: `python -m slingkoll [--demo] [--port 8099]`."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from .ha import HAClient
from .runner import Runner, run_forever
from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="slingkoll")
    parser.add_argument("--demo", action="store_true", help="kör mot ett simulerat hus")
    parser.add_argument("--speed", type=float, default=600.0, help="demo: simulerade sekunder per sekund")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = threading.Event()

    if args.demo:
        from .sim import SimHA, demo_house

        ha = SimHA(demo_house(), history_days=3)
        runner = Runner(ha, Path(tempfile.mkdtemp(prefix="slingkoll-")))
        runner.start()

        def simulate() -> None:
            while not stop.is_set():
                ha.advance(60)
                runner.tick()
                time.sleep(60 / args.speed)

        threading.Thread(target=simulate, daemon=True).start()
    else:
        runner = Runner(HAClient.from_env())
        runner.start()
        threading.Thread(target=run_forever, args=(runner, 60.0, stop), daemon=True).start()

    server = make_server(runner, args.host, args.port, demo=args.demo)

    def terminate(*_: object) -> None:
        stop.set()
        runner.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    logging.getLogger("slingkoll").info("Slingkoll lyssnar på port %d", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
