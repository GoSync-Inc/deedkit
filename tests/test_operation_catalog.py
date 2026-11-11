import pytest

from messaging_service.messenger_operations import operation_manifest, operation_spec, validate_operation


def test_catalog_keeps_archive_read_separate_from_side_effects():
    assert operation_spec('mark_read')['permission'] == 'read-state'
    assert operation_spec('mark_read')['mutation'] is True
    assert operation_spec('rename_group')['permission'] == 'groups-manage'
    assert operation_spec('list_contacts')['full_account'] is True
    assert operation_spec('chat_info')['full_account'] is False
    assert operation_spec('update_profile')['permission'] == 'profile-manage'


def test_forward_uses_archive_evidence_not_caller_provider_ids():
    assert validate_operation('telegram', 'forward_message', {
        'source_message_id': 'msg_' + 'a' * 32, 'source_revision': 2,
    }) == {'source_message_id': 'msg_' + 'a' * 32, 'source_revision': 2}
    with pytest.raises(ValueError, match='invalid_operation_parameters'):
        validate_operation('telegram', 'forward_message', {
            'source_provider_chat': '123', 'source_provider_message': '456',
        })


def test_defaults_and_strict_bounded_inputs():
    assert validate_operation('whatsapp', 'list_participants', {}) == {'limit': 50, 'offset': 0}
    for params in [{'limit': True}, {'limit': 101}, {'offset': -1}, {'unexpected': 1}]:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation('whatsapp', 'list_participants', params)
    with pytest.raises(ValueError, match='invalid_operation_parameters'):
        validate_operation('telegram', 'send_poll', {
            'question': 'Synthetic?', 'options': ['one', 'one'], 'multiple': False,
        })


# spec: ANYM-755
def test_edit_message_takes_the_send_format_options_and_omits_their_defaults():
    edit = {'text': 'Edited synthetic text'}
    for platform in ('telegram', 'whatsapp'):
        schema = {r['operation']: r for r in operation_manifest(platform)}['edit_message']
        assert set(schema['parameters_schema']) == {'text', 'format', 'link_preview', 'allow_lossy'}
        assert 'plain text only' not in schema['limitation']
        # defaults, given or not, persist exactly the parameters of today's plain edit
        assert validate_operation(platform, 'edit_message', edit) == edit
        assert validate_operation(platform, 'edit_message', {**edit, 'format': 'plain', 'link_preview': False,
                                                              'allow_lossy': False}) == edit
    assert validate_operation('telegram', 'edit_message', {**edit, 'format': 'html', 'link_preview': True}) == {
        **edit, 'format': 'html', 'link_preview': True}
    for bad in [{'format': 'rich'}, {'format': 1}, {'link_preview': 'yes'}, {'allow_lossy': 1}]:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation('telegram', 'edit_message', {**edit, **bad})


def test_registered_file_requires_digest_and_has_no_path_or_url():
    value = validate_operation('telegram', 'send_file', {
        'attachment_id': 'att_' + 'a' * 32, 'sha256': 'b' * 64,
    })
    assert value['mode'] == 'document' and value['caption'] == ''
    for extra in ['path', 'url', 'registered_file_path', 'trusted_blob_directory']:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation('telegram', 'send_file', {**value, extra: '/private/file'})


# spec: ANYM-927
def test_send_file_takes_silent_on_telegram_and_whatsapp_refuses_it_by_name():
    base = {'attachment_id': 'att_' + 'a' * 32, 'sha256': 'b' * 64}
    assert validate_operation('telegram', 'send_file', {**base, 'silent': True})['silent'] is True
    # An explicit false is the loud send of before, with the same stored parameters and digest.
    assert validate_operation('telegram', 'send_file', {**base, 'silent': False}) == validate_operation(
        'telegram', 'send_file', base)
    with pytest.raises(ValueError, match='invalid_operation_parameters'):
        validate_operation('telegram', 'send_file', {**base, 'silent': 'true'})
    with pytest.raises(ValueError, match='silent_unsupported') as refused:
        validate_operation('whatsapp', 'send_file', {**base, 'silent': True})
    assert [e['field'] for e in refused.value.field_errors] == ['silent']
    rows = {r['operation']: r for r in operation_manifest('telegram')}
    assert rows['send_file']['parameters_schema']['silent']['type'] == 'boolean'


