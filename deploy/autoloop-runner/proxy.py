#!/usr/bin/env python3
"""The autoloop runner's egress proxy (ANYM-574, docs/operations/feedback-autoloop.md, Container runner).

The runner container sits on an internal Docker network with no route out; this proxy is its only way out. It
accepts `CONNECT <name>:443` for the names in ALLOWED (GitHub, the Anthropic API, Tiden and the package registries)
and nothing else: no plain HTTP, no other port, no IP literal, and no name that resolves to a private, loopback,
link-local, carrier-grade NAT (the tailnet) or otherwise non-global address. The tunnel's first TLS record must
be a ClientHello whose server name equals the CONNECT name, so an allowed name cannot carry a TLS session to another
name on a shared address.

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
RECORD = 16384 + 2048  # a TLS record's largest body


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


def server_name(record):
    """The SNI host name of one TLS record holding a ClientHello, or None when it is anything else."""
    def u16(at):
        return int.from_bytes(body[at:at + 2], 'big')
    try:
        if record[0] != 0x16 or int.from_bytes(record[3:5], 'big') != len(record) - 5:
            return None
        body = record[5:]
        if body[0] != 0x01:
            return None
        at = 4 + 2 + 32  # handshake header, version, random
        at += 1 + body[at]  # session id
        at += 2 + u16(at)  # cipher suites
        at += 1 + body[at]  # compression methods
        end = min(at + 2 + u16(at), len(body))
        at += 2
        while at + 4 <= end:
            kind, size = u16(at), u16(at + 2)
            if kind == 0:  # server_name: list length, name type 0 (host name), name length, name
                if body[at + 6] != 0 or at + 9 + u16(at + 7) > at + 4 + size:
                    return None
                return body[at + 9:at + 9 + u16(at + 7)].decode('ascii').lower()
            at += 4 + size
    except (IndexError, UnicodeDecodeError):
        return None
    return None


async def hello(reader):
    """The client's first TLS record, read whole (at most one record), or None."""
    try:
        head = await asyncio.wait_for(reader.readexactly(5), TIMEOUT)
        size = int.from_bytes(head[3:5], 'big')
        if size > RECORD:
            return None
        return head + await asyncio.wait_for(reader.readexactly(size), TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        return None


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
    record = await hello(reader)
    if record is None or server_name(record) != host:
        print(f'refused {host!r}: TLS server name {str(server_name(record) if record else None)[:64]!r}',
              file=sys.stderr, flush=True)
        upstream_writer.close()
        writer.close()
        return
    upstream_writer.write(record)
    await upstream_writer.drain()
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
