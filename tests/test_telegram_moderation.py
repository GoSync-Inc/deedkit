"""delete_member_messages on Telegram (ANYM-934): synthetic Telethon objects only; never a provider session."""
import asyncio
import time

import pytest
from telethon import errors, types

from messaging_service.commands import CommandRejected
from messaging_service.telegram_governor import ProviderRateLimited
from messaging_service.telegram_operations import execute

CHANNEL = -1001234567890
BASIC = -4242
IDS = ['msg_' + str(n) * 32 for n in (1, 2, 3)]


def rights(**granted):
    return types.ChatAdminRights(**{'delete_messages': False, **granted})


def channel(**fields):
    return types.Channel(id=1234567890, title='Synthetic channel', photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=1, **fields)


def basic_group(**fields):
    return types.Chat(id=4242, title='Synthetic basic group', photo=types.ChatPhotoEmpty(), participants_count=3,
                      date=None, version=1, **fields)


class Client:
    """Records every call; `found` is what the read-back returns, `raises` what a named call raises."""
    def __init__(self, entity, found=None, raises=None):
        self.entity, self.found, self.raises, self.calls = entity, found, raises or {}, []

    async def get_input_entity(self, peer):
        self.calls.append(('get_input_entity', peer))
        if peer > 0:
            return types.InputPeerUser(peer, 1)
        return types.InputPeerChannel(-peer - 1000000000000, 1) if peer <= -1000000000000 else types.InputPeerChat(-peer)

    async def get_entity(self, peer):
        self.calls.append(('get_entity', peer))
        if 'get_entity' in self.raises:
            raise self.raises['get_entity']
        return self.entity

    async def delete_messages(self, peer, ids, **kwargs):
        self.calls.append(('delete_messages', peer, ids, kwargs))
        if 'delete_messages' in self.raises:
            raise self.raises['delete_messages']
        return [types.messages.AffectedMessages(pts=1, pts_count=len(ids))]

    async def get_messages(self, peer, *, ids):
        self.calls.append(('get_messages', peer, ids))
        if 'get_messages' in self.raises:
            raise self.raises['get_messages']
        return self.found if self.found is not None else [None] * len(ids)


def job(chat=CHANNEL, count=3):
    return {'operation': 'delete_member_messages', 'provider_chat': str(chat), 'expires_at': time.time() + 60,
            'parameters': {'message_ids': IDS[:count], 'expected_revisions': [1] * count},
            'targets': [{'message_id': IDS[n], 'revision': 1, 'provider_message_id': str(101 + n)}
                        for n in range(count)],
            '_policy': {'enabled': True, 'discover_chats': True, 'include_chats': [], 'exclude_chats': []}}


def run(client, chat=CHANNEL, count=3):
    return asyncio.run(execute(client, job(chat, count)))


def deletes(client):
    return [call for call in client.calls if call[0] == 'delete_messages']


def states(result):
    return [row['state'] for row in result['data']['results']]


# spec: ANYM-934
@pytest.mark.parametrize('entity,chat', [
    (channel(creator=True, megagroup=True), CHANNEL),
    (channel(admin_rights=rights(delete_messages=True), megagroup=True), CHANNEL),
    (channel(admin_rights=rights(delete_messages=True), broadcast=True), CHANNEL),
    (basic_group(creator=True), BASIC),
    (basic_group(admin_rights=rights()), BASIC),
])
def test_creator_or_admin_with_the_delete_right_deletes_the_listed_ids_in_one_revoking_request(entity, chat):
    client = Client(entity)
    result = run(client, chat)
    assert [(ids, kwargs) for _, _, ids, kwargs in deletes(client)] == [([101, 102, 103], {'revoke': True})]
    assert result['outcome'] == 'confirmed' and states(result) == ['deleted'] * 3
    assert result['data']['deleted'] == 3 and result['data']['all_deleted'] is True
    assert [row['message_id'] for row in result['data']['results']] == IDS