def test_provider_unsupported_functions_fail_before_any_dispatch():
    rows = {r['operation']: r for r in operation_manifest('whatsapp')}
    assert rows['delete_message']['supported'] is False
    assert 'fallback' in rows['delete_message']['limitation']
    with pytest.raises(ValueError, match='unsupported_operation'):
        validate_operation('whatsapp', 'delete_message', {})
    with pytest.raises(ValueError, match='unsupported_operation'):
        validate_operation('telegram', '__raw_api__', {})


def test_contact_ids_are_platform_specific_and_no_coercion():
    assert validate_operation('telegram', 'get_contact', {'user_id': '123'}) == {'user_id': '123'}
    assert validate_operation('whatsapp', 'get_contact', {'user_id': '123@lid'}) == {'user_id': '123@lid'}
    for platform, value in [('telegram', '123@c.us'), ('whatsapp', '123'), ('telegram', 123)]:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation(platform, 'get_contact', {'user_id': value})


def test_location_and_profile_validate_without_raw_payload():
    value = validate_operation('telegram', 'send_location', {'latitude': -8.6, 'longitude': 115.2})
    assert value['description'] == ''
    with pytest.raises(ValueError, match='invalid_operation_parameters'):
        validate_operation('telegram', 'send_location', {'latitude': float('nan'), 'longitude': 1})
    for platform, params in [('telegram', {'status': 'hello'}), ('whatsapp', {'bio': 'hello'})]:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation(platform, 'update_profile', params)


def test_account_lifecycle_auth_payments_and_calls_are_not_agent_operations():
    for name in ['delete_account', 'export_session', 'sign_out', 'pay', 'start_call']:
        with pytest.raises(ValueError, match='unsupported_operation'):
            operation_spec(name)


def test_platform_specific_pin_and_label_removal():
    assert validate_operation('telegram','pin_message',{})=={}
    assert validate_operation('whatsapp','pin_message',{})=={'seconds':86400}
    assert validate_operation('whatsapp','set_chat_labels',{'label_ids':[]})=={'label_ids':[]}
    assert operation_spec('send_contact')['full_account']
    assert validate_operation('whatsapp','add_contact',{'phone':'+123456789','first_name':'Synthetic'})['last_name']==''


def test_parameter_formats_described():
    from messaging_service.messenger_operations import OPERATIONS, _fields, parameter_format
    plain = {'string', 'integer', 'number', 'boolean'}
    for platform, provider in [('telegram', None), ('whatsapp', None), ('whatsapp', 'whatsmeow')]:
        for row in operation_manifest(platform, provider=provider):
            reviewed = _fields(platform, row['operation'], OPERATIONS[row['operation']])
            for name, descriptor in row['parameters_schema'].items():
                # Metadata is added; reviewed keys, types and limits stay as they were.
                assert {k: v for k, v in descriptor.items() if k != 'format'} == reviewed[name]
                if descriptor['type'] in plain: assert 'format' not in descriptor
                else: assert descriptor['format'] and descriptor['format'] == parameter_format(platform, descriptor['type'])
    rows = {r['operation']: r for r in operation_manifest('telegram')}
    assert 'decimal' in rows['create_group']['parameters_schema']['user_ids']['format']
    assert 'person_' in rows['get_contact']['parameters_schema']['user_id']['format']
    whatsapp = {r['operation']: r for r in operation_manifest('whatsapp')}
    assert '@c.us' in whatsapp['get_contact']['parameters_schema']['user_id']['format']
    assert 'format' not in OPERATIONS['create_group']['fields']['user_ids']


GROUP_ADMIN = ['set_group_messages_admins_only', 'set_group_info_admins_only', 'set_slow_mode', 'set_join_approval',
               'list_join_requests', 'approve_join_requests', 'reject_join_requests']


