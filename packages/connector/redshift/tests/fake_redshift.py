"""A small stand-in for a Redshift database, backed by SQLite files.

The connector's SQL is plain enough (quoted identifiers, ``CAST ... AS
VARCHAR``, ``ORDER BY ... NULLS FIRST``, a subquery) that SQLite runs it
unchanged, so the sync is tested against a real SQL engine instead of a
mock that pattern-matches statements. ``public`` is an attached database, so
``"public"."tickets"`` resolves like a schema-qualified Redshift table, and
``svv_all_tables`` / ``svv_all_columns`` hold the catalog with comments, the
way Redshift's system views do.

Every statement goes through the connector's own ``DirectConnection``, and
``statements`` records each one with its parameters.
"""

import sqlite3

from cognee_community_connector_redshift import DirectConnection

DATABASE = "dev"


class FakeRedshift:
    def __init__(self, tmp_path):
        self._main = str(tmp_path / "redshift_main.db")
        self._public = str(tmp_path / "redshift_public.db")
        self.statements: list[tuple[str, dict | None]] = []
        self.fail_on: str | None = None
        self.fail_message: str | None = None
        with self._admin() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS svv_all_tables (database_name TEXT, "
                "schema_name TEXT, table_name TEXT, table_type TEXT, remarks TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS svv_all_columns (database_name TEXT, "
                "schema_name TEXT, table_name TEXT, column_name TEXT, ordinal_position INT, "
                "data_type TEXT, remarks TEXT)"
            )

    def _connect(self):
        db = sqlite3.connect(self._main, check_same_thread=False)
        db.execute("ATTACH DATABASE ? AS public", (self._public,))
        db.create_function("current_database", 0, lambda: DATABASE)
        return db

    def _admin(self):
        return _Admin(self._connect())

    def connection(self) -> DirectConnection:
        fake = self

        class _Recording(DirectConnection):
            def query(self, sql, params=None):
                fake.statements.append((sql, dict(params) if params else None))
                if fake.fail_on and fake.fail_on in sql:
                    raise RuntimeError(fake.fail_message or f"statement failed: {fake.fail_on}")
                try:
                    yield from super().query(sql, params)
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc):
                        # What Redshift answers for a table that is gone.
                        raise RuntimeError(f'ERROR: relation "{exc}" does not exist') from exc
                    raise

        return _Recording(self._connect, paramstyle="named")

    # -- schema --------------------------------------------------------------
    def create_table(self, name, columns, *, comment=None, column_comments=None):
        """``columns`` is ``{column: type}``; ``name`` is a table in ``public``."""
        column_comments = column_comments or {}
        with self._admin() as db:
            definition = ", ".join(f'"{c}" {t}' for c, t in columns.items())
            db.execute(f'CREATE TABLE public."{name}" ({definition})')
            db.execute(
                "INSERT INTO svv_all_tables VALUES (?, 'public', ?, 'TABLE', ?)",
                (DATABASE, name, comment),
            )
            for position, (column, data_type) in enumerate(columns.items(), start=1):
                db.execute(
                    "INSERT INTO svv_all_columns VALUES (?, 'public', ?, ?, ?, ?, ?)",
                    (DATABASE, name, column, position, data_type, column_comments.get(column)),
                )

    def drop_table(self, name):
        with self._admin() as db:
            db.execute(f'DROP TABLE public."{name}"')
            db.execute("DELETE FROM svv_all_tables WHERE table_name = ?", (name,))
            db.execute("DELETE FROM svv_all_columns WHERE table_name = ?", (name,))

    def hide_from_catalog(self, name):
        """The table stays readable but svv_all_* no longer lists it (a catalog gap)."""
        with self._admin() as db:
            db.execute("DELETE FROM svv_all_tables WHERE table_name = ?", (name,))
            db.execute("DELETE FROM svv_all_columns WHERE table_name = ?", (name,))

    def comment_on(self, name, comment):
        with self._admin() as db:
            db.execute(
                "UPDATE svv_all_tables SET remarks = ? WHERE table_name = ?", (comment, name)
            )

    # -- data ----------------------------------------------------------------
    def insert(self, name, *rows):
        with self._admin() as db:
            for row in rows:
                columns = ", ".join(f'"{c}"' for c in row)
                marks = ", ".join("?" for _ in row)
                db.execute(
                    f'INSERT INTO public."{name}" ({columns}) VALUES ({marks})', tuple(row.values())
                )

    def update(self, name, key_column, key, /, **values):
        with self._admin() as db:
            assignments = ", ".join(f'"{c}" = ?' for c in values)
            db.execute(
                f'UPDATE public."{name}" SET {assignments} WHERE "{key_column}" = ?',
                (*values.values(), key),
            )

    def delete(self, name, key_column, key):
        with self._admin() as db:
            db.execute(f'DELETE FROM public."{name}" WHERE "{key_column}" = ?', (key,))


class _Admin:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        self._db.commit()
        self._db.close()
