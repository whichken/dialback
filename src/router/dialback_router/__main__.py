"""Entry point: python -m dialback_router /path/to/dialback.yaml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import yaml

from .rules import RuleEngine
from .server import RouterServer


def main() -> int:
    parser = argparse.ArgumentParser(description="Dialback router service")
    parser.add_argument("config", help="path to dialback.yaml")
    parser.add_argument("--bind", default=None, help="override bind address")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    with open(args.config) as f:
        config = yaml.safe_load(f)

    bind = args.bind or config.get("listen_host", "0.0.0.0")
    port = args.port or int(config.get("listen_port", 8888))

    engine = RuleEngine.from_config(config)
    server = RouterServer(engine)

    try:
        asyncio.run(server.serve(bind, port))
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
