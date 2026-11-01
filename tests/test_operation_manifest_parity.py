"""The operation tables of Python, Go and JS describe one set of operations; today's differences are pinned.

Python (messenger_operations.OPERATIONS and telegram_operations.PARAMETERS) is the validator; the native
Go adapter (operationFields) and the browser fallback (SPECS) carry their own copies of the field names. Each
copy is read as source text, so no provider is built or started. A difference not listed in an EXPECTED_*
constant below fails the test, and so does a listed difference that no longer exists: whoever removes
a drift also removes its line. Resolving a listed difference changes behaviour and is a defect card, not
a refactor. This module pins the tables so a refactor keeps them equal.
"""
from pathlib import Path
import re

from messaging_service import messenger_operations as ops
from messaging_service import telegram_operations as telegram

ROOT = Path(__file__).resolve().parents[1]
# All non-test sources of each adapter, so moving a table to another file of the same adapter does not fail here.
GO = '\n'.join(p.read_text() for p in sorted((ROOT / 'whatsapp/whatsmeow').glob('*.go')) if not p.name.endswith('_test.go'))
JS = '\n'.join(p.read_text() for p in sorted((ROOT / 'whatsapp').glob('*.mjs')))

# Operations the browser fallback (operations.mjs) lacks: the native adapter alone implements them.
# Mirrors messenger_operations._supported for a provider other than whatsmeow.
EXPECTED_NATIVE_ONLY = {
    'approve_join_requests', 'check_chat_link', 'join_chat', 'list_join_requests', 'open_chat_by_phone',
    'reject_join_requests', 'request_chat_history', 'set_group_info_admins_only',
    'set_group_messages_admins_only', 'set_join_approval'}
# In the Go table, but Python marks them unsupported for whatsmeow (NATIVE_UNSUPPORTED); Go answers
# operation_unsupported for each of them.
EXPECTED_GO_UNSUPPORTED = {'add_contact', 'delete_contact', 'get_labels', 'set_chat_labels'}
# In the JS table, but unsupported for the browser fallback: authorize() rejects delete_message
# (operations.mjs: upstream delete(true) can fall back to a local deletion).
EXPECTED_JS_UNSUPPORTED = {'delete_message'}
# Field names the Go and JS tables accept and the Python catalog does not define (Go and JS agree on each).
# Defect-card candidates: delete_message.revoke (Go reads it; the Python spec has no field),
# get_reactions and get_labels paging (the Python spec has none).
EXPECTED_EXTRA_FIELDS = {
    'delete_message': {'revoke'},
    'get_labels': {'limit', 'offset'},
    'get_reactions': {'limit', 'offset'},
}
# Operations whose raw OPERATIONS[name]['fields'] differ from the per-platform fields the manifest publishes
# (_fields overrides them); deriving PARAMETERS from the raw field table would change these four.
EXPECTED_RAW_FIELD_OVERRIDES = {'pin_message', 'request_chat_history', 'send_contact', 'update_profile'}
# Go readOperations lists every non-mutating WhatsApp operation except this local write.
EXPECTED_GO_READ_MISSING = {'open_chat_by_phone'}


def _go_fields():
    match = re.search(r'var operationFields\s*=\s*map\[string\]string\{(.*?)\n\}', GO, re.S)
    assert match, 'operationFields'
    body = match.group(1)
    return {name: set(fields.split()) for name, fields in re.findall(r'"(\w+)":\s*"([^"]*)"', body)}


def _go_set(name):
    match = re.search(r'var %s\s*=\s*wordSet\("([^"]*)"\)' % name, GO)
    assert match, name
    return set(match.group(1).split())


def _js_fields():
    match = re.search(r'const SPECS\s*=\s*\{(.*?)\n\s*\};', JS, re.S)
    assert match, 'SPECS'
    body = match.group(1)
    return {name: set(re.findall(r"'(\w+)'", fields)) for name, fields in re.findall(r'(\w+):\[([^\]]*)\]', body)}


def _js_set(name):
    match = re.search(r'const %s\s*=\s*new Set\(\[(.*?)\]\)' % name, JS, re.S)
    assert match, name
    return set(re.findall(r"'(\w+)'", match.group(1)))


def _python_fields(platform, provider=None):
    """Published field names per operation that the platform's catalog implements."""
    return {row['operation']: set(row['parameters_schema'])
            for row in ops.operation_manifest(platform, provider=provider)
            if platform in ops.OPERATIONS[row['operation']]['platforms']}


def _extra_fields(copy, python):
    return {name: copy[name] - python[name] for name in copy if copy[name] - python[name]}