# spec: ANYM-242
def test_group_admin_operations_validate_parameters():
    from messaging_service.messenger_operations import ParameterError
    def errors(platform, operation, parameters):
        with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
            validate_operation(platform, operation, parameters, provider='whatsmeow')
        return failure.value.field_errors
    assert errors('telegram', 'set_slow_mode', {'seconds': 45}) == [
        {'field': 'seconds', 'reason': 'enum', 'allowed': [0, 10, 30, 60, 300, 900, 3600]}]
    assert validate_operation('telegram', 'set_slow_mode', {'seconds': 0}) == {'seconds': 0}
    assert errors('telegram', 'set_slow_mode', {'seconds': True})[0]['reason'] == 'type'
    ids = [str(100 + i) for i in range(21)]
    assert errors('telegram', 'approve_join_requests', {'user_ids': ids}) == [
        {'field': 'user_ids', 'reason': 'max_items', 'expected': 'at most 20 items'}]
    assert errors('whatsapp', 'reject_join_requests', {'user_ids': ['1@lid', '1@lid']})[0]['reason'] == 'unique'
    assert errors('telegram', 'approve_join_requests', {'user_ids': []})[0]['reason'] == 'min_items'
    assert errors('telegram', 'set_group_messages_admins_only', {'enabled': True, 'rights': {'send_messages': True}}) == [
        {'field': 'rights', 'reason': 'unknown_field', 'expected': 'only enabled'}]
    assert errors('whatsapp', 'set_join_approval', {'enabled': 'yes'})[0] == {'field': 'enabled', 'reason': 'type', 'expected': 'boolean'}
    assert errors('telegram', 'set_group_info_admins_only', {})[0]['reason'] == 'required'
    assert validate_operation('whatsapp', 'list_join_requests', {}, provider='whatsmeow') == {'limit': 50}
    assert errors('whatsapp', 'list_join_requests', {'limit': 101})[0]['reason'] == 'maximum'
    for name in GROUP_ADMIN:
        spec = operation_spec(name)
        assert spec['target'] == 'chat' and not spec['full_account']
        assert spec['permission'] == ('groups-read' if name == 'list_join_requests' else 'groups-manage')
        assert spec['mutation'] is (name != 'list_join_requests')


# spec: ANYM-242
def test_group_admin_catalog_support():
    telegram = {r['operation']: r for r in operation_manifest('telegram')}
    native = {r['operation']: r for r in operation_manifest('whatsapp', provider='whatsmeow')}
    browser = {r['operation']: r for r in operation_manifest('whatsapp', provider='webjs')}
    assert not telegram['set_join_approval']['supported'] and not native['set_slow_mode']['supported']
    assert all(telegram[n]['supported'] for n in GROUP_ADMIN if n != 'set_join_approval')
    assert all(native[n]['supported'] for n in GROUP_ADMIN if n != 'set_slow_mode')
    assert not any(browser[n]['supported'] for n in GROUP_ADMIN)
    assert 'native' in browser['set_join_approval']['limitation']
    for name in GROUP_ADMIN:
        with pytest.raises(ValueError, match='unsupported_operation'):
            validate_operation('whatsapp', name, {'enabled': True}, provider='webjs')
    assert 'overwritten' in telegram['set_group_messages_admins_only']['limitation']
    assert telegram['set_slow_mode']['parameters_schema']['seconds']['format'].startswith('one of 0, 10')


MEMBER_IDS = ['msg_%032x' % n for n in range(1, 22)]


# spec: ANYM-934
def test_delete_member_messages_is_a_telegram_mutation_behind_a_permission_no_key_holds():
    from messaging_service.policy import LEGACY_PERMISSIONS, PERMISSIONS, effective_permissions
    spec = operation_spec('delete_member_messages')
    assert (spec['permission'], spec['target'], spec['mutation'], spec['full_account'], spec['platforms']) == (
        'groups-moderate', 'chat', True, False, ['telegram'])
    assert 'groups-moderate' in PERMISSIONS and 'groups-moderate' not in LEGACY_PERMISSIONS
    assert effective_permissions(['read', 'groups-moderate'], None) == ['read']  # a NULL key ceiling never inherits it
    telegram = {r['operation']: r for r in operation_manifest('telegram')}['delete_member_messages']
    assert telegram['supported'] and set(telegram['parameters_schema']) == {'message_ids', 'expected_revisions'}
    assert 'delete_message' in telegram['parameters_schema']['message_ids']['format']
    assert all(word in telegram['limitation'] for word in ('not_admin', 'missing_admin_right', 'retry_after_s', 'purged'))
    for provider in ('whatsmeow', 'webjs'):
        row = {r['operation']: r for r in operation_manifest('whatsapp', provider=provider)}['delete_member_messages']
        assert not row['supported'] and 'moderation_unsupported' in row['limitation']
        with pytest.raises(ValueError, match='^moderation_unsupported$'):
            validate_operation('whatsapp', 'delete_member_messages', {'message_ids': MEMBER_IDS[:1],
                                                                      'expected_revisions': [1]}, provider=provider)


