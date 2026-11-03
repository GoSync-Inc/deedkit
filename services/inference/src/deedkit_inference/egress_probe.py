"""Run as an engine child (same sandbox, same user), this tries to leave the machine and must fail every time:
TCP and UDP to a loopback port other than the service's own, and to a public address. Prints one JSON object and
exits 0 only when every attempt failed. `deedkit-inference canary --egress` runs it."""

import json
import socket
import sys

TARGETS = {
    "tcp_loopback": (socket.SOCK_STREAM, "127.0.0.1", 9),
    "udp_loopback": (socket.SOCK_DGRAM, "127.0.0.1", 9),
    "tcp_public": (socket.SOCK_STREAM, "1.1.1.1", 53),
    "udp_public": (socket.SOCK_DGRAM, "1.1.1.1", 53),
}


def attempt(kind, host, port):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.settimeout(3)
        try:
            if kind == socket.SOCK_STREAM:
                sock.connect((host, port))
            else:
                sock.sendto(b"deedkit-egress-probe", (host, port))
        except OSError as error:
            return f"refused:{type(error).__name__}"
        return "OPEN"


def main():
    result = {name: attempt(*target) for name, target in TARGETS.items()}
    print(json.dumps(result))
    return 0 if all(value.startswith("refused") for value in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
