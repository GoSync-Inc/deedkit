#!/usr/bin/env python3
"""The autoloop runner's egress proxy (ANYM-574, docs/operations/feedback-autoloop.md, Container runner).

The runner container sits on an internal Docker network with no route out; this proxy is its only way out. It
accepts `CONNECT <name>:443` for the names in ALLOWED (GitHub, the Anthropic API, Tiden and the package registries)
and nothing else: no plain HTTP, no other port, no IP literal, and no name that resolves to a private, loopback,
link-local, carrier-grade NAT (the tailnet) or otherwise non-global address.

    proxy.py [--port 3128]
"""
import argparse
import asyncio
import ipaddress
import re
import socket
import sys

ALLOWED = frozenset({
    'github.com', 'api.github.com', 'codeload.github.com', 'uploads.github.com', 'objects.githubusercontent.com',
    'raw.githubusercontent.com', 'release-assets.githubusercontent.com',  # GitHub (git, gh, release downloads)
    'api.anthropic.com',  # the model API of the agent CLI
    'app.tiden.ai',  # Tiden
    'pypi.org', 'files.pythonhosted.org', 'registry.npmjs.org', 'proxy.golang.org', 'sum.golang.org',  # registries
})
PORT = 443
NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+')
REQUEST = re.compile(rb'CONNECT ([^\s:]+):([0-9]{1,5}) HTTP/1\.[01]\r?\n')
TIMEOUT, HEAD = 30, 8192


def allowed(host, port):
    """True for an allowlisted name on port 443 only; IP literals and other names never pass."""
    return port == PORT and isinstance(host, str) and bool(NAME.fullmatch(host)) and host in ALLOWED


def public(addresses):
    """The addresses that are global unicast; a name with any other address is refused whole (rebinding)."""
    found = [ipaddress.ip_address(a) for a in addresses]
    return [str(a) for a in found] if found and all(a.is_global and not a.is_multicast for a in found) else []


async def resolve(host):
    infos = await asyncio.get_running_loop().getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    return public(sorted({info[4][0] for info in infos}))


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(reader, writer, resolver=resolve):
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    found = REQUEST.match(head)
    host, port = (found[1].decode('ascii', 'replace').lower(), int(found[2])) if found else (None, None)
    addresses = await resolver(host) if allowed(host, port) else []
    if not addresses:
        print(f'refused {host!r}:{port}', file=sys.stderr, flush=True)
        writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(addresses[0], port), TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    await writer.drain()
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def serve(port):
    server = await asyncio.start_server(handle, '0.0.0.0', port, limit=HEAD)
    async with server:
        await server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description='autoloop runner egress proxy')
    parser.add_argument('--port', type=int, default=3128)
    asyncio.run(serve(parser.parse_args(argv).port))


if __name__ == '__main__':
    main()
