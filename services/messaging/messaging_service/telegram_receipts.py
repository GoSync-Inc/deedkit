"""Normalize cached Telegram outbox boundaries without fetching provider data."""
from datetime import datetime, timezone
from .identifiers import ACCOUNT_RE


class UnsupportedReceiptPeer(Exception):
    """An outbox boundary for a peer type this collector does not know (ANYM-868): skipped by its reason code."""
    code = 'receipt_peer_unsupported'


def _verified(account, verified_self, expected_user_id):
    from telethon import types
    return (isinstance(account, str) and ACCOUNT_RE.fullmatch(account) is not None
            and type(expected_user_id) is int and 0 < expected_user_id < 2**63
            and isinstance(verified_self, types.User) and verified_self.is_self is True
            and type(verified_self.id) is int and verified_self.id == expected_user_id)


def telegram_group_receipt(account, update, *, verified_self, expected_user_id, observed_at=None):
    """A group or supergroup outbox boundary (ANYM-868): someone in the group read up to max_id, never who.

    A basic group arrives as `UpdateReadHistoryOutbox` with a `PeerChat`, a supergroup (or a broadcast channel:
    the update does not say which) as `UpdateReadChannelOutbox`. The chat is the stored marked ID; the archive
    skips a chat whose stored type is not a group (receipts.record). A private peer is `telegram_receipt`'s; an
    outbox update with another peer type raises UnsupportedReceiptPeer. Everything else, unverified or malformed,
    is ignored (None)."""
    from telethon import types, utils

    if not _verified(account, verified_self, expected_user_id): return None
    if isinstance(update, types.UpdateReadChannelOutbox):
        peer = types.PeerChannel(update.channel_id) if type(update.channel_id) is int else None
    elif isinstance(update, types.UpdateReadHistoryOutbox):
        if isinstance(update.peer, types.PeerUser): return None
        if not isinstance(update.peer, types.PeerChat): raise UnsupportedReceiptPeer()
        peer = update.peer if type(update.peer.chat_id) is int else None
    else: return None
    peer_id = getattr(peer, 'chat_id', getattr(peer, 'channel_id', None))
    if (peer_id is None or not 0 < peer_id < 2**63
            or type(update.max_id) is not int or not 0 < update.max_id < 2**63):
        return None
    chat = utils.get_peer_id(peer)
    if not -2**63 < chat < 0: return None
    observed = observed_at if observed_at is not None else datetime.now(timezone.utc)
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        return None
    return {
        'kind': 'message_receipt', 'account': account, 'platform': 'telegram',
        'chat': str(chat), 'provider_id': None, 'max_id': update.max_id,
        'state': 'read', 'basis': 'telegram_outbox_boundary', 'audience': 'provider_aggregate',
        'observed_at': observed.astimezone(timezone.utc).isoformat(), 'provider_at': None,
    }


def telegram_receipt(account, update, *, verified_self, expected_user_id, observed_at=None):
    """A private peer's outbox boundary proves read, not a precise read time.

    Other updates (inbox, content playback, groups/channels and Saved Messages)
    are not this function's: groups are telegram_group_receipt's (ANYM-868). Ignore malformed/unverified observations rather
    than stopping ordinary message collection or guessing receipt semantics.
    """
    from telethon import types

    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
        return None
    if type(expected_user_id) is not int or not 0 < expected_user_id < 2**63:
        return None
    if (not isinstance(verified_self, types.User) or verified_self.is_self is not True
            or type(verified_self.id) is not int or verified_self.id != expected_user_id):
        return None
    if not isinstance(update, types.UpdateReadHistoryOutbox) or not isinstance(update.peer, types.PeerUser):
        return None
    peer_id = update.peer.user_id
    if (type(peer_id) is not int or not 0 < peer_id < 2**63 or peer_id == expected_user_id
            or type(update.max_id) is not int or not 0 < update.max_id < 2**63):
        return None
    observed = observed_at if observed_at is not None else datetime.now(timezone.utc)
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        return None
    return {
        'kind': 'message_receipt', 'account': account, 'platform': 'telegram',
        'chat': str(peer_id), 'provider_id': None, 'max_id': update.max_id,
        'state': 'read', 'basis': 'telegram_outbox_boundary', 'audience': 'private_peer',
        'observed_at': observed.astimezone(timezone.utc).isoformat(), 'provider_at': None,
    }
