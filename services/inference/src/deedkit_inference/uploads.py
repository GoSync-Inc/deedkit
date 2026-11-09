"""Request bodies streamed to disk with a cap and a clock; nothing is held in memory but one chunk."""

import asyncio
import os
from pathlib import Path

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

from .errors import ServiceError

FIELD_LIMIT = 1024
AUDIO_FIELDS = ("model", "language", "response_format")
DOCUMENT_FIELDS = ("model",)


def too_large():
    return ServiceError(413, "payload_too_large", "the request body is larger than allowed", close=True)


def timed_out():
    return ServiceError(408, "upload_timeout", "the body did not arrive in time", close=True)


def invalid(message):
    return ServiceError(422, "invalid_request", message, close=True)


def open_private(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")


async def stream_to_file(request, path, *, limit, declared, seconds):
    """Write the body to `path`; 413 as soon as it is longer than `limit` or than it declared."""
    total = 0
    try:
        async with asyncio.timeout(seconds):
            with open_private(path) as handle:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > limit or total > declared:
                        raise too_large()
                    handle.write(chunk)
    except TimeoutError:
        raise timed_out() from None
    return total


async def read_audio_form(request, directory, *, limit, declared, seconds):
    return await read_file_form(request, directory, limit=limit, declared=declared, seconds=seconds,
                                allowed=AUDIO_FIELDS, file_name="audio.bin")


async def read_document_form(request, directory, *, limit, declared, seconds):
    """ANYM-850: the same form with a `file` and a `model` only."""
    return await read_file_form(request, directory, limit=limit, declared=declared, seconds=seconds,
                                allowed=DOCUMENT_FIELDS, file_name="document.bin")


async def read_file_form(request, directory, *, limit, declared, seconds, allowed, file_name):
    """Parse multipart/form-data while streaming: the `file` part goes to a fixed name in `directory` (its own
    file name is never used), the other parts are small strings. Returns (fields, file_path, total_bytes)."""
    content_type = request.headers.get("content-type", "")
    main, params = parse_options_header(content_type.encode("latin-1", "ignore"))
    boundary = params.get(b"boundary")
    if main != b"multipart/form-data" or not boundary:
        raise ServiceError(415, "unsupported_media_type", "multipart/form-data is required", close=True)
    fields, state = {}, {"file": None, "sink": None, "name": None, "seen_file": False}
    headers = {"field": bytearray(), "value": bytearray(), "all": {}}
    file_path = Path(directory) / file_name

    def part_begin():
        headers.update(field=bytearray(), value=bytearray(), all={})
        state.update(sink=None, name=None)

    def header_field(data, start, end):
        headers["field"] += data[start:end]

    def header_value(data, start, end):
        headers["value"] += data[start:end]

    def header_end():
        headers["all"][bytes(headers["field"]).lower()] = bytes(headers["value"])
        headers["field"], headers["value"] = bytearray(), bytearray()

    def headers_finished():
        _, disposition = parse_options_header(headers["all"].get(b"content-disposition", b""))
        name = disposition.get(b"name", b"").decode("utf-8", "replace")
        if name == "file":
            if state["seen_file"]:
                raise invalid("duplicate field")
            state["seen_file"] = True
            state["file"] = open_private(file_path)
            state["sink"] = "file"
        elif name in allowed:
            if name in fields:
                raise invalid("duplicate field")
            fields[name] = bytearray()
            state["sink"] = "field"
        else:
            raise invalid("unknown field")
        state["name"] = name

    def part_data(data, start, end):
        if state["sink"] == "file":
            state["file"].write(data[start:end])
        else:
            fields[state["name"]] += data[start:end]
            if len(fields[state["name"]]) > FIELD_LIMIT:
                raise invalid("field too long")

    def part_end():
        if state["sink"] == "file":
            state["file"].close()
            state["file"] = None

    parser = MultipartParser(
        boundary,
        {"on_part_begin": part_begin, "on_header_field": header_field, "on_header_value": header_value,
         "on_header_end": header_end, "on_headers_finished": headers_finished, "on_part_data": part_data,
         "on_part_end": part_end},
        max_size=limit,
    )
    total = 0
    try:
        async with asyncio.timeout(seconds):
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit or total > declared:
                    raise too_large()
                parser.write(chunk)
        parser.finalize()
    except TimeoutError:
        raise timed_out() from None
    except (FormParserError, ValueError):
        raise invalid("malformed multipart body") from None
    except ClientDisconnect:
        raise
    finally:
        if state["file"] is not None:
            state["file"].close()
    if not state["seen_file"] or "model" not in fields:
        raise invalid("file and model are required")
    try:
        decoded = {name: bytes(value).decode("utf-8") for name, value in fields.items()}
    except UnicodeDecodeError:
        raise invalid("fields must be UTF-8") from None
    return decoded, file_path, total