# spec: ANYM-934
def test_delete_member_messages_takes_1_to_20_unique_ids_with_one_revision_each():
    from messaging_service.messenger_operations import ParameterError
    def errors(parameters):
        with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
            validate_operation('telegram', 'delete_member_messages', parameters)
        return failure.value.field_errors
    ok = {'message_ids': MEMBER_IDS[:20], 'expected_revisions': [1] * 19 + [3]}
    assert validate_operation('telegram', 'delete_member_messages', ok) == ok
    assert errors({'message_ids': MEMBER_IDS, 'expected_revisions': [1] * 21}) == [
        {'field': 'message_ids', 'reason': 'max_items', 'expected': 'at most 20 items'},
        {'field': 'expected_revisions', 'reason': 'max_items', 'expected': 'at most 20 items'}]
    assert errors({'message_ids': [], 'expected_revisions': []})[0] == {
        'field': 'message_ids', 'reason': 'min_items', 'expected': 'at least 1 item'}
    duplicate = errors({'message_ids': [MEMBER_IDS[0], MEMBER_IDS[1], MEMBER_IDS[0]], 'expected_revisions': [1, 1, 1]})
    assert duplicate == [{'field': 'message_ids/2', 'reason': 'unique', 'expected': 'no duplicate items'}]
    assert errors({'message_ids': ['person_1'], 'expected_revisions': [1]})[0]['field'] == 'message_ids/0'
    assert errors({'message_ids': MEMBER_IDS[:2], 'expected_revisions': [1]}) == [
        {'field': 'expected_revisions', 'reason': 'min_items', 'expected': 'one revision per message_ids item'}]
    assert errors({'message_ids': MEMBER_IDS[:1], 'expected_revisions': [0]})[0]['field'] == 'expected_revisions/0'
    assert errors({'message_ids': MEMBER_IDS[:1]})[0] == {'field': 'expected_revisions', 'reason': 'required',
        'expected': operation_manifest_format('expected_revisions')}
    assert errors({**ok, 'revoke': False})[0]['reason'] == 'unknown_field'


def operation_manifest_format(name):
    rows = {r['operation']: r for r in operation_manifest('telegram')}
    return rows['delete_member_messages']['parameters_schema'][name]['format']


def ahead(**delta):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat().replace('+00:00', 'Z')


# spec: ANYM-935
def test_ban_and_unban_are_telegram_mutations_behind_groups_moderate():
    from messaging_service.messenger_operations import ADVANCED_ONLY, OPERATION_TOOLS
    for name in ('ban_member', 'unban_member'):
        spec = operation_spec(name)
        assert (spec['permission'], spec['target'], spec['mutation'], spec['full_account'], spec['platforms']) == (
            'groups-moderate', 'chat', True, False, ['telegram'])
        assert name in OPERATION_TOOLS['manage_chat']
    state = operation_spec('get_member_state')
    assert (state['permission'], state['mutation'], state['platforms']) == ('groups-moderate', False, ['telegram'])
    assert 'get_member_state' in ADVANCED_ONLY
    telegram = {r['operation']: r for r in operation_manifest('telegram')}
    assert set(telegram['ban_member']['parameters_schema']) == {'user', 'until', 'forever', 'message_ids',
                                                                'expected_revisions'}
    assert set(telegram['unban_member']['parameters_schema']) == {'user'}
    assert all(word in telegram['ban_member']['limitation'] for word in (
        'not_admin', 'missing_admin_right', 'target_protected', 'unsupported_chat_type', 'changed', 'unknown'))
    assert 'rejoin' in telegram['unban_member']['limitation']
    for provider in ('whatsmeow', 'webjs'):
        rows = {r['operation']: r for r in operation_manifest('whatsapp', provider=provider)}
        for name in ('ban_member', 'unban_member'):
            assert not rows[name]['supported'] and 'ban_unsupported' in rows[name]['limitation']
            assert 'remove_participant' in rows[name]['limitation']
            with pytest.raises(ValueError, match='^ban_unsupported$'):
                validate_operation('whatsapp', name, {'user': '1234567890@c.us', 'forever': True}, provider=provider)


