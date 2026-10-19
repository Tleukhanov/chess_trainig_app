"""Heroku-деплой: PG-адаптер (юнит-уровень, без живого PG), резолв Stockfish,
файлы деплоя (Procfile/runtime.txt/Aptfile/app.json) и выбор бэкенда.

Живого Postgres в песочнице нет: PG-путь проверен стабом psycopg через
sys.modules (SQL уходит переведённый, ?,→%s, без PRAGMA/GLOB) и эквивалентностью
предиката GLOB↔regex на строках через настоящий SQLite. Остальной suite идёт
по SQLite без DATABASE_URL.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import pgdb
from app.config import _resolve_stockfish_path
from app.db import (
    _LINK_BACKFILL_COMMON,
    _LINK_BACKFILL_PG,
    _LINK_BACKFILL_SQLITE,
    Database,
)

ROOT = Path(__file__).resolve().parent.parent


class TranslateTest(unittest.TestCase):
    def test_plain_placeholders(self) -> None:
        self.assertEqual(
            pgdb.translate_placeholders("SELECT * FROM games WHERE id = ? AND user = ?"),
            "SELECT * FROM games WHERE id = %s AND user = %s",
        )

    def test_no_placeholders_untouched(self) -> None:
        sql = "SELECT count(*) FROM games"
        self.assertEqual(pgdb.translate_placeholders(sql), sql)

    def test_question_in_single_quoted_literal_kept(self) -> None:
        # Зафиксированное поведение: ? внутри литерала — не плейсхолдер.
        sql = "SELECT 'что?' AS q WHERE id = ?"
        self.assertEqual(
            pgdb.translate_placeholders(sql),
            "SELECT 'что?' AS q WHERE id = %s",
        )

    def test_escaped_single_quote_inside_literal(self) -> None:
        sql = "SELECT 'it''s?' WHERE a = ?"
        self.assertEqual(
            pgdb.translate_placeholders(sql),
            "SELECT 'it''s?' WHERE a = %s",
        )

    def test_double_quoted_literal_kept(self) -> None:
        sql = 'SELECT "a?b" WHERE a = ?'
        self.assertEqual(
            pgdb.translate_placeholders(sql),
            'SELECT "a?b" WHERE a = %s',
        )

    def test_values_with_empty_literal(self) -> None:
        self.assertEqual(
            pgdb.translate_placeholders("INSERT INTO meta(key, value) VALUES (?, '')"),
            "INSERT INTO meta(key, value) VALUES (%s, '')",
        )


class SplitScriptTest(unittest.TestCase):
    def test_simple_split(self) -> None:
        parts = pgdb.split_statements("CREATE TABLE a(x); CREATE TABLE b(y);")
        self.assertEqual(parts, ["CREATE TABLE a(x)", "CREATE TABLE b(y)"])

    def test_semicolon_inside_literal_not_split(self) -> None:
        parts = pgdb.split_statements("INSERT INTO m VALUES ('a;b'); SELECT 1;")
        self.assertEqual(parts, ["INSERT INTO m VALUES ('a;b')", "SELECT 1"])

    def test_empty_tail_dropped(self) -> None:
        self.assertEqual(pgdb.split_statements("SELECT 1;;;"), ["SELECT 1"])
        self.assertEqual(pgdb.split_statements(""), [])


class NormalizeUrlTest(unittest.TestCase):
    def test_postgres_scheme_normalized(self) -> None:
        self.assertEqual(
            pgdb.normalize_database_url("postgres://u:p@h:5432/db"),
            "postgresql://u:p@h:5432/db",
        )

    def test_postgresql_scheme_kept(self) -> None:
        url = "postgresql://u:p@h:5432/db"
        self.assertEqual(pgdb.normalize_database_url(url), url)

    def test_empty_and_spaces(self) -> None:
        self.assertEqual(pgdb.normalize_database_url(""), "")
        self.assertEqual(pgdb.normalize_database_url("   "), "")


class GlobRegexEquivalenceTest(unittest.TestCase):
    _SAMPLES = [
        "abcdEF12", "12345678", "abcdefgh", "ABCDEFGH", "a1B2c3D4",
        "00000000", "ZZzz9999",
        "", "abc", "abcdefg", "abcdefghi", "123456789",
        "abcd_f12", "abcd-f12", "abcd f12", "abcdEF1!", "abcdEF1?",
        "abcdefgh\n"[:8], "абвгде12", "ébcdef12",
    ]

    def test_sqlite_glob_matches_pg_regex(self) -> None:
        con = sqlite3.connect(":memory:")
        try:
            for sample in self._SAMPLES:
                glob_hit = bool(
                    con.execute(
                        f"SELECT ? GLOB {_LINK_BACKFILL_SQLITE.split('GLOB', 1)[1].strip()}",
                        (sample,),
                    ).fetchone()[0]
                )
                pg_hit = len(sample) == 8 and re.fullmatch(r"[A-Za-z0-9]{8}", sample) is not None
                self.assertEqual(
                    glob_hit, pg_hit, f"расхождение предиката на {sample!r}"
                )
        finally:
            con.close()

    def test_backfill_constants_shape(self) -> None:
        self.assertIn("length(id) = 8", _LINK_BACKFILL_COMMON)
        self.assertIn("GLOB", _LINK_BACKFILL_SQLITE)
        self.assertIn("~", _LINK_BACKFILL_PG)
        self.assertIn("^[A-Za-z0-9]{8}$", _LINK_BACKFILL_PG)


class _FakeCursor:
    def __init__(self, log: list) -> None:
        self._log = log

    def execute(self, sql: str, params=()) -> _FakeCursor:
        self._log.append((sql, params))
        return self

    def fetchone(self):  # noqa: ANN202 — стаб для теста
        return None

    def fetchall(self):  # noqa: ANN202 — стаб для теста
        return []

    @property
    def rowcount(self) -> int:
        return 0


class _FakeConn:
    def __init__(self, log: list) -> None:
        self._log = log

    def execute(self, sql: str, params=()) -> _FakeCursor:
        return _FakeCursor(self._log).execute(sql, params)

    def commit(self) -> None:
        self._log.append(("COMMIT", ()))

    def rollback(self) -> None:
        self._log.append(("ROLLBACK", ()))

    def close(self) -> None:
        pass


class _FakePsycopg:
    def __init__(self, log: list) -> None:
        self._log = log
        self.dsn_seen: str | None = None

    def connect(self, dsn: str, row_factory=None) -> _FakeConn:
        self.dsn_seen = dsn
        self._log.append(("CONNECT", (dsn,)))
        return _FakeConn(self._log)


def _install_psycopg_stub(log: list) -> _FakePsycopg:
    fake = _FakePsycopg(log)
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    sys.modules["psycopg"] = fake  # type: ignore[assignment]
    sys.modules["psycopg.rows"] = rows_mod
    return fake


class BackendSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: sys.modules.pop("psycopg", None))
        self.addCleanup(lambda: sys.modules.pop("psycopg.rows", None))

    def _db_path(self) -> Path:
        return Path(self.tmp.name) / "t.db"

    def test_no_database_url_uses_sqlite(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            db = Database(self._db_path())
        self.assertEqual(db.backend, "sqlite")
        self.assertIsNone(db.dsn)
        db.init_db()
        self.assertEqual(db.upsert_user("AbC")["nick"], "AbC")

    def test_database_url_selects_pg_with_stub(self) -> None:
        log: list = []
        fake = _install_psycopg_stub(log)
        with mock.patch.dict(
            os.environ, {"DATABASE_URL": "postgres://u:p@host:5432/db"}
        ):
            db = Database(self._db_path())
            self.assertEqual(db.backend, "pg")
            self.assertEqual(db.dsn, "postgresql://u:p@host:5432/db")
            # Database не падает при импорте/подключении через стаб.
            self.assertIsNone(db.get_user("nobody"))
            db.init_db()
        self.assertEqual(fake.dsn_seen, "postgresql://u:p@host:5432/db")

    def test_pg_sql_translated_no_pragma_no_glob(self) -> None:
        log: list = []
        _install_psycopg_stub(log)
        with mock.patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://u:p@host:5432/db"}
        ):
            db = Database(self._db_path())
            db.init_db()
            db.create_session("Nick", "tok123")
            db.get_user("Nick")
        statements = [sql for sql, _ in log if isinstance(sql, str)]
        blob = "\n".join(statements)
        self.assertIn("information_schema", blob)
        self.assertNotIn("PRAGMA", blob)
        self.assertNotIn("GLOB", blob)
        self.assertNotIn("COLLATE NOCASE", blob)
        self.assertNotIn("INSERT OR REPLACE", blob)
        self.assertIn("ON CONFLICT(token) DO UPDATE", blob)
        selects = [s for s in statements if s.startswith("SELECT * FROM users")]
        self.assertTrue(selects)
        for sql in selects:
            self.assertIn("%s", sql)
            self.assertNotIn("?", sql)

    def test_pg_migration_sql_uses_if_not_exists_and_public_schema(self) -> None:
        """PG-миграция: ADD COLUMN IF NOT EXISTS и information_schema с public."""
        log: list = []
        _install_psycopg_stub(log)
        with mock.patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://u:p@host:5432/db"}
        ):
            db = Database(self._db_path())
            db.init_db()
        statements = [sql for sql, _ in log if isinstance(sql, str)]
        blob = "\n".join(statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS", blob)
        self.assertIn("table_schema = 'public'", blob)

    def test_psycopg_not_required_without_database_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            import app.pgdb  # noqa: F401 — обязан импортироваться без psycopg

            db = Database(self._db_path())
            self.assertEqual(db.backend, "sqlite")


class StockfishResolveTest(unittest.TestCase):
    def test_explicit_existing_file_wins(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"STOCKFISH_PATH": path}):
                with mock.patch("shutil.which", return_value="/usr/games/stockfish"):
                    self.assertEqual(_resolve_stockfish_path(), path)
        finally:
            os.unlink(path)

    def test_missing_file_falls_back_to_which(self) -> None:
        with mock.patch.dict(
            os.environ, {"STOCKFISH_PATH": r"C:\nonexistent\stockfish.exe"}
        ):
            with mock.patch("shutil.which", return_value="/usr/games/stockfish"):
                self.assertEqual(_resolve_stockfish_path(), "/usr/games/stockfish")

    def test_empty_env_uses_which(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCKFISH_PATH", None)
            with mock.patch("shutil.which", return_value="/usr/games/stockfish"):
                self.assertEqual(_resolve_stockfish_path(), "/usr/games/stockfish")

    def test_nothing_found_returns_as_is(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCKFISH_PATH", None)
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(_resolve_stockfish_path(), "")


class DeployFilesTest(unittest.TestCase):
    def test_procfile(self) -> None:
        text = (ROOT / "Procfile").read_text(encoding="utf-8")
        self.assertIn("uvicorn app.api:create_api", text)
        self.assertIn("--factory", text)
        self.assertIn("--host 0.0.0.0", text)
        self.assertIn("--port $PORT", text)
        self.assertIn("warning", text)

    def test_runtime(self) -> None:
        text = (ROOT / "runtime.txt").read_text(encoding="utf-8").strip()
        self.assertTrue(text.startswith("python-3.12."), text)

    def test_aptfile(self) -> None:
        text = (ROOT / "Aptfile").read_text(encoding="utf-8")
        self.assertIn("stockfish", text)

    def test_requirements_has_psycopg_only_addition(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("psycopg[binary]", text)

    def test_app_json(self) -> None:
        manifest = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
        urls = [b.get("url", "") for b in manifest["buildpacks"]]
        self.assertTrue(any("python" in u for u in urls), urls)
        self.assertTrue(any("apt" in u for u in urls), urls)
        plans = [a.get("plan", "") for a in manifest["addons"]]
        self.assertTrue(any("postgres" in p for p in plans), plans)
        env = manifest["env"]
        self.assertEqual(env["STOCKFISH_PATH"]["value"], "/usr/games/stockfish")
        self.assertEqual(env["STOCKFISH_HASH_MB"]["value"], "32")
        self.assertEqual(env["STOCKFISH_THREADS"]["value"], "1")
        self.assertEqual(env["STOCKFISH_TIME_MS"]["value"], "800")
        self.assertEqual(env["STOCKFISH_DEPTH"]["value"], "12")
        self.assertEqual(env["STOCKFISH_MULTIPV"]["value"], "1")
        for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            self.assertIn(key, env)
        formation = manifest["formation"]["web"]
        self.assertEqual(formation["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
