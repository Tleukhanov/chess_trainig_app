"""Тесты плана дня, дневного лога, стрика, часового пояса и админ-когорты."""

from __future__ import annotations

import datetime
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import timeutil
from app.api import create_api, _maybe_snapshot_rating, _snapshot_rating_worker
from app.db import Database, is_admin
from app.dayplan import KINDS, build_tasks

_RICH_PLAN = {
    "repertoire": {
        "weak_openings": [
            {
                "opening": "Sicilian Defense",
                "eco": "B20",
                "games": 12,
                "points_pct": 41.7,
                "errors_per_game": 1.5,
                "color": "black",
            },
            {
                "opening": "French Defense",
                "eco": "C00",
                "games": 5,
                "points_pct": 50.0,
                "errors_per_game": 1.0,
                "color": "black",
            },
        ],
        "white": [],
        "black": [],
    },
    "patterns": {
        "motifs": [
            ["вилка", {"count": 5, "avg_drop": 12.5}],
            ["связка", {"count": 3, "avg_drop": 8.0}],
        ],
        "phases": [
            ["endgame", {"count": 4, "blunders": 2, "mistakes": 1, "avg_drop": 15.0}],
            ["midgame", {"count": 6, "blunders": 1, "mistakes": 2, "avg_drop": 9.0}],
        ],
        "total_bad": 16,
    },
    "humanity": {"unnatural": 3, "total": 12},
}

_TASK = {
    "task_key": "game1|6",
    "game_id": "game1",
    "ply": 6,
    "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "classification": "blunder",
    "drop": 32.0,
    "opening": "Sicilian Defense",
    "eco": "B20",
    "user_color": "black",
    "done": False,
}


class BuildTasksTests(unittest.TestCase):
    """Чистая функция build_tasks: состав, стабильность, мусор на входе."""

    def test_rich_plan_gives_five_tasks(self) -> None:
        tasks = build_tasks(_RICH_PLAN, _TASK, "2026-10-26")
        self.assertEqual(len(tasks), 5)
        self.assertEqual({task["kind"] for task in tasks}, set(KINDS))
        for task in tasks:
            self.assertIsInstance(task["id"], str)
            self.assertTrue(task["id"].startswith("2026-10-26-"))
            self.assertIn(task["kind"], KINDS)
            self.assertTrue(task["title"])
            self.assertTrue(task["why"])
            self.assertIsInstance(task["action"], dict)
            self.assertIsInstance(task["done"], bool)

        opening = next(t for t in tasks if t["kind"] == "opening")
        self.assertEqual(opening["action"]["view"], "opening")
        self.assertEqual(opening["action"]["opening"], "Sicilian Defense")
        self.assertEqual(opening["action"]["eco"], "B20")
        self.assertEqual(opening["action"]["color"], "black")

        motif = next(t for t in tasks if t["kind"] == "middlegame")
        self.assertEqual(motif["action"]["motif"], "вилка")

        endgame = next(t for t in tasks if t["kind"] == "endgame")
        self.assertEqual(endgame["action"]["phase"], "endgame")

        daily = next(t for t in tasks if t["kind"] == "task")
        self.assertEqual(daily["action"]["task_key"], "game1|6")

    def test_sparse_plan_gives_task_and_review(self) -> None:
        tasks = build_tasks({}, _TASK, "2026-10-26")
        self.assertEqual([task["kind"] for task in tasks], ["task", "review"])
        self.assertEqual(len({task["id"] for task in tasks}), 2)

    def test_no_data_no_task_gives_review_only(self) -> None:
        tasks = build_tasks({}, None, "2026-10-26")
        self.assertEqual([task["kind"] for task in tasks], ["review"])
        self.assertEqual(tasks[0]["action"]["view"], "review")

    def test_build_is_stable_and_ids_unique(self) -> None:
        first = build_tasks(_RICH_PLAN, _TASK, "2026-10-26")
        second = build_tasks(_RICH_PLAN, _TASK, "2026-10-26")
        self.assertEqual(first, second)
        self.assertEqual(
            len({task["id"] for task in first}), len(first)
        )
        other_day = build_tasks(_RICH_PLAN, _TASK, "2026-10-27")
        self.assertNotEqual(
            {task["id"] for task in first},
            {task["id"] for task in other_day},
        )

    def test_garbage_plan_does_not_crash(self) -> None:
        garbage = {
            "repertoire": {"weak_openings": "oops"},
            "patterns": {
                "motifs": [None, ["", {}], ("вилка", "not-a-dict")],
                "phases": {"midgame": {"count": 1}},
            },
            "humanity": "bad",
        }
        tasks = build_tasks(garbage, _TASK, "2026-10-26")
        self.assertIn("review", {task["kind"] for task in tasks})
        self.assertIn("task", {task["kind"] for task in tasks})
        for task in tasks:
            self.assertIn(task["kind"], KINDS)


