"""Run under launchd on the USB host. Bind to loopback or its private tailnet address."""

import argparse
import ipaddress
import sys

import uvicorn

from .app import create_app


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['secret']:
        # App secrets for ${secret:NAME}: `deedkit-android secret set|list|delete` (ANYM-969).
        from .secrets import main as secrets_main
        raise SystemExit(secrets_main(argv[1:]))
    parser = argparse.ArgumentParser(epilog='App secrets: deedkit-android secret set|list|delete --config FILE.')
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13246)
    args = parser.parse_args(argv)
    address = ipaddress.ip_address(args.host)
    if not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
        parser.error("bind to loopback or a Tailscale IPv4 address")
    # Leave headroom beyond the gateway's five-second idle pool expiry and
    # tailnet transit, so it retires a connection before this server closes it.
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, access_log=False,
                timeout_keep_alive=30)


if __name__ == "__main__":
    main()