# spec: ANYM-162
def test_the_parsers_read_every_table():
    # Guards the guard: a source reformat that empties a parser must fail here, not pass the parity tests silently.
    assert len(_go_fields()) >= 50 and len(_js_fields()) >= 40
    assert {'chat_info', 'send_file', 'join_chat'} <= set(_go_fields())
    assert {'chat_info', 'send_file', 'set_chat_labels'} <= set(_js_fields())
    assert {'check_chat_link', 'chat_info'} <= _go_set('readOperations')
    assert {'get_profile', 'add_contact'} <= _js_set('GLOBAL')


# spec: ANYM-162
def test_go_table_names_every_whatsapp_operation_of_the_python_catalog():
    python = _python_fields('whatsapp', 'whatsmeow')
    assert set(_go_fields()) == set(python)
    unsupported = {row['operation'] for row in ops.operation_manifest('whatsapp', provider='whatsmeow')
                   if not row['supported'] and 'whatsapp' in ops.OPERATIONS[row['operation']]['platforms']}
    assert unsupported == EXPECTED_GO_UNSUPPORTED


# spec: ANYM-162
def test_js_table_is_the_go_table_without_the_native_only_operations():
    go, js = _go_fields(), _js_fields()
    assert set(go) - set(js) == EXPECTED_NATIVE_ONLY
    assert set(js) <= set(go)
    browser = _python_fields('whatsapp')
    supported = {row['operation'] for row in ops.operation_manifest('whatsapp') if row['supported']}
    assert set(js) == supported | EXPECTED_JS_UNSUPPORTED
    assert set(js) - supported == EXPECTED_JS_UNSUPPORTED
    assert set(browser) - set(js) == EXPECTED_NATIVE_ONLY


# spec: ANYM-162
def test_go_and_js_accept_the_same_field_names_for_shared_operations():
    go, js = _go_fields(), _js_fields()
    assert {name: (go[name], js[name]) for name in js if go[name] != js[name]} == {}


# spec: ANYM-162
def test_go_and_js_fields_equal_the_python_whatsapp_fields_but_the_listed_extras():
    python = _python_fields('whatsapp', 'whatsmeow')
    go, js = _go_fields(), _js_fields()
    assert _extra_fields(go, python) == EXPECTED_EXTRA_FIELDS
    assert _extra_fields(js, python) == EXPECTED_EXTRA_FIELDS
    # Python never defines a field the other copies lack.
    assert {name: python[name] - copy[name] for copy in (go, js) for name in copy if python[name] - copy[name]} == {}
    # update_profile and send_contact are the per-platform renames: the manifest, not the raw spec, equals Go and JS.
    assert go['update_profile'] == js['update_profile'] == python['update_profile'] == {'name', 'status'}
    assert go['send_contact'] == js['send_contact'] == python['send_contact'] == {'contact_ids'}


# spec: ANYM-162
def test_telegram_parameters_equal_the_python_telegram_fields():
    python = _python_fields('telegram')
    assert set(telegram.PARAMETERS) == set(python)
    assert {name: (telegram.PARAMETERS[name], python[name])
            for name in python if telegram.PARAMETERS[name] != python[name]} == {}
    assert python['update_profile'] == {'first_name', 'last_name', 'bio'}


# spec: ANYM-162
def test_raw_field_table_differs_from_published_fields_only_for_the_listed_overrides():
    # W2-6 would derive PARAMETERS from OPERATIONS[name]['fields']; these four are resolved per platform by _fields.
    differing = {name for name, spec in ops.OPERATIONS.items() if 'telegram' in spec['platforms']
                 and set(spec['fields']) != telegram.PARAMETERS[name]}
    assert differing == EXPECTED_RAW_FIELD_OVERRIDES
    whatsapp = {name for name, spec in ops.OPERATIONS.items() if 'whatsapp' in spec['platforms']
                and set(spec['fields']) != _go_fields()[name]}
    assert whatsapp == (EXPECTED_RAW_FIELD_OVERRIDES & {'send_contact', 'update_profile'}) | set(EXPECTED_EXTRA_FIELDS)


# spec: ANYM-162
def test_go_and_js_classification_sets_follow_the_python_catalog():
    catalog = {name: spec for name, spec in ops.OPERATIONS.items() if 'whatsapp' in spec['platforms']}
    account = {name for name, spec in catalog.items() if spec['target'] == 'account'}
    read = {name for name, spec in catalog.items() if not spec['mutation']}
    go, js = _go_fields(), _js_fields()
    assert _go_set('globalOperations') == account & set(go)
    assert _js_set('GLOBAL') == account & set(js)
    assert _go_set('readOperations') == (read & set(go)) - EXPECTED_GO_READ_MISSING
    assert _js_set('READ') == read & set(js)
