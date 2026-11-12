"""Тесты честного недельного снимка и описательных дельт (audit C3/M8).

Синтетические партии/анализы; движок и сеть не запускаются. Покрывают:
миграцию plan_snapshots (SQLite, повторный init, сохранение данных),
первое сохранение недели (прошлая неделя не переписывается), жёсткие
гейты >=7 дней и >=10 новых анализов, описательные дельты (не «рост»),
переименование improved_* → active_* в cohort API и фронтенд-строки.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import timeutil
from app.api import _SESSION_COOKIE, create_api
from app.db import Database, _PLAN_SNAPSHOTS_DDL, _SCHEMA
from app.games import Game
from app.progress import (
    MIN_NEW_GAMES,
    MIN_SNAPSHOT_AGE_DAYS,
    MIN_TREND_GAMES,
    SNAPSHOT_CAVEAT,
    monday_of_week,
    snapshot_delta,
    summarize_recent,
)
from app import pgdb

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

_DAY_MS = 24 * 3600 * 1000


def _game(gid: str, created_at: int, result: str = "win") -> Game:
    return Game(
        id=gid,
        rated=True,
        speed="rapid",
        created_at=created_at,
        status="mate",
        winner=None,
        white={"name": "me"},
        black={"name": "opp"},
        opening="Sicilian Defense",
        eco="B20",
        moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"],
        clocks=[],
        user_color="white",
        opponent="opp",
        user_rating=1500,
        user_rating_diff=0,
        result_for_user=result,
    )


def _analysis() -> dict:
    def move(san: str, cls: str, drop: float) -> dict:
        return {
            "san": san,
            "classification": cls,
            "drop": drop,
            "win_before": 60.0,
            "win_after": 60.0 - drop,
        }

    return {
        "game_id": "g",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "opp",
        "avg_win_loss": 20.0,
        "avg_win_before": 60.0,
        "blunders": [2],
        "mistakes": [4],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": [
            move("e4", "good", 0.0),
            move("e5", "best", 0.0),
            move("Nf3", "blunder", 20.0),
            move("Nc6", "best", 0.0),
            move("Bc4", "mistake", 12.0),
            move("Bc5", "best", 0.0),
        ],
    }


class SnapshotMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "snap.db"

    def test_repeated_init_preserves_snapshots(self) -> None:
        db = Database(self.db_path)
        db.init_db()
        first = db.save_plan_snapshot(
            "Me",
            "2026-11-09",
            window_n=12,
            focus_phase="endgame",
            weak_openings=[{"opening": "Sicilian Defense", "games": 4}],
            motifs=[{"motif": "fork", "count": 3, "avg_drop": 15.0}],
            avg_drop=16.0,
            score=55.0,
            drills_target=5,
        )
        self.assertTrue(first["created"])
        # Повторный init не теряет и не переписывает данные.
        db2 = Database(self.db_path)
        db2.init_db()
        snap = db2.get_latest_plan_snapshot("me")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["window_n"], 12)
        self.assertEqual(snap["week_start"], "2026-11-09")
        self.assertEqual(snap["weak_openings"][0]["opening"], "Sicilian Defense")
        self.assertEqual(snap["motifs"][0]["motif"], "fork")
        self.assertEqual(snap["avg_drop"], 16.0)
        self.assertEqual(snap["score"], 55.0)
        self.assertEqual(snap["drills_target"], 5)
        self.assertEqual(snap["version"], 1)

    def test_first_save_of_week_wins(self) -> None:
        db = Database(self.db_path)
        db.init_db()
        first = db.save_plan_snapshot(
            "me", "2026-11-09", window_n=12, focus_phase="endgame",
            weak_openings=[], motifs=[], avg_drop=16.0, score=55.0,
            drills_target=5,
        )
        second = db.save_plan_snapshot(
            "me", "2026-11-09", window_n=3, focus_phase="opening",
            weak_openings=[{"opening": "X"}], motifs=[],
            avg_drop=1.0, score=99.0, drills_target=1,
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        kept = db.get_plan_snapshot("ME", "2026-11-09")
        self.assertEqual(kept["window_n"], 12)
        self.assertEqual(kept["focus_phase"], "endgame")

    def test_save_validates_inputs(self) -> None:
        db = Database(self.db_path)
        db.init_db()
        with self.assertRaises(ValueError):
            db.save_plan_snapshot("", "2026-11-09", window_n=1, focus_phase=None,
                                  weak_openings=[], motifs=[], avg_drop=None,
                                  score=None, drills_target=None)
        with self.assertRaises(ValueError):
            db.save_plan_snapshot("me", "not-a-date", window_n=1,
                                  focus_phase=None, weak_openings=[],
                                  motifs=[], avg_drop=None, score=None,
                                  drills_target=None)

    def test_ddl_is_postgresql_compatible(self) -> None:
        self.assertNotIn("COLLATE NOCASE", _PLAN_SNAPSHOTS_DDL)
        self.assertIn("UNIQUE(nick, week_start)", _PLAN_SNAPSHOTS_DDL)
        self.assertIn("version", _PLAN_SNAPSHOTS_DDL)
        self.assertIn("plan_snapshots", _SCHEMA)
        # Весь _SCHEMA остаётся исполнимым через SplitStatements pgdb
        # (то, как он уходит в PG через executescript).
        statements = pgdb.split_statements(_SCHEMA)
        self.assertTrue(
            any("plan_snapshots" in stmt for stmt in statements),
            "plan_snapshots DDL должен быть в общей схеме",
        )

    def test_count_analyzed_games_since(self) -> None:
        db = Database(self.db_path)
        db.init_db()
        games = [_game(f"g{i}", 1_000 + i) for i in range(3)]
        db.save_games(games, "me")
        old_ms = int(time.time() * 1000) - 10 * _DAY_MS
        for game in games:
            db.save_analysis(game.id, _analysis(), depth=8, analyzed_at=old_ms)
        self.assertEqual(db.count_analyzed_games_since("me", 0), 3)
        self.assertEqual(db.count_analyzed_games_since("me", old_ms + 1), 0)
        self.assertEqual(db.count_analyzed_games_since("me", old_ms), 3)

    def test_monday_of_week(self) -> None:
        import datetime

        self.assertEqual(
            monday_of_week(datetime.date(2026, 11, 12)), "2026-11-09"
        )
        self.assertEqual(
            monday_of_week(datetime.date(2026, 11, 9)), "2026-11-09"
        )


class SnapshotDeltaPureTests(unittest.TestCase):
    def _snap(self) -> dict:
        return {
            "week_start": "2026-10-26",
            "window_n": 12,
            "avg_drop": 18.0,
            "score": 50.0,
        }

    def _cur(self) -> dict:
        return {"window_n": 12, "avg_drop": 12.0, "score_pct": 60.0}

    def test_pending_before_seven_days(self) -> None:
        result = snapshot_delta(
            self._snap(), self._cur(), new_games=50, age_days=2.0
        )
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["deltas"], None)
        self.assertEqual(result["days_remaining"], MIN_SNAPSHOT_AGE_DAYS - 2)

    def test_insufficient_data_below_ten_new_games(self) -> None:
        result = snapshot_delta(
            self._snap(), self._cur(), new_games=MIN_NEW_GAMES - 1, age_days=9.0
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["deltas"], None)
        self.assertEqual(result["new_games"], MIN_NEW_GAMES - 1)

    def test_ok_returns_raw_deltas_with_caveat(self) -> None:
        result = snapshot_delta(
            self._snap(), self._cur(), new_games=15, age_days=9.0
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deltas"]["score_pct"], 10.0)
        self.assertEqual(result["deltas"]["avg_drop"], -6.0)
        self.assertIn("не доказывают", result["caveat"])


class _ApiBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)

    def _seed_analyzed(self, nick: str, count: int, *, analyzed_ms: int | None = None):
        now_ms = int(time.time() * 1000)
        games = [
            _game(f"{nick[:2]}{i}", 1_000_000 + i * 1000) for i in range(count)
        ]
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(games, nick)
            stamp = analyzed_ms if analyzed_ms is not None else now_ms
            for game in games:
                db.save_analysis(game.id, _analysis(), depth=8, analyzed_at=stamp)

    def _login(self, nick: str) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user(nick)
            token = secrets.token_hex(32)
            canonical = db.get_user_record(nick)["nick"]
            db.create_session(canonical, token, days=30)
        self.client.cookies.set(_SESSION_COOKIE, token)

    def _set_snapshot_age(self, nick: str, age_days: float) -> None:
        created_at = int(time.time() * 1000) - int(age_days * _DAY_MS)
        with Database(self.db_path) as db:
            db.init_db()
            with db._connection() as conn:
                conn.execute(
                    "UPDATE plan_snapshots SET created_at = ? WHERE LOWER(nick) = LOWER(?)",
                    (created_at, nick),
                )


class SnapshotApiTests(_ApiBase):
    def _save_goal(self) -> "object":
        return self.client.post(
            "/api/plan/goal",
            data={"goal": "Test goal", "minutes": "30", "focus": "auto",
                  "goal_type": "fix_weak", "tournament_date": "",
                  "target_rating": ""},
        )

    def test_goal_save_creates_snapshot_with_actual_window(self) -> None:
        self._seed_analyzed("alice", 12)
        self._login("alice")
        resp = self._save_goal()
        self.assertEqual(resp.status_code, 200, resp.text)
        with Database(self.db_path) as db:
            db.init_db()
            snap = db.get_latest_plan_snapshot("alice")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["window_n"], 12)
        self.assertEqual(snap["week_start"], monday_of_week(
            __import__("datetime").date.fromisoformat(timeutil.today())))
        self.assertEqual(snap["version"], 1)
        self.assertIsInstance(snap["drills_target"], int)
        self.assertIsInstance(snap["score"], float)
        self.assertIsInstance(snap["avg_drop"], float)
        # Агрегаты окна, не сырые партии.
        self.assertNotIn("moves", snap)
        self.assertNotIn("games", snap)

    def test_second_save_same_week_keeps_first_snapshot(self) -> None:
        self._seed_analyzed("alice", 12)
        self._login("alice")
        self._save_goal()
        with Database(self.db_path) as db:
            first = db.get_latest_plan_snapshot("alice")
        resp = self._save_goal()
        self.assertEqual(resp.status_code, 200, resp.text)
        with Database(self.db_path) as db:
            second = db.get_latest_plan_snapshot("alice")
        self.assertEqual(first["created_at"], second["created_at"])

    def test_goal_save_without_analyzed_games_creates_no_snapshot(self) -> None:
        self._login("alice")
        resp = self._save_goal()
        self.assertEqual(resp.status_code, 200, resp.text)
        with Database(self.db_path) as db:
            db.init_db()
            self.assertIsNone(db.get_latest_plan_snapshot("alice"))

    def test_read_endpoints_do_not_write_snapshot(self) -> None:
        self._seed_analyzed("alice", 12)
        self._login("alice")
        self.assertEqual(self.client.get("/api/plan/week").status_code, 200)
        self.assertEqual(self.client.get("/api/progress").status_code, 200)
        with Database(self.db_path) as db:
            db.init_db()
            self.assertIsNone(db.get_latest_plan_snapshot("alice"))

    def test_snapshot_status_no_snapshot(self) -> None:
        self._login("alice")
        resp = self.client.get("/api/plan/week/snapshot")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "no_snapshot")
        self.assertIsNone(payload["snapshot"])
        self.assertIn("не доказывают", payload["caveat"])

    def test_snapshot_status_pending_right_after_save(self) -> None:
        self._seed_analyzed("alice", 12)
        self._login("alice")
        self._save_goal()
        payload = self.client.get("/api/plan/week/snapshot").json()
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["snapshot"]["window_n"], 12)
        self.assertIn("created_iso", payload["snapshot"])

    def test_snapshot_status_insufficient_data(self) -> None:
        old_ms = int(time.time() * 1000) - 10 * _DAY_MS
        self._seed_analyzed("alice", 12, analyzed_ms=old_ms)
        self._login("alice")
        self._save_goal()
        self._set_snapshot_age("alice", 8.0)
        payload = self.client.get("/api/plan/week/snapshot").json()
        self.assertEqual(payload["status"], "insufficient_data")
        self.assertLess(payload["new_games"], MIN_NEW_GAMES)
        self.assertIsNone(payload["deltas"])

    def test_snapshot_status_ok_returns_deltas(self) -> None:
        self._seed_analyzed("alice", 12)
        self._login("alice")
        self._save_goal()
        self._set_snapshot_age("alice", 8.0)
        payload = self.client.get("/api/plan/week/snapshot").json()
        self.assertEqual(payload["status"], "ok", payload)
        self.assertGreaterEqual(payload["new_games"], MIN_NEW_GAMES)
        self.assertEqual(set(payload["deltas"]), {"score_pct", "avg_drop"})
        self.assertIn("не доказывают", payload["caveat"])

    def test_summarize_recent_aggregates(self) -> None:
        pairs = [(_game(f"s{i}", 1_000_000 + i), _analysis()) for i in range(4)]
        summary = summarize_recent(pairs)
        self.assertEqual(summary["window_n"], 4)
        self.assertEqual(summary["score_pct"], 100.0)
        self.assertEqual(summary["avg_drop"], 16.0)


class CohortHonestNamingTests(_ApiBase):
    def _make_admin(self, nick: str) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            with db._connection() as conn:
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE nick_lower = ?",
                    (nick.lower(),),
                )

    def test_cohort_renames_improved_to_active_and_keeps_aliases(self) -> None:
        today = timeutil.today()
        start = timeutil.dates_back(30, today)[0]
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user("alice")
            db.save_rating_snapshot("alice", start, 1500)
            db.save_rating_snapshot("alice", today, 1540)
            # 10 активных дней с >= 60% выполнения — порог active_completion.
            for day in timeutil.dates_back(10, today):
                db.upsert_daily_log("alice", day, minutes=40,
                                    tasks_done=4, tasks_total=5)
        self._login("boss")
        self._make_admin("boss")
        payload = self.client.get("/api/admin/cohort?days=30").json()
        alice = next(u for u in payload["users"] if u["nick"] == "alice")
        # Новые имена — активность, а не «улучшение».
        self.assertTrue(alice["active_rating"])
        self.assertTrue(alice["active_completion"])
        self.assertTrue(alice["active"])
        # Устаревшие алиасы совпадают по значению, но помечены deprecated.
        self.assertEqual(alice["improved_rating"], alice["active_rating"])
        self.assertEqual(alice["improved_activity"], alice["active_completion"])
        self.assertEqual(alice["improved"], alice["active"])
        self.assertIn("improved", payload["deprecated_keys"])
        self.assertIn("не доказательство", payload["note"])
        self.assertEqual(payload["summary"]["active_by_rating"], 1)
        self.assertEqual(payload["summary"]["active_by_completion"], 1)
        self.assertGreaterEqual(payload["summary"]["active"], 1)
        # Сырые дельты остаются на месте.
        self.assertEqual(alice["rating_delta"], 40)

    def test_cohort_csv_has_active_columns(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user("alice")
        self._login("boss")
        self._make_admin("boss")
        resp = self.client.get("/api/admin/cohort.csv?days=30")
        self.assertEqual(resp.status_code, 200)
        header = resp.text.splitlines()[0]
        for column in ("active", "active_rating", "active_completion",
                       "improved", "improved_rating", "improved_activity"):
            self.assertIn(column, header)


class FrontendHonestCopyTests(unittest.TestCase):
    def test_week_snapshot_card_and_caveat(self) -> None:
        js = (FRONTEND / "app.js").read_text(encoding="utf-8")
        for needle in (
            "Снимок недели",
            "/api/plan/week/snapshot",
            "недостаточно новых данных",
            "описательно",
            "не доказательство роста рейтинга",
            "не доказывает рост рейтинга",
        ):
            self.assertIn(needle, js, needle)

    def test_progress_shows_sample_size_and_gate(self) -> None:
        js = (FRONTEND / "app.js").read_text(encoding="utf-8")
        self.assertIn("min_games", js)
        self.assertIn("НЕДОСТАТОЧНО ДАННЫХ", js)
        self.assertIn("n=", js)

    def test_cohort_caveat_visible(self) -> None:
        js = (FRONTEND / "app.js").read_text(encoding="utf-8")
        self.assertIn("вовлечённости", js)
        self.assertIn("не измеряют рост навыка", js)
        self.assertNotIn(">Улучшились<", js)

    def test_min_trend_games_constant_is_ten(self) -> None:
        self.assertEqual(MIN_TREND_GAMES, 10)
        self.assertEqual(MIN_NEW_GAMES, 10)
        self.assertEqual(MIN_SNAPSHOT_AGE_DAYS, 7)
        self.assertIn("не доказывают", SNAPSHOT_CAVEAT)


if __name__ == "__main__":
    unittest.main()
