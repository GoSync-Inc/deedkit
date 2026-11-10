"""Run under launchd on the USB host. Bind to loopback or its private tailnet address."""

import argparse
import ipaddress

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13246)
    args = parser.parse_args()
    address = ipaddress.ip_address(args.host)
    if not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
        parser.error("bind to loopback or a Tailscale IPv4 address")
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
