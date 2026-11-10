"""Native MCP images for the Android observation tool only."""

import base64
import binascii
import hashlib
import json

from mcp.types import ImageContent, TextContent

from ..policy import Denied

IMAGE_LIMIT = 16 * 1024 * 1024
ENCODED_LIMIT = ((IMAGE_LIMIT + 2) // 3) * 4


def content(name, result):
    if name != "android_observe" or "image" not in result:
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))], result
    picture = result["image"]
    try:
        if not isinstance(picture, dict) or picture.get("mime_type") != "image/png":
            raise ValueError
        encoded = picture["data"]
        if not isinstance(encoded, str) or len(encoded) > ENCODED_LIMIT:
            raise ValueError
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > IMAGE_LIMIT:
            raise ValueError
    except (KeyError, ValueError, binascii.Error):
        raise Denied("invalid_android_image", 502) from None
    structured = {**result, "image": {"mime_type": "image/png", "bytes": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()}}
    return [TextContent(type="text", text=json.dumps(structured, ensure_ascii=False, separators=(",", ":"))),
            ImageContent(type="image", mimeType="image/png", data=encoded)], structured
