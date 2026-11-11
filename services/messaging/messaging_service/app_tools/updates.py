"""Update tools: read changes and acknowledge them per consumer (moved from app.py, W5-2c).

Signatures and docstrings are the published tool schemas and descriptions; the canonical contract snapshot
(tests/platform/fixtures/messaging-contracts.json) pins them byte for byte.
"""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import StrictInt, StrictStr

from ..tool_types import Detail, EventKind, EventOrigin


def register_update_tools(mcp, call):
    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False,
        destructiveHint=False, openWorldHint=False))
    def get_updates(ctx: Context, consumer: str='main', limit: int=20,
                    accounts: list[StrictStr] | None=None, chats: list[StrictStr] | None=None,
                    senders: list[StrictStr] | None=None, origins: list[EventOrigin] | None=None,
                    kinds: list[EventKind] | None=None, direction: Literal['','incoming','outgoing']='', from_time: str='', to_time: str='',
                    compact: bool | None=None, body_chars: StrictInt | None=None, include_coverage: bool | None=None, detail: Detail='compact',
                    exclude_channels: bool=False, order: Literal['event','sent_at']='event',
                    reset_filters: bool=False, expected_ack_version: StrictInt | None=None, start_from: Literal['beginning','now']='beginning',
                    view: Literal['events','by_chat']='events') -> dict[str, Any]:
        """Read changes without advancing ACK. Consumer belongs to this principal.
        origins=['live'] excludes backfill. kinds=['message.status'] selects passive receipt
        updates, without marking provider chats read. order=sent_at sorts the delivered batch only;
        ACK/pagination stay ingestion ordered. To replace this consumer's filters explicitly,
        use reset_filters=true with its current expected_ack_version and cursor-write permission.
        Reset preserves acknowledged position and invalidates outstanding receipts; it does not replay old history.
        view=by_chat: read-only overview, one row per chat with new incoming messages after this consumer's ACK
        and from_time (from_time required without an ACK): live and backfill counts, the newest live message,
        owner_replied_after_newest, read state, per account source state; newest live first, limit = chats.
        No receipt; origins, kinds, order and outgoing are invalid.
        """
        return call(ctx,'get_updates',detail=detail,consumer=consumer,limit=limit,accounts=accounts,chats=chats,senders=senders,
                    origins=origins,kinds=kinds,direction=direction,from_time=from_time,to_time=to_time,
                    compact=compact,body_chars=body_chars,include_coverage=include_coverage,
                    exclude_channels=exclude_channels,order=order,reset_filters=reset_filters,
                    expected_ack_version=expected_ack_version,start_from=start_from,view=view)

    @mcp.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False,
        destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def ack_updates(ctx: Context, consumer: str, receipt: str, expected_version: int) -> dict[str, Any]:
        """Acknowledge exactly the delivered page after processing; independent of other clients."""
        return call(ctx,'ack_updates',consumer=consumer,receipt=receipt,expected_version=expected_version)