# spec: ANYM-935
def test_ban_member_takes_until_or_forever_and_optional_messages_of_that_person():
    from messaging_service.messenger_operations import ParameterError
    def errors(parameters):
        with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
            validate_operation('telegram', 'ban_member', parameters)
        return [(e['field'], e['reason']) for e in failure.value.field_errors]
    assert validate_operation('telegram', 'ban_member', {'user': '42', 'forever': True}) == {'user': '42',
                                                                                             'forever': True}
    until = ahead(days=3)
    assert validate_operation('telegram', 'ban_member', {'user': '42', 'until': until.replace('Z', '+00:00')}) == {
        'user': '42', 'until': until}
    assert validate_operation('telegram', 'ban_member', {'user': '42', 'until': ahead(days=365, hours=23)})
    assert errors({'user': '42'}) == [('until', 'required')]
    assert errors({'user': '42', 'until': until, 'forever': True}) == [('forever', 'unique')]
    assert errors({'user': '42', 'forever': False}) == [('forever', 'enum')]
    assert errors({'user': '42', 'until': ahead(seconds=50)}) == [('until', 'minimum')]
    assert errors({'user': '42', 'until': ahead(days=366, minutes=5)}) == [('until', 'maximum')]
    assert errors({'user': '42', 'until': '2026-11-11 10:00'}) == [('until', 'format')]
    assert errors({'user': 'person_1', 'forever': True}) == [('user', 'format')]
    ids = MEMBER_IDS[:2]
    ok = {'user': '42', 'forever': True, 'message_ids': ids, 'expected_revisions': [1, 2]}
    assert validate_operation('telegram', 'ban_member', ok) == ok
    assert errors({'user': '42', 'forever': True, 'message_ids': ids}) == [('expected_revisions', 'required')]
    assert errors({'user': '42', 'forever': True, 'expected_revisions': [1]}) == [('message_ids', 'required')]
    assert errors({**ok, 'expected_revisions': [1]}) == [('expected_revisions', 'min_items')]
    assert errors({**ok, 'message_ids': MEMBER_IDS}) == [('message_ids', 'max_items')]
    assert validate_operation('telegram', 'unban_member', {'user': '42'}) == {'user': '42'}
    with pytest.raises(ParameterError):
        validate_operation('telegram', 'unban_member', {'user': '42', 'forever': True})


# spec: ANYM-932
def test_post_and_delete_story_are_telegram_mutations_behind_stories_post_no_key_holds():
    from messaging_service.messenger_operations import ADVANCED_ONLY, OPERATION_TOOLS, OPT_IN_AS
    from messaging_service.policy import LEGACY_PERMISSIONS, MESSENGER_PERMISSIONS, PERMISSIONS
    for name in ('post_story', 'delete_story'):
        spec = operation_spec(name)
        assert (spec['permission'], spec['target'], spec['mutation'], spec['full_account'], spec['platforms']) == (
            'stories-post', 'account', True, True, ['telegram'])
        assert name in ADVANCED_ONLY and not any(name in names for names in OPERATION_TOOLS.values())
    limits, own = operation_spec('get_story_limits'), operation_spec('list_own_stories')
    assert (limits['permission'], limits['mutation'], own['permission'], own['mutation']) == (
        'stories-post', False, 'read-live', False)
    assert OPT_IN_AS == {'list_own_stories': 'post_story', 'get_story_limits': 'post_story'}
    # A new permission: no default or legacy ceiling and no existing messenger family carries it.
    assert 'stories-post' in PERMISSIONS and 'stories-post' not in LEGACY_PERMISSIONS | MESSENGER_PERMISSIONS
    telegram = {r['operation']: r for r in operation_manifest('telegram')}
    assert set(telegram['post_story']['parameters_schema']) == {'attachment_id', 'upload_id', 'sha256', 'caption',
                                                                'audience'}
    assert telegram['post_story']['parameters_schema']['audience']['default'] is None
    assert telegram['post_story']['parameters_schema']['audience']['required'] is True
    assert set(telegram['delete_story']['parameters_schema']) == {'story_id'}
    assert all(word in telegram['post_story']['limitation'] for word in (
        'media_unsuitable', 'stories_limit_reached', 'story_flood_wait', 'premium_required', 'caption_too_long',
        'media_rejected', 'list_own_stories', '24 hours', '60 s'))
    assert 'not_posted_here' in telegram['delete_story']['limitation']
    assert 'already_gone' in telegram['delete_story']['limitation']
    for provider in ('whatsmeow', 'webjs'):
        rows = {r['operation']: r for r in operation_manifest('whatsapp', provider=provider)}
        assert not any(rows[name]['supported'] for name in ('post_story', 'delete_story', 'list_own_stories',
                                                             'get_story_limits'))


