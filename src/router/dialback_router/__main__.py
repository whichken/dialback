"""Entry point: python -m dialback_router /path/to/dialback.yaml"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys

import yaml

from .admin import AdminApp
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
    admin_cfg = config.get("admin") or {}
    admin_port = admin_cfg.get("listen_port")

    engine = RuleEngine.from_config(config)

    # persisted era choice (set via the control UI) wins over config file
    saved = AdminApp.load_state()
    if saved.get("era") and saved["era"] != engine.era and engine.era_dir:
        try:
            engine.switch_era(saved["era"])
            logging.info("restored persisted era %s from state file", saved["era"])
        except FileNotFoundError:
            logging.warning("persisted era %s not found; keeping %s",
                            saved["era"], engine.era)

    request_log = collections.deque(maxlen=200)
    admin_app = AdminApp(engine, request_log, admin_cfg)
    server = RouterServer(engine, request_log, admin_app)

    try:
        asyncio.run(server.serve(bind, port, admin_port))
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
