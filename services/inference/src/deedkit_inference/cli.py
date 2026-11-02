"""`deedkit-inference serve`: validate everything, bind loopback, drain on SIGTERM."""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Config, load_config
from .errors import ConfigError, PolicyError
from . import pausestate
from .models import ModelError, load_models, verify_all

EXIT_REFUSED = 2
EXIT_DRAIN_TIMEOUT = 3
EXIT_RECOVER_FAILED = 4
log = logging.getLogger(__name__)


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


DEFAULT_MODEL_DIR = Config.model_fields["model_dir"].default
CANARY_DIR = Path(__file__).resolve().parents[2] / "tests" / "canary"


def command_pull(args):
    """The only command that downloads. Run it as the account that owns the model store, never as the service user."""
    from .pull import pull  # not imported by the service: it is the one module that talks to the Hub

    try:
        done = pull(load_models(), args.model_dir, only=args.only, min_free_gb=args.min_free_gb,
                    log=lambda line: print(line, file=sys.stderr))
    except ModelError as error:
        print(f"deedkit-inference pull: {error.code} {error.detail}".strip(), file=sys.stderr)
        return 1
    print("\n".join(f"verified {key}" for key in done))
    return 0


def command_verify(args):
    try:
        results = verify_all(load_models(), args.model_dir)
    except ModelError as error:
        print(f"deedkit-inference verify: {error.code}", file=sys.stderr)
        return 1
    for key, problem in results:
        print(f"{'ok  ' if problem is None else 'FAIL'} {key}{'' if problem is None else ' ' + problem}")
    return 0 if all(problem is None for _, problem in results) else 1


def command_canary(args):
    from . import canary

    code = 0
    if args.egress:
        probe = subprocess.run([sys.executable, "-I", "-m", "deedkit_inference.egress_probe"], check=False,
                               capture_output=True, text=True, timeout=60)
        print("egress", probe.stdout.strip() or probe.stderr.strip()[:200])
        code = probe.returncode
    if not args.key_file:
        return code
    directory = Path(args.set)
    if args.generate or not directory.is_dir():
        subprocess.run([sys.executable, str(CANARY_DIR / "generate.py"), str(directory)], check=True)
    expected = json.loads((CANARY_DIR / "expected.json").read_text())
    truth = json.loads((directory / "truth.json").read_text())
    truth.update({name: (directory / name).read_text() for name in ("voice_ru.txt", "voice_en.txt")})
    key = Path(args.key_file).read_text().strip()
    outputs = canary.run(args.url.rstrip("/"), key, directory, expected, (CANARY_DIR / "prompt.txt").read_text())
    failures = canary.score(outputs, expected, truth)
    print(json.dumps({"outputs": outputs, "failures": failures}, ensure_ascii=False, indent=1))
    return 1 if failures else code


def command_recover(args):
    """Continue and kill the process groups a previous service left stopped (ANYM-691 criterion 5); --check only reads."""
    path = pausestate.ledger_path(args.state_dir)
    try:
        if args.check:
            groups = pausestate.Ledger(path).groups()
            pausestate.process_table()
            print(json.dumps({"check": "ok", "groups": len(groups)}))
            return 0
        report = pausestate.recover(path)
    except pausestate.LedgerError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return EXIT_RECOVER_FAILED
    print(json.dumps(report))
    return EXIT_RECOVER_FAILED if report["kept"] else 0


def recover_at_start(config):
    """Never blocks or fails the start: a stopped group left by the previous instance is continued and killed, or logged."""
    if not config.usage_file:
        return
    try:
        report = pausestate.recover(pausestate.ledger_path(Path(config.usage_file).parent))
        if report["groups"]:
            log.warning("startup recovery of stopped engine groups: %s", report)
    except Exception as error:  # noqa: BLE001 - recovery must never stop the service from starting
        log.error("startup recovery failed: %s", error)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="deedkit-inference", description="DeedKit shared inference service")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="validate the configuration and serve on loopback")
    run.add_argument("--config", default=os.environ.get("DEEDKIT_INFERENCE_CONFIG"))
    fetch = sub.add_parser("pull", help="download and verify the pinned models (the only command that does)")
    fetch.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    fetch.add_argument("--only", action="append", help="an alias or alias/phase; repeat")
    fetch.add_argument("--min-free-gb", type=float, default=15.0)
    check = sub.add_parser("verify", help="re-hash every pinned file, no network")
    check.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    probe = sub.add_parser("canary", help="run the regression canary against a running service")
    probe.add_argument("--url", default="http://127.0.0.1:8090")
    probe.add_argument("--key-file")
    probe.add_argument("--set", default=str(Path.home() / "canary-set"), help="directory of the generated set")
    probe.add_argument("--generate", action="store_true", help="(re)generate the set (needs Pillow, say, ffmpeg)")
    probe.add_argument("--egress", action="store_true", help="run the egress probe as an engine child would")
    rec = sub.add_parser("recover", help="continue and kill engine groups a dead service left stopped")
    rec.add_argument("--state-dir", required=True)
    rec.add_argument("--check", action="store_true", help="only read the ledger and the process table")
    args = parser.parse_args(argv)
    if args.command == "recover":
        raise SystemExit(command_recover(args))
    if args.command == "pull":
        raise SystemExit(command_pull(args))
    if args.command == "verify":
        raise SystemExit(command_verify(args))
    if args.command == "canary":
        raise SystemExit(command_canary(args))
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
    recover_at_start(config)
    raise SystemExit(asyncio.run(serve(app, config)))
