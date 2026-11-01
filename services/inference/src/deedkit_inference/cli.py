"""`deedkit-inference serve`: validate everything, bind loopback, drain on SIGTERM."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import contextmanager

import uvicorn

from .app import create_app
from .config import load_config
from .errors import ConfigError, PolicyError

EXIT_REFUSED = 2
EXIT_DRAIN_TIMEOUT = 3


class DrainingServer(uvicorn.Server):
    """uvicorn without its own signal handling: the drain below decides when the server stops."""

    @contextmanager
    def capture_signals(self):
        yield


async def serve(app, config):
    service = app.state.service
    server = DrainingServer(uvicorn.Config(
        app, host=config.bind, port=config.port, log_config=None, access_log=False, server_header=False,
        timeout_graceful_shutdown=5, lifespan="on",
    ))
    outcome = {"code": 0}
    drain_task = []

    async def drain():
        clean = await service.drain()
        outcome["code"] = 0 if clean else EXIT_DRAIN_TIMEOUT
        server.should_exit = True

    def on_signal():
        if not drain_task:
            drain_task.append(asyncio.create_task(drain()))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)
    await server.serve()
    if drain_task:
        await asyncio.gather(*drain_task)
    return outcome["code"]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="deedkit-inference", description="DeedKit shared inference service")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="validate the configuration and serve on loopback")
    run.add_argument("--config", default=os.environ.get("DEEDKIT_INFERENCE_CONFIG"))
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config or DEEDKIT_INFERENCE_CONFIG is required")
    try:
        config = load_config(args.config)
        app = create_app(config)
    except (ConfigError, PolicyError) as error:
        print(f"deedkit-inference: refusing to start: {error}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from None
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", force=True
    )
    raise SystemExit(asyncio.run(serve(app, config)))
