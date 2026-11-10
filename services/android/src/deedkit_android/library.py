"""Immutable app definitions and optimistic heads in the existing SQLite store."""

import hashlib
import re
import time

from .config import Refused


class Library:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS library_versions (
                owner TEXT, kind TEXT, name TEXT, version TEXT, source TEXT, created REAL,
                PRIMARY KEY(owner,kind,name,version));
            CREATE TABLE IF NOT EXISTS library_heads (
                owner TEXT, kind TEXT, name TEXT, published TEXT, draft TEXT,
                PRIMARY KEY(owner,kind,name));
            CREATE TABLE IF NOT EXISTS library_events (
                id INTEGER PRIMARY KEY, owner TEXT, kind TEXT, name TEXT,
                action TEXT, version TEXT, message TEXT, created REAL);
        ''')
        with self.db:
            for row in self.db.execute('SELECT * FROM flows').fetchall():
                if not self.head(row['owner'], 'flow', row['name']):
                    self.write(row['owner'], 'flow', row['name'], row['source'], 'put', '')

    def head(self, owner, kind, name):
        return self.db.execute('SELECT * FROM library_heads WHERE owner=? AND kind=? AND name=?',
                               (owner, kind, name)).fetchone()

    def read(self, owner, kind, name, version=None, stage='published'):
        self.check_name(kind, name)
        if version is None:
            head = self.head(owner, kind, name)
            version = (head['draft'] or head['published']) if head and stage == 'draft' else head['published'] if head else None
        row = self.db.execute('SELECT version,source FROM library_versions WHERE owner=? AND kind=? AND name=? AND version=?',
                              (owner, kind, name, version)).fetchone()
        if row is None:
            raise Refused('flow_not_found' if kind == 'flow' else 'map_not_found', 404)
        return {'kind': kind, 'name': name, 'version': row['version'], 'yaml': row['source']}

    @staticmethod
    def check_name(kind, name):
        if kind not in {'flow', 'map'} or not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', name):
            raise Refused('invalid_library_name', 422)

    def write(self, owner, kind, name, source, action, message):
        version = hashlib.sha256(source.encode()).hexdigest()
        exists = self.db.execute('SELECT 1 FROM library_versions WHERE owner=? AND kind=? AND name=? AND version=?',
                                 (owner, kind, name, version)).fetchone()
        count = self.db.execute('SELECT count(*) FROM library_versions WHERE owner=?', (owner,)).fetchone()[0]
        if not exists and count >= 1024:
            raise Refused('library_storage_full', 507)
        self.db.execute('INSERT OR IGNORE INTO library_versions VALUES(?,?,?,?,?,?)',
                        (owner, kind, name, version, source, time.time()))
        head = self.head(owner, kind, name)
        if not head or (head['published'] is None and head['draft'] is None):
            count = self.db.execute('SELECT count(*) FROM library_heads WHERE owner=? AND kind=? AND (published IS NOT NULL OR draft IS NOT NULL)',
                                    (owner, kind)).fetchone()[0]
            if count >= 128:
                raise Refused('flow_storage_full', 507)
            self.db.execute('INSERT OR IGNORE INTO library_heads VALUES(?,?,?,NULL,NULL)', (owner, kind, name))
        column = 'draft' if action == 'draft' else 'published'
        self.db.execute(f'UPDATE library_heads SET {column}=? WHERE owner=? AND kind=? AND name=?',
                        (version, owner, kind, name))
        self.event(owner, kind, name, action, version, message)
        return {'kind': kind, 'name': name, 'version': version, 'stage': column}

    def event(self, owner, kind, name, action, version, message):
        self.db.execute('INSERT INTO library_events(owner,kind,name,action,version,message,created) VALUES(?,?,?,?,?,?,?)',
                        (owner, kind, name, action, version, message, time.time()))

    def manage(self, owner, *, action, kind='flow', name=None, source=None, version=None,
               base_version=None, test_request_id=None, message='', limit=50, before=None, stage='published'):
        if action == 'list':
            rows = self.db.execute('SELECT name,published AS version,draft AS draft_version FROM library_heads '
                                   'WHERE owner=? AND kind=? AND (published IS NOT NULL OR draft IS NOT NULL) ORDER BY name', (owner, kind))
            return {'maps' if kind == 'map' else 'flows': [dict(row) for row in rows]}
        self.check_name(kind, name)
        if action == 'get':
            return self.read(owner, kind, name, version, stage)
        if action == 'history':
            rows = self.db.execute('SELECT id,action,version,message,created FROM library_events '
                                   'WHERE owner=? AND kind=? AND name=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?',
                                   (owner, kind, name, before, before, limit)).fetchall()
            return {'events': [dict(row) for row in rows], 'next_before': rows[-1]['id'] if len(rows) == limit else None}
        head = self.head(owner, kind, name)
        published, candidate = (head['published'], head['draft']) if head else (None, None)
        with self.db:
            if action in {'put', 'draft'}:
                if not source or len(source.encode()) > 32768:
                    raise Refused('invalid_flow_source', 422)
                expected = (candidate or published) if action == 'draft' else published
                if (action == 'draft' and base_version != expected) or (action == 'put' and version and version != published):
                    raise Refused('library_version_conflict' if action == 'draft' else 'flow_version_conflict')
                result = self.write(owner, kind, name, source, action, message)
                if action == 'put':
                    self.db.execute('INSERT OR REPLACE INTO flows VALUES(?,?,?,?)', (owner, name, result['version'], source))
                return result
            if action in {'publish', 'rollback'}:
                target = self.read(owner, kind, name, version)
                if version is None:
                    raise Refused('version_required', 422)
                if published == version:
                    return {**target, 'stage': 'published'}
                if base_version != published:
                    raise Refused('library_version_conflict')
                if action == 'publish':
                    if candidate != version:
                        raise Refused('draft_version_conflict')
                    self.check_test(owner, kind, name, version, test_request_id)
                else:
                    prior = self.db.execute("SELECT 1 FROM library_events WHERE owner=? AND kind=? AND name=? AND version=? AND action IN ('put','publish','rollback')",
                                            (owner, kind, name, version)).fetchone()
                    if not prior:
                        raise Refused('version_was_not_published')
                self.db.execute('UPDATE library_heads SET published=?,draft=NULL WHERE owner=? AND kind=? AND name=?',
                                (version, owner, kind, name))
                if kind == 'flow':
                    self.db.execute('INSERT OR REPLACE INTO flows VALUES(?,?,?,?)', (owner, name, version, target['yaml']))
                self.event(owner, kind, name, action, version, message)
                return {**target, 'stage': 'published'}
            if action == 'delete':
                if version and version != published:
                    raise Refused('flow_version_conflict')
                self.db.execute('UPDATE library_heads SET published=NULL,draft=NULL WHERE owner=? AND kind=? AND name=?', (owner, kind, name))
                if kind == 'flow':
                    self.db.execute('DELETE FROM flows WHERE owner=? AND name=?', (owner, name))
                self.event(owner, kind, name, action, published, message)
                return {'name': name, 'deleted': True}
            raise Refused('invalid_library_action', 422)

    def check_test(self, owner, kind, name, version, request_id):
        try:
            result = self.store.operation(owner, request_id)
        except Refused:
            raise Refused('successful_test_required') from None
        pins = result.get('library', {}).get('pins', [])
        if result['state'] != 'succeeded' or {'kind': kind, 'name': name, 'version': version} not in pins:
            raise Refused('successful_test_required')
        for pin in result.get('library', {}).get('floating', pins):
            if self.read(owner, pin['kind'], pin['name'], stage='draft')['version'] != pin['version']:
                raise Refused('test_dependencies_changed')
