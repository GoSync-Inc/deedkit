"""Normalize cached Telegram outbox boundaries without fetching provider data."""
from datetime import datetime, timezone
from .identifiers import ACCOUNT_RE


def telegram_receipt(account, update, *, verified_self, expected_user_id, observed_at=None):
    """A private peer's outbox boundary proves read, not a precise read time.

    Other updates (inbox, content playback, groups/channels and Saved Messages)
    are deliberately unsupported. Ignore malformed/unverified observations rather
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