class DailyPlanDbTests(unittest.TestCase):
    """Хранение плана/лога, toggle, стрик и границы суток."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "trainer.db")
        self.db.init_db()

    def test_toggle_and_daily_log_counts(self) -> None:
        day = "2026-10-26"
        tasks = build_tasks(_RICH_PLAN, _TASK, day)
        self.db.save_daily_plan("bob", day, tasks)

        saved = self.db.get_daily_plan("bob", day)
        self.assertEqual([t["id"] for t in saved], [t["id"] for t in tasks])
        log = self.db.get_daily_log("bob", day)
        self.assertEqual(log["tasks_total"], 5)
        self.assertEqual(log["tasks_done"], 0)

        target = saved[0]["id"]
        self.assertTrue(self.db.toggle_daily_task("bob", day, target, True))
        log = self.db.get_daily_log("bob", day)
        self.assertEqual(log["tasks_done"], 1)
        self.assertEqual(log["tasks_total"], 5)

        # Повторное сохранение плана не сбрасывает прогресс.
        self.db.save_daily_plan("bob", day, tasks)
        self.assertTrue(self.db.get_daily_plan("bob", day)[0]["done"])
        self.assertFalse(
            self.db.toggle_daily_task("bob", day, "no-such-task", True)
        )

    def test_streak_counts_consecutive_active_days(self) -> None:
        today = timeutil.today()
        dates = timeutil.dates_back(6, today)
        for offset in (0, 1, 2):
            self.db.upsert_daily_log("bob", dates[-1 - offset], minutes=10)
        self.db.upsert_daily_log("bob", dates[-4], minutes=0)
        self.db.upsert_daily_log("bob", dates[-5], minutes=10)
        self.assertEqual(self.db.streak_days("bob", today), 3)
        # Сегодня пусто — стрик не «вчерашний», а честный ноль.
        self.db.upsert_daily_log("bob", today, minutes=0, tasks_done=0)
        self.assertEqual(self.db.streak_days("bob", today), 0)

    def test_daily_log_merges_and_history_is_dense(self) -> None:
        today = timeutil.today()
        dates = timeutil.dates_back(4, today)
        self.db.upsert_daily_log(
            "bob", dates[-1], minutes=30, tasks_done=2, tasks_total=5
        )
        self.db.upsert_daily_log("bob", dates[-1], tasks_done=3)
        log = self.db.get_daily_log("bob", dates[-1])
        self.assertEqual(log["minutes"], 30)
        self.assertEqual(log["tasks_done"], 3)
        self.assertEqual(log["tasks_total"], 5)

        history = self.db.list_daily_logs("bob", 4, today)
        self.assertEqual([item["date"] for item in history], dates)
        self.assertEqual(
            [(item["tasks_done"], item["minutes"]) for item in history],
            [(0, 0), (0, 0), (0, 0), (3, 30)],
        )

    def test_timezone_is_almaty(self) -> None:
        expected = datetime.datetime.now(ZoneInfo("Asia/Almaty")).strftime(
            "%Y-%m-%d"
        )
        self.assertEqual(timeutil.today("Asia/Almaty"), expected)
        self.assertEqual(
            timeutil.now("Asia/Almaty").utcoffset(),
            datetime.timedelta(hours=5),
        )
        # 20:00 UTC 1 января — это уже 2 января в Алматы.
        boundary = int(
            datetime.datetime(
                2026, 1, 1, 20, 0, tzinfo=datetime.timezone.utc
            ).timestamp()
            * 1000
        )
        self.assertEqual(
            timeutil.date_of_ms(boundary, "Asia/Almaty"), "2026-01-02"
        )
        start_ms, end_ms = timeutil.day_bounds_ms("2026-01-02", "Asia/Almaty")
        self.assertEqual(end_ms - start_ms, 86_400_000)
        self.assertEqual(
            datetime.datetime.fromtimestamp(
                start_ms / 1000, datetime.timezone.utc
            ),
            datetime.datetime(2026, 1, 1, 19, 0, tzinfo=datetime.timezone.utc),
        )

    def test_rating_snapshot_worker_and_throttle(self) -> None:
        day = "2026-10-26"
        with patch(
            "app.api.fetch_lichess_rating",
            return_value={"rating": 2150, "speed": "rapid"},
        ):
            _snapshot_rating_worker(self.db.path, "bob", day)
        self.assertEqual(self.db.get_rating_snapshot("bob", day), 2150)

        # Ошибка сети не всплывает наружу и не затирает сохранённое.
        with patch(
            "app.api.fetch_lichess_rating", side_effect=RuntimeError("offline")
        ):
            _snapshot_rating_worker(self.db.path, "bob", "2026-10-27")
        self.assertIsNone(self.db.get_rating_snapshot("bob", "2026-10-27"))

        # Не чаще раза в день: маркер попытки ставится до потока.
        with patch("app.api._RATING_SNAPSHOT_ENABLED", True), patch(
            "app.api.threading.Thread"
        ) as thread_cls:
            _maybe_snapshot_rating(self.db, "bob", "2026-10-28")
            _maybe_snapshot_rating(self.db, "bob", "2026-10-28")
        self.assertEqual(thread_cls.call_count, 1)

    def test_is_admin_by_role_and_env(self) -> None:
        self.db.upsert_user("alice")
        self.db.upsert_user("carol")
        self.assertFalse(is_admin(self.db, "carol"))
        with self.db._connection() as conn:
            conn.execute(
                "UPDATE users SET role = 'admin' WHERE nick_lower = 'alice'"
            )
        self.assertTrue(is_admin(self.db, "alice"))

        with patch(
            "app.db.settings", SimpleNamespace(admin_nicks=("Boss",))
        ):
            self.assertTrue(is_admin(self.db, "boss"))
            self.assertTrue(is_admin(self.db, "BOSS"))
            self.assertFalse(is_admin(self.db, "carol"))


class AdminCohortApiTests(unittest.TestCase):
    """GET /api/admin/cohort(.csv): 403 для обычных, цифры для админа."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        self.today = timeutil.today()
        self.start_day = timeutil.dates_back(30, self.today)[0]
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user("alice")
            db.upsert_user("boss")
            db.set_current_user("alice")
            db.save_rating_snapshot("alice", self.start_day, 1500)
            db.save_rating_snapshot("alice", self.today, 1540)
            db.upsert_daily_log(
                "alice", self.today, minutes=40, tasks_done=4, tasks_total=5
            )

    def test_non_admin_forbidden(self) -> None:
        self.assertEqual(
            self.client.get("/api/admin/cohort").status_code, 403
        )
        self.assertEqual(
            self.client.get("/api/admin/cohort.csv").status_code, 403
        )

    def test_admin_cohort_and_csv(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            with db._connection() as conn:
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE nick_lower = 'boss'"
                )
            db.set_current_user("boss")

        resp = self.client.get("/api/admin/cohort?days=30")
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["days"], 30)
        alice = next(u for u in payload["users"] if u["nick"] == "alice")
        self.assertEqual(alice["rating_start"], 1500)
        self.assertEqual(alice["rating_end"], 1540)
        self.assertEqual(alice["rating_delta"], 40)
        self.assertTrue(alice["improved_rating"])
        self.assertTrue(alice["improved"])
        self.assertEqual(alice["days_active"], 1)
        self.assertEqual(alice["minutes_total"], 40)
        self.assertEqual(alice["completion_pct"], 80.0)
        self.assertGreaterEqual(payload["summary"]["improved"], 1)
        self.assertEqual(
            payload["summary"]["improved_by_rating"], 1
        )

        csv_resp = self.client.get("/api/admin/cohort.csv?days=30")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("text/csv", csv_resp.headers["content-type"])
        text = csv_resp.text
        self.assertTrue(text.startswith("nick,rating_start,rating_end"))
        self.assertIn("alice,1500,1540,40", text)


if __name__ == "__main__":
    unittest.main()
