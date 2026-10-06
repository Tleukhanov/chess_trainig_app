"""Мульти-юзерная схема: миграция v0.3→v0.4, профили, изоляция партий.

Старая схема (v0.3): ``games.id PRIMARY KEY`` + ``analyses`` со ссылкой
на ``games(id)``. Новая: PK ``(id, user)``, ``analyses`` без FK, таблицы
``users``/``meta``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.games import Game

_LEGACY_GAMES = """
CREATE TABLE games(
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    rated INTEGER,
    speed TEXT,
    created_at INTEGER,
    status TEXT,
    winner TEXT,
    white TEXT,
    black TEXT,
    opening TEXT,
    eco TEXT,
    user_color TEXT,
    opponent TEXT,
    user_rating INTEGER,
    user_rating_diff INTEGER,
    result_for_user TEXT,
    moves_json TEXT,
    clocks_json TEXT,
    fetched_at INTEGER
);
"""

_LEGACY_ANALYSES = """
CREATE TABLE analyses(
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    engine TEXT,
    depth INTEGER,
    analysis_json TEXT,
    analyzed_at INTEGER
);
"""

_GAME_ROW_COLUMNS = (
    "id, user, rated, speed, created_at, status, winner, white, black, "
    "opening, eco, user_color, opponent, user_rating, user_rating_diff, "
    "result_for_user, moves_json, clocks_json, fetched_at"
)


def _game(gid: str, result: str = "win") -> Game:
    return Game(
        id=gid,
        rated=True,
        speed="rapid",
        created_at=1_700_000_000,
        status="mate",
        winner=None,
        white={"id": "w1", "username": "White"},
        black={"id": "b1", "username": "Black"},
        opening="Sicilian Defense",
        eco="B20",
        moves=["e4", "c5"],
        user_color="white",
        opponent=None,
        user_rating=1500,
        user_rating_diff=5,
        result_for_user=result,
    )


def _legacy_db(path: Path) -> None:
    """Собирает БД в схеме v0.3 с данными двух игроков.

    В v0.3 у каждого game id ровно одна копия (PK по id) — вторая партия
    игрока просто лежит отдельной строкой. Пересечение «оба играли одно и то же»
    до v0.4 физически невозможно, его проверяем уже на новой схеме.
    """
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_GAMES + _LEGACY_ANALYSES)
    conn.executescript("CREATE INDEX idx_games_user ON games(user);")
    row = [
        "g-ancient", "Tleukhanov", 1, "rapid", 1_690_000_000, "mate",
        "white", '{"id": "w1", "username": "Tleukhanov"}', '{"id": "b1", "username": "Rival"}',
        "Sicilian Defense", "B20", "white", "Rival", 1500, 5, "win",
        '["e4", "c5"]', "[]", 1_700_000_000,
    ]
    conn.execute(
        f"INSERT INTO games({_GAME_ROW_COLUMNS}) VALUES ({', '.join('?' for _ in row)})",
        row,
    )
    other = [str(v) for v in row]
    other[0] = "g-recent"
    other[1] = "rival"
    other[4] = "1700000100"
    other[6] = "black"
    other[7] = '{"id": "w1", "username": "Tleukhanov"}'
    other[8] = '{"id": "b1", "username": "Rival"}'
    other[11] = "black"
    other[12] = "Tleukhanov"
    other[16] = '["e4", "c5", "Nf3"]'
    conn.execute(
        f"INSERT INTO games({_GAME_ROW_COLUMNS}) VALUES ({', '.join('?' for _ in other)})",
        other,
    )
    for gid in ("g-ancient", "g-recent"):
        conn.execute(
            "INSERT INTO analyses(game_id, engine, depth, analysis_json, analyzed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (gid, "stockfish", 14, '{"moves": []}', 1_700_000_000),
        )
    conn.commit()
    conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, True)

    def test_old_schema_becomes_composite_pk(self) -> None:
        path = self.tmpdir / "old.db"
        _legacy_db(path)
        db = Database(path)
        db.init_db()
        with db._connection() as conn:
            pk = conn.execute("PRAGMA table_info(games)").fetchall()
            pk_cols = [row[5] for row in pk if row[5] > 0]
            self.assertEqual(pk_cols, [1, 2])  # (id, user)
            self.assertEqual(conn.execute("SELECT count(*) FROM games").fetchone()[0], 2)
            self.assertNotIn(
                "REFERENCES games",
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='analyses'"
                ).fetchone()[0],
            )
        # анализ один, а живёт у обоих игроков
        self.assertEqual(len(db.get_analyzed_games("Tleukhanov")), 1)
        self.assertEqual(len(db.get_analyzed_games("rival")), 1)
        # профили подтянулись из партий, текущий — самый частый/активный
        current = db.get_current_user()
        self.assertIsNotNone(current)
        self.assertIn(str(current["nick_lower"]), {"tleukhanov", "rival"})
        db.close()

    def test_migration_is_idempotent(self) -> None:
        path = self.tmpdir / "old.db"
        _legacy_db(path)
        Database(path).init_db()
        Database(path).init_db()
        db = Database(path)
        with db._connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM games").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM analyses").fetchone()[0], 2)
        db.close()

    def test_fresh_db_has_all_tables(self) -> None:
        db = Database(self.tmpdir / "new.db")
        db.init_db()
        with db._connection() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertTrue({"games", "analyses", "users", "meta"} <= names)
        self.assertIsNone(db.get_current_user())
        self.assertEqual(db.list_users(), [])
        db.close()

    def test_analysis_of_shared_game_computed_once(self) -> None:
        path = self.tmpdir / "new.db"
        db = Database(path)
        db.init_db()
        # обычная схема: игра между двумя людьми принадлежит обоим
        for user in ("alice", "bob"):
            self.assertEqual(db.save_games([_game("g1")], user), 1)
        db.save_analysis("g1", {"moves": [{"san": "e4"}]}, depth=8)
        # анализ один (не пересчитывается), виден обоим
        with db._connection() as conn:
            self.assertEqual(
                conn.execute("SELECT depth FROM analyses WHERE game_id='g1'").fetchone()[0],
                8,
            )
        self.assertEqual(len(db.get_analyzed_games("alice")), 1)
        self.assertEqual(len(db.get_analyzed_games("bob")), 1)
        # повторный анализ той же глубины не удваивает данные
        db.save_analysis("g1", {"moves": [{"san": "e4"}]}, depth=8)
        with db._connection() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM analyses WHERE game_id='g1'").fetchone()[0],
                1,
            )


class TwoUsersIsolationTests(unittest.TestCase):
    """Одна партия между двумя зарегистрированными людьми принадлежит обоим."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "users.db")
        self.db.init_db()
        self.addCleanup(self.db.close)

    def test_same_game_saved_under_two_users(self) -> None:
        for user in ("alice", "bob"):
            self.assertEqual(self.db.save_games([_game("g1")], user), 1)
        self.assertEqual(len(self.db.get_games("alice")), 1)
        self.assertEqual(len(self.db.get_games("bob")), 1)
        self.db.save_analysis("g1", {"moves": []}, depth=14)
        self.assertEqual(len(self.db.get_analyzed_games("alice")), 1)
        self.assertEqual(len(self.db.get_analyzed_games("bob")), 1)

    def test_get_game_scoped_by_user(self) -> None:
        from dataclasses import replace

        for user, color, result in (("alice", "white", "win"), ("bob", "black", "loss")):
            game = replace(_game("g1", result=result), user_color=color)
            self.db.save_games([game], user)
        alice = self.db.get_game("g1", user="alice")
        bob = self.db.get_game("g1", user="bob")
        self.assertEqual(alice.user_color, "white")
        self.assertEqual(alice.result_for_user, "win")
        self.assertEqual(bob.user_color, "black")
        self.assertEqual(bob.result_for_user, "loss")
        self.assertEqual(self.db.get_game("g1").id, "g1")

    def test_case_insensitive_lookups(self) -> None:
        self.db.save_games([_game("g1")], "Tleukhanov")
        self.assertEqual(len(self.db.get_games("tleukhanov")), 1)
        self.assertEqual(len(self.db.get_analyzed_games("TLEUKHANOV")), 0)  # ещё не проанализирована
        self.db.save_analysis("g1", {"moves": []}, depth=14)
        self.assertEqual(len(self.db.get_analyzed_games("tleukhanov")), 1)


class UsersCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "users.db")
        self.db.init_db()
        self.addCleanup(self.db.close)

    def test_upsert_get_current_fide(self) -> None:
        profile = self.db.upsert_user("  Tleukhanov  ", fide_id="4100000")
        self.assertEqual(profile["nick_lower"], "tleukhanov")
        self.assertEqual(self.db.get_user("TLEUKHANOV")["fide_id"], "4100000")
        self.db.set_current_user("tleukhanov")
        self.assertEqual(self.db.get_current_user()["nick"], "Tleukhanov")
        self.db.set_user_fide("Tleukhanov", "4100999")
        self.assertEqual(self.db.get_user("tleukhanov")["fide_id"], "4100999")

    def test_empty_nick_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.db.upsert_user("   ")

    def test_list_users_orders_by_nick_on_ties(self) -> None:
        self.db.upsert_user("bob")
        self.db.upsert_user("Alice", fide_id="42")
        users = self.db.list_users()
        # bound_at у обеих один и тот же (секунда), дальше сортируем по нику
        self.assertEqual([u["nick_lower"] for u in users], ["alice", "bob"])
        self.assertEqual(users[0]["fide_id"], "42")

    def test_backfill_merges_case_variants(self) -> None:
        self.db.save_games([_game("g1")], "Tleukhanov")
        self.db.save_games([Game(id="g2", moves=["d4"])], "tleukhanov")
        self.db.init_db()  # бэкфилл запускается на каждом init
        with self.db._connection() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM users WHERE nick_lower='tleukhanov'")
                .fetchone()[0], 1,
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM games").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()