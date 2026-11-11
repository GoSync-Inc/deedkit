"""ban_member, unban_member and get_member_state on Telegram (ANYM-935): synthetic Telethon objects only; never a
provider session."""
import asyncio
from datetime import datetime, timedelta, timezone
import time

import pytest
from telethon import errors, functions, types

from messaging_service.commands import CommandRejected
from messaging_service.telegram_governor import ProviderRateLimited
from messaging_service.telegram_operations import execute

CHANNEL = -1001234567890
BASIC = -4242
USER = 42
OWNER = 7
IDS = ['msg_' + str(n) * 32 for n in (1, 2)]


def admin(**granted):
    return types.ChatAdminRights(**{'ban_users': False, 'delete_messages': False, **granted})


def channel(**fields):
    return types.Channel(id=1234567890, title='Synthetic supergroup', photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=1, **{'megagroup': True, **fields})


def basic_group():
    return types.Chat(id=4242, title='Synthetic basic group', photo=types.ChatPhotoEmpty(), participants_count=3,
                      date=None, version=1, creator=True)


def member():
    return types.ChannelParticipant(user_id=USER, date=None)


def banned(until=None, left=False):
    return types.ChannelParticipantBanned(peer=types.PeerUser(USER), kicked_by=OWNER, date=None, left=left,
                                          banned_rights=types.ChatBannedRights(until_date=until, view_messages=True))


class Client:
    """Records every call. `states` are the GetParticipant answers in order (an exception is raised); `raises` maps a
    call name to what it raises; `cached` says whether the session holds the person before a message read."""
    def __init__(self, entity, states=(), raises=None, cached=True, deleted=True, linked=None):
        self.entity, self.states, self.raises, self.cached = entity, list(states), raises or {}, cached
        self.deleted, self.linked, self.calls = deleted, linked, []

    def named(self, name):
        if name in self.raises:
            raise self.raises[name]

    async def get_input_entity(self, peer):
        self.calls.append(('get_input_entity', peer))
        if isinstance(peer, types.PeerUser):
            self.named('person')
            if not self.cached:
                raise ValueError('not cached')
            return types.InputPeerUser(peer.user_id, 1)
        if peer > 0:
            return types.InputPeerUser(peer, 1)
        return types.InputPeerChannel(-peer - 1000000000000, 1) if peer <= -1000000000000 else types.InputPeerChat(-peer)

    async def get_entity(self, peer):
        self.calls.append(('get_entity', peer))
        self.named('get_entity')
        return self.entity

    async def delete_messages(self, peer, ids, **kwargs):
        self.calls.append(('delete_messages', ids, kwargs))
        self.named('delete_messages')
        return [types.messages.AffectedMessages(pts=1, pts_count=len(ids))]

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', ids))
        self.named('get_messages')
        if ids == [901]:  # the anchor read brings the person into the session
            self.cached = True
        return [None] * len(ids) if self.deleted else [object()] * len(ids)

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append((name, request))
        self.named(name)
        if isinstance(request, functions.channels.GetParticipantRequest):
            state = self.states.pop(0)
            if isinstance(state, Exception):
                raise state
            return types.channels.ChannelParticipant(participant=state, chats=[], users=[])
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return types.messages.ChatFull(full_chat=types.ChannelFull(
                id=1234567890, about='', read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                chat_photo=types.PhotoEmpty(0), notify_settings=types.PeerNotifySettings(), exported_invite=None,
                bot_info=[], pts=1, linked_chat_id=self.linked), chats=[], users=[])
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)