# spec: ANYM-932
def test_post_story_takes_one_original_a_bounded_caption_and_a_named_audience():
    from messaging_service.messenger_operations import ParameterError
    def errors(parameters, operation='post_story'):
        with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
            validate_operation('telegram', operation, parameters)
        return [(e['field'], e['reason']) for e in failure.value.field_errors]
    attachment, sha = 'att_' + 'a' * 32, 'b' * 64
    ok = {'attachment_id': attachment, 'sha256': sha, 'audience': 'close_friends'}
    assert validate_operation('telegram', 'post_story', ok) == {**ok, 'caption': ''}
    for audience in ('contacts', 'close_friends', 'everyone'):
        assert validate_operation('telegram', 'post_story', {**ok, 'audience': audience})['audience'] == audience
    upload = {'upload_id': 'up_' + 'c' * 32, 'sha256': sha, 'audience': 'contacts', 'caption': 'Синтетика'}
    assert validate_operation('telegram', 'post_story', upload) == upload
    # No default audience: the owner confirms who sees it.
    assert errors({'attachment_id': attachment, 'sha256': sha}) == [('audience', 'required')]
    assert errors({**ok, 'audience': 'selected'}) == [('audience', 'enum')]
    assert errors({**ok, 'audience': ['contacts']}) == [('audience', 'type')]
    assert errors({'sha256': sha, 'audience': 'contacts'}) == [('attachment_id', 'required')]
    assert errors({**ok, 'upload_id': 'up_' + 'c' * 32}) == [('upload_id', 'unique')]
    assert errors({'attachment_id': attachment, 'audience': 'contacts'}) == [('sha256', 'required')]
    assert validate_operation('telegram', 'post_story', {**ok, 'caption': 'x' * 2048})['caption'] == 'x' * 2048
    assert errors({**ok, 'caption': 'x' * 2049}) == [('caption', 'max_length')]
    assert errors({**ok, 'mode': 'photo'}) == [('mode', 'unknown_field')]
    assert errors({**ok, 'period': 172800}) == [('period', 'unknown_field')]
    assert validate_operation('telegram', 'delete_story', {'story_id': 7}) == {'story_id': 7}
    assert errors({'story_id': 0}, 'delete_story') == [('story_id', 'minimum')]
    assert errors({'story_id': '7'}, 'delete_story') == [('story_id', 'type')]
    assert errors({}, 'delete_story') == [('story_id', 'required')]
    with pytest.raises(ValueError, match='^unsupported_operation$'):
        validate_operation('whatsapp', 'post_story', ok, provider='whatsmeow')


