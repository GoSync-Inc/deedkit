"""`agent-runner serve | stop-all | resume | check-config`.

serve binds exactly two sockets: the mini's tailnet address on 8448 with TLS (a `tailscale cert` certificate),
answering only production's tailnet address, and loopback for the local CLI. Any other bind refuses to start.
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from . import config as cfg
from .app import PeerGuard, create_app
from .docker import DockerCLI
from .hostgate import CommandGate
from .runner import Runner, claude_token, stop_all
from .store import Store

EXIT_REFUSED = 2
LOOPBACK = ("127.0.0.1", "::1")


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):  # one handler below stops both servers
        yield


def secret_env(config):
    def values(engine):
        return claude_token(config) if engine == "claude" and config.engine("claude") else {}
    return values


def build(config):
    docker = DockerCLI(config, secret_env(config))
    return Runner(config, docker, CommandGate(config.host_gate, config.host_check))


def check_files(config):
    for path in (config.listen.tls_cert, config.listen.tls_key):
        if not Path(path).is_file():
            raise cfg.ConfigError("tls_file_missing", str(path))


async def serve(runner, keys, config):
    app = create_app(runner, keys)
    listen = config.listen
    public = Server(uvicorn.Config(
        PeerGuard(app, [config.production_peer]), host=listen.tailnet_host, port=listen.port,
        ssl_certfile=str(listen.tls_cert), ssl_keyfile=str(listen.tls_key), log_config=None, access_log=False,
        server_header=False, lifespan="off"))
    local = Server(uvicorn.Config(
        PeerGuard(app, LOOPBACK), host=listen.loopback_host, port=listen.loopback_port, log_config=None,
        access_log=False, server_header=False, lifespan="off"))

    def on_signal():
        public.should_exit = local.should_exit = True
        runner.shutdown()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)
    await asyncio.gather(public.serve(), local.serve())


def command_serve(args):
    config = cfg.load(args.config)
    check_files(config)
    runner = build(config)
    runner.startup()
    worker = threading.Thread(target=runner.serve_forever, name="agent-scheduler", daemon=True)
    worker.start()
    asyncio.run(serve(runner, cfg.Keys(args.config), config))
    worker.join(5)
    return 0


def command_stop_all(args):
    config = cfg.load(args.config)
    store = Store(Path(config.state_dir) / "tasks.sqlite3", time.time)
    stopped = stop_all(store, DockerCLI(config), Path(config.state_dir) / "pause")
    print(f"paused; stopped {len(stopped)} task(s)")
    return 0


def command_resume(args):
    config = cfg.load(args.config)
    (Path(config.state_dir) / "pause").unlink(missing_ok=True)
    print("resumed")
    return 0


def command_check(args):
    config = cfg.load(args.config)
    check_files(config)
    print("ok")
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="agent-runner")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, text in (("serve", command_serve, "validate the configuration and serve"),
                                ("stop-all", command_stop_all, "pause new starts and stop every task"),
                                ("resume", command_resume, "remove the pause file"),
                                ("check-config", command_check, "validate the configuration only")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--config", required=True, type=Path)
        command.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except cfg.ConfigError as refused:
        print(f"agent-runner {args.command}: refused: {refused}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
