"""A running calendar Store keeps reading a table that a migration adds a column to.

`service migrate` applies a schema while calendar-api and calendar-worker may run (schema_v4 added columns to
`plans`, read with SELECT *). A server-side prepared SELECT * then fails with "cached plan must not change result
type" (messaging hit this in production, Tiden issue 086304e6). Synthetic table in the per-test schema only."""
from psycopg import sql


# spec: ANYM-552
def test_select_star_survives_an_added_column_on_the_same_pooled_connection(store):
    table = sql.Identifier(store.schema, "synthetic_plan_probe")
    with store._connect() as admin:
        admin.execute(sql.SQL("CREATE TABLE {} (id text PRIMARY KEY)").format(table))
        admin.execute(sql.SQL("INSERT INTO {} VALUES ('a')").format(table))
    with store.pool.connection() as c:
        # More runs than psycopg's default prepare threshold (5), so a preparing pool would hold a plan.
        for _ in range(8):
            assert c.execute(sql.SQL("SELECT * FROM {} WHERE id=%s").format(table), ("a",)).fetchone() == {"id": "a"}
        c.commit()
        with store._connect() as admin:
            admin.execute(sql.SQL("ALTER TABLE {} ADD COLUMN token_hash text").format(table))
        row = c.execute(sql.SQL("SELECT * FROM {} WHERE id=%s").format(table), ("a",)).fetchone()
        assert row == {"id": "a", "token_hash": None}