def job(operation='ban_member', chat=CHANNEL, **parameters):
    parameters = {'user': str(USER), **({'forever': True} if operation == 'ban_member' and 'until' not in parameters
                                        else {}), **parameters}
    count = len(parameters.get('message_ids', ()))
    return {'operation': operation, 'provider_chat': str(chat), 'expires_at': time.time() + 60,
            'parameters': parameters, 'person': {'user': str(USER), 'provider_message_id': '901'},
            **({'targets': [{'message_id': IDS[n], 'revision': 1, 'provider_message_id': str(101 + n)}
                            for n in range(count)]} if count else {}),
            '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}}


def with_messages(**parameters):
    return {'message_ids': IDS, 'expected_revisions': [1, 1], **parameters}


def run(client, operation='ban_member', chat=CHANNEL, verified_self=None, **parameters):
    return asyncio.run(execute(client, job(operation, chat, **parameters), verified_self=verified_self))


def names(client):
    return [call[0] for call in client.calls if call[0] not in ('get_input_entity', 'get_entity')]


def bans(client):
    return [call[1] for call in client.calls if call[0] == 'EditBannedRequest']


def ahead(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0)


def text(value):
    return value.isoformat().replace('+00:00', 'Z')


# spec: ANYM-935
def test_a_ban_sends_one_view_messages_ban_and_reads_the_state_back():
    client = Client(channel(admin_rights=admin(ban_users=True)), states=[member(), banned()])
    result = run(client)
    assert result['outcome'] == 'confirmed'
    assert (result['data']['changed'], result['data']['ban_request'], result['data']['state']) == (
        True, 'applied', 'banned')
    assert result['data']['banned_until'] is None
    assert result['data']['evidence'] == {'basis': 'provider_read_back', 'state': 'banned'}
    (request,) = bans(client)
    assert request.banned_rights.view_messages is True and request.banned_rights.until_date is None
    assert request.participant == types.InputPeerUser(USER, 1)
    end = ahead(days=3)
    dated = Client(channel(creator=True), states=[member(), banned(end)])
    result = run(dated, until=text(end))
    assert bans(dated)[0].banned_rights.until_date == end and result['data']['banned_until'] == text(end)


# spec: ANYM-935
def test_a_person_who_already_left_is_banned_and_one_not_in_the_session_is_found_by_their_archived_message():
    client = Client(channel(creator=True), states=[errors.UserNotParticipantError(None), banned(left=True)],
                    cached=False)
    result = run(client)
    assert result['outcome'] == 'confirmed' and result['data']['state'] == 'banned' and len(bans(client)) == 1
    assert names(client)[0] == 'get_messages' and client.calls[client.calls.index(('get_messages', [901])) + 1] == (
        'get_input_entity', types.PeerUser(USER))
    unknown = Client(channel(creator=True), cached=False)
    unknown.raises['get_messages'] = ValueError('no such message')
    with pytest.raises(CommandRejected, match='^provider_preflight_failed$'):
        run(unknown)
    assert bans(unknown) == []


# spec: ANYM-935
def test_a_person_already_in_the_requested_state_is_confirmed_unchanged_without_a_write():
    end = ahead(days=2)
    for state, parameters in ((banned(), {}), (banned(end), {'until': text(end + timedelta(seconds=20))})):
        client = Client(channel(creator=True), states=[state])
        result = run(client, **parameters)
        assert result['outcome'] == 'confirmed' and result['data']['changed'] is False
        assert result['data']['ban_request'] == 'not_needed' and bans(client) == []
    longer = Client(channel(creator=True), states=[banned(end), banned()])
    assert run(longer)['data']['changed'] is True and len(bans(longer)) == 1  # until -> forever is a new ban
    for state in (member(), errors.UserNotParticipantError(None)):
        client = Client(channel(creator=True), states=[state])
        result = run(client, 'unban_member')
        assert (result['outcome'], result['data']['changed'], result['data']['state']) == (
            'confirmed', False, 'not_banned')
        assert bans(client) == []


# spec: ANYM-935
def test_an_unban_sends_empty_rights_and_reads_not_banned_back():
    client = Client(channel(admin_rights=admin(ban_users=True)), states=[banned(), errors.UserNotParticipantError(None)])
    result = run(client, 'unban_member')
    assert (result['outcome'], result['data']['changed'], result['data']['state']) == ('confirmed', True, 'not_banned')
    (request,) = bans(client)
    assert request.banned_rights == types.ChatBannedRights(until_date=None)


# spec: ANYM-935
def test_the_listed_messages_are_deleted_before_the_ban():
    client = Client(channel(admin_rights=admin(ban_users=True, delete_messages=True)), states=[member(), banned()])
    result = run(client, **with_messages())
    assert names(client) == ['GetParticipantRequest', 'delete_messages', 'get_messages', 'EditBannedRequest',
                             'GetParticipantRequest']
    assert client.calls[[c[0] for c in client.calls].index('delete_messages')][1:] == ([101, 102], {'revoke': True})
    assert result['outcome'] == 'confirmed' and result['data']['state'] == 'banned'
    assert [row['state'] for row in result['data']['results']] == ['deleted', 'deleted']
    assert result['data']['all_deleted'] is True
    # Already banned: the spam is still deleted and nothing else is written.
    again = Client(channel(creator=True), states=[banned()])
    result = run(again, **with_messages())
    assert result['data']['changed'] is False and bans(again) == [] and result['data']['deleted'] == 2


# spec: ANYM-935
def test_an_uncertain_or_rate_limited_delete_sends_no_ban():
    unknown = Client(channel(creator=True), states=[member()], raises={'delete_messages': TimeoutError()})
    result = run(unknown, **with_messages())
    assert result['outcome'] == 'unknown' and result['data']['ban_request'] == 'not_attempted'
    assert bans(unknown) == [] and names(unknown).count('delete_messages') == 1
    flood = Client(channel(creator=True), states=[member()], raises={'delete_messages': ProviderRateLimited(40)})
    result = run(flood, **with_messages())
    assert result['data']['ban_request'] == 'not_attempted' and bans(flood) == []
    assert result['data']['results'][0]['evidence']['retry_after_s'] == 40


# spec: ANYM-935
@pytest.mark.parametrize('entity,listed,code,evidence', [
    (channel(), False, 'not_admin', None),
    (channel(admin_rights=admin(delete_messages=True)), False, 'missing_admin_right', {'missing_right': 'ban_users'}),
    (channel(admin_rights=admin(ban_users=True)), True, 'missing_admin_right', {'missing_right': 'delete_messages'}),
])
def test_rights_are_checked_before_anything_is_deleted_or_banned(entity, listed, code, evidence):
    client = Client(entity, states=[member(), banned()])
    with pytest.raises(CommandRejected, match='^%s$' % code) as failure:
        run(client, **(with_messages() if listed else {}))
    assert (failure.value.data or {}).get('evidence') == evidence
    assert names(client) == []


# spec: ANYM-935
def test_the_owner_an_admin_or_the_creator_is_protected():
    owner = types.User(id=USER, is_self=True)
    client = Client(channel(creator=True), states=[member()])
    with pytest.raises(CommandRejected, match='^target_protected$') as failure:
        run(client, verified_self=owner, **with_messages())
    assert failure.value.data == {'reason': 'owner'} and names(client) == []
    roles = {'admin': types.ChannelParticipantAdmin(user_id=USER, promoted_by=OWNER, date=None, admin_rights=admin()),
             'creator': types.ChannelParticipantCreator(user_id=USER, admin_rights=admin())}
    for reason, participant in roles.items():
        client = Client(channel(creator=True), states=[participant])
        with pytest.raises(CommandRejected, match='^target_protected$') as failure:
            run(client, **with_messages())
        assert failure.value.data == {'reason': reason} and names(client) == ['GetParticipantRequest']


# spec: ANYM-935
def test_a_timeout_or_a_flood_wait_after_sending_is_unknown_and_never_retried():
    for raised in (TimeoutError(), ProviderRateLimited(25)):
        client = Client(channel(creator=True), states=[member()], raises={'EditBannedRequest': raised})
        result = run(client)
        assert result['outcome'] == 'unknown' and result['data']['ban_request'] == 'unknown' and len(bans(client)) == 1
    assert result['data']['evidence']['retry_after_s'] == 25
    refused = Client(channel(creator=True), states=[member()], raises={'EditBannedRequest': errors.UserAdminInvalidError(None)})
    result = run(refused, **with_messages())
    assert result['data']['ban_request'] == 'refused' and result['data']['provider_code'] == 'USER_ADMIN_INVALID'
    early = Client(channel(creator=True), states=[ProviderRateLimited(30)])
    with pytest.raises(CommandRejected, match='^rate_limited$') as failure:
        run(early)
    assert failure.value.retry_after == 30 and bans(early) == []


# spec: ANYM-935
def test_a_basic_group_a_lapsed_end_or_a_malformed_job_refuses_before_any_write():
    with pytest.raises(CommandRejected, match='^unsupported_chat_type$'):
        run(Client(basic_group()), chat=BASIC)
    client = Client(channel(creator=True))
    with pytest.raises(CommandRejected, match='^operation_expired$'):
        run(client, until=text(ahead(seconds=20)))
    broken = job()
    broken['person'] = {'user': '43'}
    with pytest.raises(CommandRejected, match='^invalid_parameters$'):
        asyncio.run(execute(client, broken))
    assert client.calls == []


# spec: ANYM-935
def test_get_member_state_reads_the_state_role_and_a_channels_discussion_group():
    end = ahead(days=5)
    client = Client(channel(creator=True, megagroup=False, broadcast=True), states=[banned(end)], linked=555)
    result = run(client, 'get_member_state')
    assert result['outcome'] == 'confirmed'
    data = result['data']
    assert (data['state'], data['banned_until'], data['chat_kind'], data['linked_chat']) == (
        'banned', text(end), 'channel', '-1000000000555')
    assert bans(client) == []
    owner = Client(channel(creator=True), states=[member()])
    data = run(owner, 'get_member_state', verified_self=types.User(id=USER, is_self=True))['data']
    assert (data['state'], data['role'], data['chat_kind'], data['linked_chat']) == (
        'member', 'owner', 'supergroup', None)


# spec: ANYM-935
def test_remove_participant_still_refuses_a_supergroup_so_no_ban_is_hidden_in_it():
    client = Client(channel(creator=True))
    remove = {'operation': 'remove_participant', 'provider_chat': str(CHANNEL), 'expires_at': time.time() + 60,
              'parameters': {'user_id': str(USER)},
              '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}}
    with pytest.raises(CommandRejected, match='^unsupported_operation$'):
        asyncio.run(execute(client, remove))
    assert bans(client) == [] and not any(name.endswith('Request') for name in names(client))
