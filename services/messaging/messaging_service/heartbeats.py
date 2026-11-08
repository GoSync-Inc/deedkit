"""Component heartbeats (schema 50, ANYM-534): the API, matcher and dispatcher report their version.

`set-delivery-features --enable` turns self-service delivery on only while all three are fresh and agree, so a
component still running an older release keeps the feature off. A failed write never stops the component.
"""
from __future__ import annotations

COMPONENTS = ('api', 'matcher', 'dispatcher')
HEARTBEAT_S = 30
MAX_AGE_S = 120
# ANYM-825: the package version does not change per merge, so a gate that needs new matcher behaviour names
# a feature this build reports after a '+' (version text, no schema change); older builds report none.
# The text must fit migration 050's CHECK (version ~ '^[0-9A-Za-z.+-]{1,64}$'): no underscores.
FEATURES = ('source-filters',)


def identity():
    from . import __version__
    return __version__ + '+' + '.'.join(FEATURES)


def record(store, component):
    import psycopg
    from .store import RUNTIME_SCHEMA
    try:
        with store.pool.connection() as c:
            c.execute('''INSERT INTO component_heartbeats(component,version,schema_version,beat_at)
                VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version,
                schema_version=EXCLUDED.schema_version,beat_at=EXCLUDED.beat_at''', (component, identity(), RUNTIME_SCHEMA))
        return True
    except psycopg.Error:
        # Includes a schema before 50 (no table) and a role without the grant.
        return False


def mismatch(c, *, features=()):
    """None when every component is fresh and runs this version on the current schema (and, when named,
    reports these build features), else the first problem."""
    from . import __version__
    from .store import RUNTIME_SCHEMA, schema_version
    current = schema_version(c)
    if current != RUNTIME_SCHEMA:
        return {'error': 'component_schema_mismatch', 'component': 'operator', 'schema_version': current}
    rows = {row['component']: row for row in c.execute('''SELECT component,version,schema_version,
        clock_timestamp()-beat_at<=make_interval(secs=>%s) AS fresh FROM component_heartbeats''', (MAX_AGE_S,))}
    for name in COMPONENTS:
        row = rows.get(name)
        if row is None:
            return {'error': 'component_heartbeat_absent', 'component': name}
        if not row['fresh']:
            return {'error': 'component_heartbeat_stale', 'component': name}
        version, _, built = row['version'].partition('+')
        if version != __version__:
            return {'error': 'component_version_mismatch', 'component': name, 'version': row['version'],
                'expected': __version__}
        if set(features) - set(built.split('.')):
            return {'error': 'component_feature_missing', 'component': name, 'version': row['version'],
                'expected': identity()}
        if row['schema_version'] != current:
            return {'error': 'component_schema_mismatch', 'component': name, 'schema_version': row['schema_version']}
    return None
