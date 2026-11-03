"""ANYM-646: list_chats and search_people take the singular `account` beside `accounts`, as search does."""
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from test_archive import event

FIRST = 'alias-first'
SECOND = 'alias-second'
BOTH = [FIRST, SECOND]


@pytest.fixture
def alias(seeded):
    """Two accounts with several chats and named senders; a key for both and a key for the first only."""
    s, *_ = seeded
    archive = Archive(s)
    for account in BOTH:
        archive.register(account, 'telegram', 'Synthetic '+account, discover=True)
    for account, chat, sender, name in [(FIRST, '1', '41', 'Anna Alias'), (FIRST, '2', '42', 'Anna Alias Two'),
                                        (SECOND, '3', '43', 'Anna Alias Three'), (SECOND, '4', '44', 'Anna Alias Four')]:
        archive.ingest(event(account=account, chat=chat, sender=sender, provider_id='1',
                             sender_profile={'display_name': name, 'username': 'alias_'+sender}))
    return s, s.issue_key('alias-both', BOTH), s.issue_key('alias-first-only', [FIRST])


def listing(s, key, tool, **filters):
    if tool == 'list_chats':
        return s.list_chats(key, **filters)
    return s.search_people(key, 'Anna', **filters)


def paged(s, key, tool, **filters):
    """Every page of a one-row listing with its cursor, so rows, order and cursors compare in full."""
    pages, after = [], ''
    while True:
        page = listing(s, key, tool, after=after, limit=1, **filters)
        pages.append(page)
        after = page['next_cursor']
        if not after or len(pages) > 10:
            return pages


# spec: ANYM-646
@pytest.mark.parametrize('tool', ['list_chats', 'search_people'])
def test_account_alone_returns_the_same_pages_as_accounts_with_that_one_id(alias, tool):
    s, key, _ = alias
    for name in BOTH:
        pages = paged(s, key, tool, account=name)
        assert pages == paged(s, key, tool, accounts=[name])
        assert len(pages) == 2 and all(page['items'] for page in pages[:1])
    assert paged(s, key, tool, account=FIRST) != paged(s, key, tool, account=SECOND)


# spec: ANYM-646
@pytest.mark.parametrize('tool', ['list_chats', 'search_people'])
def test_a_cursor_from_one_form_continues_with_the_other(alias, tool):
    s, key, _ = alias
    singular = listing(s, key, tool, account=FIRST, limit=1)
    plural = listing(s, key, tool, accounts=[FIRST], limit=1)
    assert singular['next_cursor'] and singular == plural
    assert (listing(s, key, tool, accounts=[FIRST], after=singular['next_cursor'], limit=1)
            == listing(s, key, tool, account=FIRST, after=singular['next_cursor'], limit=1))
    assert listing(s, key, tool, accounts=[FIRST], after=singular['next_cursor'], limit=1)['items']


# spec: ANYM-646
@pytest.mark.parametrize('tool', ['list_chats', 'search_people'])
def test_both_fields_are_anded_and_an_empty_account_is_no_filter(alias, tool):
    s, key, _ = alias
    assert listing(s, key, tool, account=SECOND, accounts=[FIRST])['items'] == []
    assert paged(s, key, tool, account=FIRST, accounts=BOTH) == paged(s, key, tool, accounts=[FIRST])
    assert listing(s, key, tool, account='') == listing(s, key, tool)
    assert listing(s, key, tool, account='', accounts=[FIRST]) == listing(s, key, tool, accounts=[FIRST])


# spec: ANYM-646
@pytest.mark.parametrize('tool', ['list_chats', 'search_people'])
def test_an_account_the_key_cannot_see_behaves_as_it_does_in_accounts(alias, tool):
    s, _, first_only = alias
    assert listing(s, first_only, tool, account=SECOND) == listing(s, first_only, tool, accounts=[SECOND])
    assert listing(s, first_only, tool, account='nobody') == listing(s, first_only, tool, accounts=['nobody'])
    assert listing(s, first_only, tool, account=SECOND)['items'] == []


# spec: ANYM-646
@pytest.mark.parametrize('tool', ['list_chats', 'search_people'])
def test_rest_and_mcp_accept_account_and_name_both_fields(alias, tool):
    s, key, _ = alias
    auth = {'Authorization': 'Bearer '+key, 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25'}
    with TestClient(create_app(s, ['testserver'])) as client:
        rest = client.post('/v1/'+tool, headers=auth, json={'account': FIRST})
        plural = client.post('/v1/'+tool, headers=auth, json={'accounts': [FIRST]})
        assert rest.status_code == 200 and rest.json() == plural.json() and rest.json()['items']
        bad = client.post('/v1/'+tool, headers=auth, json={'account': 7})
        assert bad.status_code >= 400 and bad.json()['error'] != 'unknown_argument'
        mcp = client.post('/mcp', headers=auth, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': {'account': FIRST}}}).json()['result']
        assert not mcp.get('isError')
        listed = client.post('/mcp', headers=auth, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).json()
        row, = [t for t in listed['result']['tools'] if t['name'] == tool]
        assert 'account' in row['inputSchema']['properties']
        # Both fields are named (list_chats keeps a tight description budget: account/accounts).
        assert 'account/accounts' in row['description'] or 'account (one ID)' in row['description']
