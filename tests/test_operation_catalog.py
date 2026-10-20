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


def test_registered_file_requires_digest_and_has_no_path_or_url():
    value = validate_operation('telegram', 'send_file', {
        'attachment_id': 'att_' + 'a' * 32, 'sha256': 'b' * 64,
    })
    assert value['mode'] == 'document' and value['caption'] == ''
    for extra in ['path', 'url', 'registered_file_path', 'trusted_blob_directory']:
        with pytest.raises(ValueError, match='invalid_operation_parameters'):
            validate_operation('telegram', 'send_file', {**value, extra: '/private/file'})


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
