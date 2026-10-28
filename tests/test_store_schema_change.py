"""A running Store keeps reading a table that an additive migration changes under it (Tiden issue 086304e6).

Production 2026-10-27: migration 045 added a column to messages while the Telegram collector ran; its pooled
connection had a server-side prepared `SELECT * FROM messages ... FOR UPDATE`, and PostgreSQL refused it with
"cached plan must not change result type", which stopped the collector. Synthetic table only."""
import psycopg


# spec: ANYM-444
def test_select_star_survives_an_added_column_on_the_same_pooled_connection(database):
    with psycopg.connect(database.pool.conninfo, autocommit=True) as admin:
        admin.execute('DROP TABLE IF EXISTS synthetic_plan_probe')
        admin.execute("CREATE TABLE synthetic_plan_probe (id text PRIMARY KEY); INSERT INTO synthetic_plan_probe VALUES ('a')")
        try:
            with database.pool.connection() as c:
                # More runs than psycopg's default prepare threshold (5), so a preparing pool would hold a plan.
                for _ in range(8):
                    assert c.execute('SELECT * FROM synthetic_plan_probe WHERE id=%s', ('a',)).fetchone() == {'id': 'a'}
                c.commit()
                admin.execute("ALTER TABLE synthetic_plan_probe ADD COLUMN purged boolean NOT NULL DEFAULT false")
                row = c.execute('SELECT * FROM synthetic_plan_probe WHERE id=%s FOR UPDATE', ('a',)).fetchone()
                assert row == {'id': 'a', 'purged': False}
                assert c.execute('SELECT * FROM synthetic_plan_probe WHERE id=%s', ('a',)).fetchone() == row
        finally:
            admin.execute('DROP TABLE IF EXISTS synthetic_plan_probe')
