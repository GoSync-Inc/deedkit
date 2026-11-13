"""Native MCP images for the Android observation tool only."""

import base64
import asyncio
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
                                     "sha256": hashlib.sha256(data).hexdigest(),
                                     **{key: picture[key] for key in ("width", "height") if key in picture}}}
    return [TextContent(type="text", text=json.dumps(structured, ensure_ascii=False, separators=(",", ":"))),
            ImageContent(type="image", mimeType="image/png", data=encoded)], structured


async def wake_step(dispatch):
    """Bounded metadata producer. Root-configured routes/credentials only; no subscription/provider ACK."""
    from ..android_wake_bridge import Route, step
    snapshot = dispatch.policy.load()
    statuses = []
    for binding in snapshot.get('android_wakes', []):
        route = Route(**{k: binding[k] for k in ('owner_id', 'device_id', 'source_id', 'auth_epoch', 'enabled')})
        if not route.enabled:
            statuses.append({'status': 'unavailable', 'reason': 'android_wake_paused'})
            continue
        actor = next(p for p in snapshot['principals'] if p['id'] == binding['principal_id'])

        def admit():
            current, principal, _ = dispatch.policy.authorize_digest(actor['key_sha256'], 'android_agent')
            if (current['revision'] != snapshot['revision'] or binding not in current.get('android_wakes', [])
                    or principal['id'] != binding['principal_id']):
                raise Denied('policy_changed', 409)
            if any(module not in principal['credentials'] for module in ('android', 'messaging')):
                raise Denied('android_wake_credentials_unavailable', 503)
            return current, principal

        async def call(module, operation, body):
            current, principal = admit()
            key = dispatch.call_credential(current, principal, module)
            try:
                result = await dispatch.clients.call(module, operation, key, body)
            except Denied:
                raise OSError('android_wake_backend_unconfirmed') from None
            admit()  # no policy/key snapshot survives an await
            return result

        async def read_source(after, limit):
            return await call('android', 'order_source', {'device_id': route.device_id,
                'source_id': route.source_id, 'auth_epoch': route.auth_epoch, 'after': after, 'limit': limit})

        try:
            async with asyncio.timeout(10):
                statuses.append(await step(route,
                    read_cursor=lambda device: call('messaging', 'android-wakes/state', {'device_id': device}),
                    read_source=read_source,
                    stage=lambda body: call('messaging', 'android-wakes/stage', body),
                    receipt=lambda wake: call('messaging', 'android-wakes/receipt', {'wake_id': wake})))
        except (Denied, OSError, ValueError):
            statuses.append({'status': 'held', 'reason': 'android_wake_admission_or_receipt_unconfirmed'})
    return statuses


async def wake_loop(dispatch, status):
    """One task owned by the existing gateway lifespan; bounded reads, no native or phone process."""
    while True:
        try:
            status[:] = await wake_step(dispatch)
        except (Denied, OSError, ValueError):
            status[:] = [{'status': 'held', 'reason': 'android_wake_policy_unavailable'}]
        await asyncio.sleep(30)