# spec: ANYM-934
@pytest.mark.parametrize('entity,chat,code,evidence', [
    (channel(admin_rights=rights(ban_users=True), megagroup=True), CHANNEL, 'missing_admin_right',
     {'missing_right': 'delete_messages'}),
    (channel(admin_rights=rights(post_messages=True), broadcast=True), CHANNEL, 'missing_admin_right',
     {'missing_right': 'delete_messages'}),
    (channel(megagroup=True), CHANNEL, 'not_admin', None),
    (channel(broadcast=True), CHANNEL, 'not_admin', None),
    (basic_group(), BASIC, 'not_admin', None),
    (types.ChannelForbidden(id=1234567890, access_hash=1, title='Gone'), CHANNEL, 'not_admin', None),
])
def test_a_missing_right_or_no_admin_role_refuses_before_any_delete(entity, chat, code, evidence):
    client = Client(entity)
    with pytest.raises(CommandRejected) as refused:
        run(client, chat)
    assert str(refused.value) == code
    assert refused.value.data == ({'evidence': evidence} if evidence else None)
    assert not deletes(client) and not [call for call in client.calls if call[0] == 'get_messages']


# spec: ANYM-934
def test_each_message_is_settled_by_the_read_back_and_none_is_retried():
    still_there = types.Message(id=102, peer_id=types.PeerChannel(1234567890), message='x', date=None)
    client = Client(channel(creator=True, megagroup=True),
                    found=[None, still_there, types.MessageEmpty(id=103, peer_id=None)])
    result = run(client)
    assert states(result) == ['deleted', 'failed', 'deleted'] and result['outcome'] == 'confirmed'
    assert result['data']['results'][1]['evidence'] == {'basis': 'provider_read_back', 'message_present': True}
    assert len(deletes(client)) == 1 and result['data']['all_deleted'] is False
    # A read-back that names some other message decides nothing for that row.
    other = types.Message(id=999, peer_id=types.PeerChannel(1234567890), message='x', date=None)
    client = Client(channel(creator=True, megagroup=True), found=[None, other, None])
    result = run(client)
    assert states(result) == ['deleted', 'unknown', 'deleted'] and result['outcome'] == 'unknown'


# spec: ANYM-934
def test_a_flood_wait_before_the_request_is_failed_and_after_it_unknown_with_retry_after():
    before = Client(channel(creator=True, megagroup=True), raises={'get_entity': ProviderRateLimited(30)})
    result = run(before)
    assert not deletes(before) and states(result) == ['failed'] * 3 and result['outcome'] == 'confirmed'
    assert all(row['evidence']['retry_after_s'] == 30 for row in result['data']['results'])
    sent = Client(channel(creator=True, megagroup=True), raises={'delete_messages': ProviderRateLimited(45)})
    result = run(sent)
    assert len(deletes(sent)) == 1 and states(result) == ['unknown'] * 3 and result['outcome'] == 'unknown'
    assert all(row['evidence']['retry_after_s'] == 45 for row in result['data']['results'])
    read = Client(channel(creator=True, megagroup=True), raises={'get_messages': ProviderRateLimited(12)})
    result = run(read)
    assert states(result) == ['unknown'] * 3 and result['data']['results'][0]['evidence']['retry_after_s'] == 12


# spec: ANYM-934
def test_a_refused_request_is_failed_and_any_other_error_after_sending_is_unknown():
    refused = Client(channel(creator=True, megagroup=True),
                     raises={'delete_messages': errors.MessageDeleteForbiddenError(request=None)})
    result = run(refused)
    assert states(result) == ['failed'] * 3 and result['data']['results'][0]['provider_code'] == 'MESSAGE_DELETE_FORBIDDEN'
    broken = Client(channel(creator=True, megagroup=True), raises={'delete_messages': ConnectionError('wire')})
    assert states(run(broken)) == ['unknown'] * 3
    unread = Client(channel(creator=True, megagroup=True), raises={'get_messages': ConnectionError('wire')})
    result = run(unread)
    assert states(result) == ['unknown'] * 3 and 'wire' not in str(result)


# spec: ANYM-934
def test_malformed_targets_or_a_private_chat_refuse_without_a_provider_call():
    client = Client(channel(creator=True, megagroup=True))
    for targets in ([], [{'message_id': IDS[0], 'provider_message_id': 'x'}],
                    [{'message_id': IDS[0], 'provider_message_id': '7'}] * 2,
                    [{'message_id': IDS[0], 'provider_message_id': str(2**31)}]):
        with pytest.raises(CommandRejected, match='invalid_parameters'):
            asyncio.run(execute(client, {**job(), 'targets': targets}))
    assert client.calls == []
    with pytest.raises(CommandRejected, match='unsupported_operation'):
        asyncio.run(execute(Client(types.User(id=42)), job(chat=42)))