# spec: ANYM-243
def test_chat_link_validation():
    from messaging_service.messenger_operations import ParameterError
    def errors(platform, parameters, operation='check_chat_link'):
        with pytest.raises(ParameterError, match='^invalid_operation_parameters$') as failure:
            validate_operation(platform, operation, parameters, provider='whatsmeow')
        return failure.value.field_errors
    def key(platform, link, operation='check_chat_link'):
        return validate_operation(platform, operation, {'link': link}, provider='whatsmeow')['link']
    assert key('telegram', 'https://t.me/+AbCd_123-xyz') == '+AbCd_123-xyz'
    assert key('telegram', 'https://t.me/joinchat/AbCd1234') == '+AbCd1234'
    assert key('telegram', 'https://t.me/Synthetic_Channel', 'join_chat') == '@synthetic_channel'
    assert key('telegram', '@Synthetic_Channel') == '@synthetic_channel'
    bare = errors('telegram', {'link': 'Synthetic_Channel'})
    assert bare[0]['field'] == 'link' and bare[0]['reason'] == 'format'
    assert 'https://t.me/+CODE' in bare[0]['expected'] and '@NAME' in bare[0]['expected']
    for link in ('https://t.me/+AbCd1234?start=1', 'https://t.me/name_x#frag', 'https://t.me/+short',
                 'https://t.me/1abcde', 'http://t.me/+AbCd1234', 'https://example.com/+AbCd1234'):
        assert errors('telegram', {'link': link})[0]['reason'] == 'format', link
    assert key('whatsapp', 'https://chat.whatsapp.com/AbCd1234EfGh') == 'AbCd1234EfGh'
    assert key('whatsapp', 'AbCd1234EfGh', 'join_chat') == 'AbCd1234EfGh'
    channel = errors('whatsapp', {'link': 'https://whatsapp.com/channel/0029AbCd1234'})
    assert channel[0]['reason'] == 'format' and 'not yet supported' in channel[0]['expected']
    assert errors('whatsapp', {'link': 'https://chat.whatsapp.com/AbCd1234?x=1'})[0]['reason'] == 'format'
    assert errors('whatsapp', {'link': 'AbCd_1234'})[0]['reason'] == 'format'
    assert errors('telegram', {})[0]['reason'] == 'required'
    assert errors('telegram', {'link': 5})[0]['reason'] == 'type'
    assert errors('telegram', {'link': '@name_x', 'chat_id': 'x'})[0] == {
        'field': 'chat_id', 'reason': 'unknown_field', 'expected': 'only link'}


# spec: ANYM-315
def test_poll_vote_is_native_only_on_whatsapp():
    from messaging_service.messenger_operations import NATIVE_UNSUPPORTED
    telegram = {r['operation']: r for r in operation_manifest('telegram')}
    native = {r['operation']: r for r in operation_manifest('whatsapp', provider='whatsmeow')}
    browser = {r['operation']: r for r in operation_manifest('whatsapp', provider='webjs')}
    assert telegram['vote_poll']['supported'] and native['vote_poll']['supported'] and not browser['vote_poll']['supported']
    assert 'native' in browser['vote_poll']['limitation']
    with pytest.raises(ValueError, match='unsupported_operation'):
        validate_operation('whatsapp', 'vote_poll', {'option_indices': [0]}, provider='webjs')
    # The empty selection retracts the owner's vote.
    assert validate_operation('whatsapp', 'vote_poll', {'option_indices': []}, provider='whatsmeow') == {'option_indices': []}
    # A WhatsApp poll archives up to 12 options; a Telegram one keeps its 10 (Astra r1 M2).
    assert validate_operation('whatsapp', 'vote_poll', {'option_indices': [11]}, provider='whatsmeow') == {'option_indices': [11]}
    for platform, index in (('whatsapp', 12), ('telegram', 10)):
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation(platform, 'vote_poll', {'option_indices': [index]},
                               provider='whatsmeow' if platform == 'whatsapp' else None)
    assert NATIVE_UNSUPPORTED == frozenset(('add_contact', 'delete_contact', 'get_labels', 'set_chat_labels'))


# spec: ANYM-243
def test_chat_link_catalog():
    check, join = operation_spec('check_chat_link'), operation_spec('join_chat')
    assert (check['permission'], check['target'], check['full_account'], check['mutation']) == (
        'groups-read', 'account', True, False)
    assert (join['permission'], join['target'], join['full_account'], join['mutation']) == (
        'groups-manage', 'account', True, True)
    telegram = {r['operation']: r for r in operation_manifest('telegram')}
    native = {r['operation']: r for r in operation_manifest('whatsapp', provider='whatsmeow')}
    browser = {r['operation']: r for r in operation_manifest('whatsapp', provider='webjs')}
    for name in ('check_chat_link', 'join_chat'):
        assert telegram[name]['supported'] and native[name]['supported'] and not browser[name]['supported']
        assert 'native' in browser[name]['limitation']
        with pytest.raises(ValueError, match='unsupported_operation'):
            validate_operation('whatsapp', name, {'link': 'AbCd1234EfGh'}, provider='webjs')
    assert 'check_chat_link' in telegram['join_chat']['limitation']
