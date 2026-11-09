"""ANYM-842: every messaging process names its database sessions after its service, so pg_stat_activity and lock
samples say which service holds a session. Synthetic configuration only; the disposable test database."""
import importlib
import json
from pathlib import Path

import psycopg
import pytest

from messaging_service import store as store_module
from messaging_service.store import Store, name_sessions

ROOT = Path(__file__).resolve().parents[1]


def own_name(connection):
    return connection.execute('SELECT application_name FROM pg_stat_activity WHERE pid=pg_backend_pid()').fetchone()


@pytest.fixture
def no_name(monkeypatch):
    # Recorded first, so the name a test sets is removed again afterwards.
    monkeypatch.setenv('PGAPPNAME', 'placeholder')
    monkeypatch.delenv('PGAPPNAME')
    return monkeypatch


# spec: ANYM-842
def test_every_store_and_direct_connection_of_a_named_process_carries_the_service_name(database, tmp_path, no_name):
    assert name_sessions('matcher') == 'matcher'
    store = Store(database.pool.conninfo, tmp_path, 'http://testserver')
    try:
        with store.pool.connection() as c:
            assert own_name(c)['application_name'] == 'matcher'
    finally:
        store.close()
    with psycopg.connect(database.pool.conninfo) as c:
        assert own_name(c)[0] == 'matcher'


# spec: ANYM-842
def test_a_name_the_environment_already_gives_wins_and_names_are_bounded(database, no_name):
    # The promotion tool runs steps with PGAPPNAME=deedkit-migration-<step> and finds the session by it.
    no_name.setenv('PGAPPNAME', 'deedkit-migration-synthetic')
    assert name_sessions('api') == 'deedkit-migration-synthetic'
    with psycopg.connect(database.pool.conninfo) as c:
        assert own_name(c)[0] == 'deedkit-migration-synthetic'
    no_name.delenv('PGAPPNAME')
    assert name_sessions('Enricher Audio_Image') == 'enricher-audio-image'
    no_name.delenv('PGAPPNAME')
    assert name_sessions('enricher-' + 'x' * 80) == 'enricher-' + 'x' * 54
    for bad in ('', '---', '9lives'):
        no_name.delenv('PGAPPNAME', raising=False)
        with pytest.raises(ValueError, match='invalid_session_name'):
            name_sessions(bad)


class Named(Exception):
    pass


ENTRYPOINTS = [
    ('telegram_collector', [], 'collector-telegram'),
    ('whatsapp_collector', [], 'collector-whatsapp'),
    ('subscription_worker', [], 'matcher'),
    ('delivery_worker', [], 'delivery'),
    ('file_index', [], 'indexer'),
    ('file_lifecycle', [], 'file-lifecycle'),
    ('cold_archive', [], 'cold-archive'),
    ('message_purge', [], 'message-purge'),
    ('file_understanding', [], 'file-understanding'),
    ('backup_worker', [], 'backup'),
]


# spec: ANYM-842
@pytest.mark.parametrize('module,argv,expected', ENTRYPOINTS, ids=[entry[0] for entry in ENTRYPOINTS])
def test_each_service_names_its_sessions_before_it_reads_arguments_or_connects(module, argv, expected, monkeypatch):
    def named(service):
        raise Named(service)
    monkeypatch.setattr(store_module, 'name_sessions', named)
    monkeypatch.setattr('sys.argv', ['synthetic', *argv])
    with pytest.raises(Named) as raised:
        importlib.import_module('messaging_service.' + module).main()
    assert raised.value.args == (expected,)


# spec: ANYM-842
@pytest.mark.parametrize('release,expected', [(None, 'api'), ('synthetic-generation', 'http-api')])
def test_the_api_names_its_sessions_and_the_http_plan_generation_is_http_api(release, expected, monkeypatch):
    entry = importlib.import_module('messaging_service.__main__')
    def named(service):
        raise Named(service)
    monkeypatch.setattr(entry, 'name_sessions', named)
    monkeypatch.setattr('sys.argv', ['messaging_service', 'serve'])
    if release:
        monkeypatch.setenv('DEEDKIT_RELEASE_ID', release)
    else:
        monkeypatch.delenv('DEEDKIT_RELEASE_ID', raising=False)
    with pytest.raises(Named) as raised:
        entry.main()
    assert raised.value.args == (expected,)


# spec: ANYM-842
def test_an_enricher_names_its_sessions_after_its_profiles(monkeypatch, tmp_path):
    from messaging_service import enrichment
    config = json.loads((ROOT / 'deploy/enrichment-mini.example.json').read_text())
    path = tmp_path / 'enrichment.json'
    path.write_text(json.dumps(config))
    def named(service):
        raise Named(service)
    monkeypatch.setattr(store_module, 'name_sessions', named)
    monkeypatch.setattr(enrichment, 'configuration', lambda _: config)
    monkeypatch.setattr('sys.argv', ['synthetic', '--config', str(path)])
    with pytest.raises(Named) as raised:
        enrichment.main()
    assert raised.value.args == ('enricher-' + '-'.join(sorted(config['profiles'])),)
