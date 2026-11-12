"""Тесты Agent E: refresh устаревших анализов и история повторений задач.

Покрывает:
- предикат ``analysis_needs_refresh`` (нет ``themes`` у ходов игрока);
- ``needs_refresh`` в отчёте (count/list, безопасный перенос старого кеша);
- POST /api/report/refresh (подтверждение, успех, ошибка движка,
  общий с другим игроком кеш, лимиты);
- взвешенный выбор задачи дня (fresh drop/recency weighting);
- запись попыток (per-position), K-дневное окно подавления решённых
  позиций, явный запрос повтора, метка «попытка N»;
- build_tasks протаскивает номер попытки в задачу дня.

Синтетические партии/анализы; движок и сеть не запускаются.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.analyzer import analysis_needs_refresh
from app.api import (
    _SESSION_COOKIE,
    _load_task_history,
    _pick_daily_task,
    _record_task_attempt,
    _suppressed_task_keys,
    _task_repeat_window_days,
    _task_weight,
    create_api,
)
from app.db import Database
from app.dayplan import build_tasks
from app.games import Game
from app.report import build_report, format_report
from app.timeutil import today as _today_app


def mkgame(
    id: str,
    *,
    user_color: str = "white",
    created_at: int = 1_700_000_000_000,
    moves: list[str] | None = None,
    opening: str = "Sicilian Defense",
    eco: str = "B20",
) -> Game:
    return Game(
        id=id,
        white={"name": "me"},
        black={"name": "opponent"},
        rated=True,
        speed="rapid",
        created_at=created_at,
        status="win",
        winner="white",
        opening=opening,
        eco=eco,
        moves=list(moves if moves is not None else ["e4", "e5", "Nf3"]),
        clocks=[],
        user_color=user_color,
        opponent="opponent",
        user_rating=2000,
        user_rating_diff=0,
        result_for_user="win",
    )


def _move(san: str, *, drop: float, classification: str, best: str | None,
          themes: dict | None = "auto", ply: int = 0) -> dict:
    move = {
        "san": san,
        "classification": classification,
        "drop": drop,
        "win_before": 60.0,
        "win_after": 60.0 - drop,
        "best_move_san": best,
        "best_line": [],
        "played_line": [],
    }
    if themes == "auto":
        themes = {
            "theme_opportunities": [],
            "theme_used": [],
            "confidence": {},
            "drop": drop,
        }
    if themes is not None:
        move["themes"] = themes
    return move


def stale_analysis(game_id: str, *, avg_win_loss: float = 3.3) -> dict:
    """Старый кеш: нет поля themes ни у одного хода."""
    return {
        "game_id": game_id,
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "opponent",
        "avg_win_loss": avg_win_loss,
        "avg_win_before": 55.0,
        "blunders": [0],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": [
            _move("e4", drop=25.0, classification="blunder", best="Nf3", themes=None),
            _move("e5", drop=0.0, classification="good", best=None, themes=None),
            _move("Nf3", drop=0.0, classification="good", best=None, themes=None),
        ],
    }


def fresh_analysis(game_id: str, *, avg_win_loss: float = 1.1) -> dict:
    analysis = stale_analysis(game_id, avg_win_loss=avg_win_loss)
    for move in analysis["moves"]:
        move["themes"] = {
            "theme_opportunities": [],
            "theme_used": [],
            "confidence": {},
            "drop": move["drop"],
        }
    return analysis


class NeedsRefreshPredicateTests(unittest.TestCase):
    def test_missing_themes_needs_refresh(self) -> None:
        self.assertTrue(analysis_needs_refresh(stale_analysis("g1")))

    def test_present_themes_are_fresh(self) -> None:
        self.assertFalse(analysis_needs_refresh(fresh_analysis("g1")))

    def test_opponent_moves_may_lack_themes(self) -> None:
        # Только ходы игрока решают; ходы соперника без тем не делают кеш устаревшим.
        analysis = fresh_analysis("g1")
        analysis["moves"][1].pop("themes")
        self.assertFalse(analysis_needs_refresh(analysis))

    def test_any_user_move_missing_themes_flags(self) -> None:
        analysis = fresh_analysis("g1")
        analysis["moves"][2].pop("themes")  # ход игрока (белые, ply=2)
        self.assertTrue(analysis_needs_refresh(analysis))

    def test_garbage_does_not_crash(self) -> None:
        for junk in (None, {}, {"moves": "nope"}, {"moves": []}, "x", 42):
            self.assertFalse(analysis_needs_refresh(junk))
        # Партии без ходов игрока (нечётное число полуходов не проблема)
        self.assertFalse(analysis_needs_refresh({"moves": [{"san": "e4"}], "user_color": "black"}))


class NeedsRefreshReportTests(unittest.TestCase):
    def _pairs(self, specs):
        pairs = []
        for game_id, analysis in specs:
            game = mkgame(game_id)
            pairs.append((game, analysis))
        return pairs

    def test_report_counts_and_lists_stale(self) -> None:
        pairs = self._pairs([
            ("old1", stale_analysis("old1")),
            ("new1", fresh_analysis("new1")),
            ("old2", stale_analysis("old2")),
        ])
        report = build_report(pairs, "tester")
        needs = report["needs_refresh"]
        self.assertEqual(needs["count"], 2)
        self.assertEqual(needs["total"], 3)
        self.assertEqual(sorted(needs["game_ids"]), ["old1", "old2"])

    def test_format_report_mentions_stale_state(self) -> None:
        pairs = self._pairs([("old1", stale_analysis("old1"))])
        text = format_report(build_report(pairs, "tester"))
        self.assertIn("Устаревшие анализы", text)
        pairs_fresh = self._pairs([("new1", fresh_analysis("new1"))])
        text_fresh = format_report(build_report(pairs_fresh, "tester"))
        self.assertNotIn("Устаревшие анализы", text_fresh)

    def test_format_report_old_cache_line(self) -> None:
        pairs = self._pairs([("old1", stale_analysis("old1"))])
        text = format_report(build_report(pairs, "tester"))
        self.assertIn("«Анализ»", text)


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        rating_patch = patch("app.api._RATING_SNAPSHOT_ENABLED", False)
        rating_patch.start()
        self.addCleanup(rating_patch.stop)

    def _set_current(self, nick: str) -> None:
        token = secrets.token_hex(32)
        with Database(self.db_path) as db:
            db.init_db()
            rec = db.get_user_record(nick)
            if not rec:
                db.upsert_user(nick)
                rec = db.get_user_record(nick)
            db.set_current_user(nick)
            canonical = rec["nick"] if rec else nick
            db.create_session(canonical, token, days=30)
        self.client.cookies.set(_SESSION_COOKIE, token)

    def _seed_analyzed(self, nick: str, game: Game, analysis: dict) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([game], nick)
            db.save_analysis(game.id, analysis, depth=8)


class ReportRefreshApiTests(ApiTestBase):
    class FakeRefreshEngine:
        fail_on: set[str] = set()
        calls: list[str] = []

        def __init__(self, depth: int = 10, multipv: int = 1, time_ms=None, **_: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def analyze_game(self, game):
            type(self).calls.append(game.id)
            if game.id in type(self).fail_on:
                raise RuntimeError("engine boom")
            fake = MagicMock()
            fake.summary.return_value = fresh_analysis(game.id)
            return fake

    def setUp(self) -> None:
        super().setUp()
        type(self).FakeRefreshEngine.fail_on = set()
        type(self).FakeRefreshEngine.calls = []
        self._set_current("tester")
        self._seed_analyzed("tester", mkgame("stale1"), stale_analysis("stale1"))
        self._seed_analyzed("tester", mkgame("fresh1"), fresh_analysis("fresh1"))

    def test_confirm_required(self) -> None:
        resp = self.client.post("/api/report/refresh", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/report/refresh", json={"confirm": False})
        self.assertEqual(resp.status_code, 400)

    def test_refresh_replaces_only_after_success(self) -> None:
        with patch.object(api_module, "Engine", self.FakeRefreshEngine):
            resp = self.client.post("/api/report/refresh", json={"confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertIn("stale1", payload["refreshed"])
        self.assertNotIn("fresh1", payload["refreshed"])  # свежий анализ не трогаем
        self.assertEqual(payload["failed"], [])
        self.assertEqual(payload["needs_refresh"]["count"], 0)
        with Database(self.db_path) as db:
            db.init_db()
            served = db.get_analysis("stale1")
            self.assertIsNotNone(served)
            self.assertEqual(served["avg_win_loss"], 1.1)  # новый кеш

    def test_failed_refresh_keeps_old_analysis(self) -> None:
        type(self).FakeRefreshEngine.fail_on = {"stale1"}
        with patch.object(api_module, "Engine", self.FakeRefreshEngine):
            resp = self.client.post("/api/report/refresh", json={"confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["refreshed"], [])
        self.assertEqual([f["game_id"] for f in payload["failed"]], ["stale1"])
        self.assertIn("engine boom", payload["failed"][0]["error"])
        # Старый анализ цел — кеш не повреждён.
        with Database(self.db_path) as db:
            db.init_db()
            served = db.get_analysis("stale1")
            self.assertEqual(served["avg_win_loss"], 3.3)
        self.assertEqual(payload["needs_refresh"]["count"], 1)

    def test_foreign_copy_is_skipped(self) -> None:
        # Другой игрок тоже имеет копию партии stale1: общий анализ не перезаписываем.
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame("stale1")], "someoneelse")
        with patch.object(api_module, "Engine", self.FakeRefreshEngine):
            resp = self.client.post("/api/report/refresh", json={"confirm": True})
        payload = resp.json()
        self.assertEqual(payload["refreshed"], [])
        self.assertEqual([s["game_id"] for s in payload["skipped"]], ["stale1"])
        self.assertNotIn("stale1", type(self).FakeRefreshEngine.calls)

    def test_explicit_game_ids_and_limit(self) -> None:
        with patch.object(api_module, "Engine", self.FakeRefreshEngine):
            resp = self.client.post(
                "/api/report/refresh",
                json={"confirm": True, "game_ids": ["fresh1", "missing"], "limit": 5},
            )
        payload = resp.json()
        self.assertEqual(payload["refreshed"], ["fresh1"])
        self.assertEqual(payload["not_found"], ["missing"])

    def test_report_endpoint_includes_needs_refresh(self) -> None:
        resp = self.client.get("/api/report")
        self.assertEqual(resp.status_code, 200)
        needs = resp.json()["needs_refresh"]
        self.assertEqual(needs["count"], 1)
        self.assertEqual(needs["total"], 2)
        self.assertEqual(needs["game_ids"], ["stale1"])


_RICH_PLAN = {
    "repertoire": {"weak_openings": []},
    "patterns": {"motifs": [], "phases": [], "total_bad": 0},
    "humanity": {"unnatural": 0, "total": 0},
}


def _task_payload_stub(**overrides) -> dict:
    task = {
        "task_key": "g1|0",
        "game_id": "g1",
        "ply": 0,
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "classification": "blunder",
        "drop": 25.0,
        "opening": "Sicilian Defense",
        "eco": "B20",
        "user_color": "white",
        "done": False,
    }
    task.update(overrides)
    return task


class BuildTasksAttemptTests(unittest.TestCase):
    def test_attempt_is_propagated(self) -> None:
        tasks = build_tasks({}, _task_payload_stub(attempt=3), "2026-11-12")
        task = next(t for t in tasks if t["kind"] == "task")
        self.assertEqual(task["attempt"], 3)
        self.assertEqual(task["action"]["attempt"], 3)
        self.assertIn("попытка 3", task["why"])

    def test_first_attempt_adds_no_attempt_note(self) -> None:
        tasks = build_tasks({}, _task_payload_stub(attempt=1), "2026-11-12")
        task = next(t for t in tasks if t["kind"] == "task")
        self.assertEqual(task["attempt"], 1)
        self.assertNotIn("попытка 1", task["why"])

    def test_missing_attempt_is_tolerated(self) -> None:
        tasks = build_tasks({}, _task_payload_stub(), "2026-11-12")
        task = next(t for t in tasks if t["kind"] == "task")
        self.assertNotIn("attempt", task)
        self.assertNotIn("attempt", task["action"])


class TaskWeightTests(unittest.TestCase):
    def _item(self, drop: float, created_at: int) -> dict:
        return {
            "created_at": created_at,
            "move": {"drop": drop},
        }

    def test_fresh_big_drop_weighs_more(self) -> None:
        now = 1_800_000_000_000
        fresh_big = _task_weight(self._item(40.0, now), now_ms=now)
        old_big = _task_weight(self._item(40.0, now - 120 * 86_400_000), now_ms=now)
        fresh_small = _task_weight(self._item(5.0, now), now_ms=now)
        self.assertGreater(fresh_big, fresh_small)
        self.assertGreater(fresh_big, old_big)
        self.assertGreater(_task_weight(self._item(5.0, now - 120 * 86_400_000), now), 0.0)

    def test_missing_drop_defaults_and_garbage_created(self) -> None:
        weight = _task_weight({"created_at": "junk", "move": {}}, now_ms=1_800_000_000_000)
        self.assertGreater(weight, 0.0)


class SuppressionPredicateTests(unittest.TestCase):
    def test_window_boundary(self) -> None:
        today = "2026-11-12"
        history = [
            {"task_key": "a", "date": "2026-11-12", "correct": True, "attempt": 1, "ts": 0},
            {"task_key": "b", "date": "2026-11-06", "correct": True, "attempt": 1, "ts": 0},
            {"task_key": "c", "date": "2026-11-05", "correct": True, "attempt": 1, "ts": 0},
            {"task_key": "d", "date": "2026-11-12", "correct": False, "attempt": 1, "ts": 0},
        ]
        suppressed = _suppressed_task_keys(history, today, 7)
        self.assertIn("a", suppressed)   # сегодня — внутри окна
        self.assertIn("b", suppressed)   # 6 дней — внутри окна
        self.assertNotIn("c", suppressed)  # ровно K дней — окно истекло
        self.assertNotIn("d", suppressed)  # неудачная попытка не прячет позицию

    def test_window_zero_disables(self) -> None:
        history = [{"task_key": "a", "date": "2026-11-12", "correct": True}]
        self.assertEqual(_suppressed_task_keys(history, "2026-11-12", 0), set())

    def test_window_days_from_env(self) -> None:
        with patch.dict(os.environ, {"TASK_REPEAT_WINDOW_DAYS": "0"}):
            self.assertEqual(_task_repeat_window_days(), 0)
        with patch.dict(os.environ, {"TASK_REPEAT_WINDOW_DAYS": "junk"}):
            self.assertEqual(_task_repeat_window_days(), 7)
        with patch.dict(os.environ, {"TASK_REPEAT_WINDOW_DAYS": "9999"}):
            self.assertEqual(_task_repeat_window_days(), 365)


class AttemptHistoryTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._set_current("tester")

    def _history(self) -> list[dict]:
        with Database(self.db_path) as db:
            db.init_db()
            return _load_task_history(db, "tester")

    def test_attempts_are_counted_per_position(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(_record_task_attempt(db, "tester", "g1|0", "2026-11-12", True), 1)
            self.assertEqual(_record_task_attempt(db, "tester", "g1|0", "2026-11-12", False), 2)
            self.assertEqual(_record_task_attempt(db, "tester", "g2|0", "2026-11-12", True), 1)
            self.assertEqual(_record_task_attempt(db, "tester", "g1|0", "2026-11-12", True), 3)
        history = self._history()
        self.assertEqual([item["attempt"] for item in history], [1, 2, 1, 3])
        self.assertEqual([item["correct"] for item in history], [True, False, True, True])

    def test_history_is_capped_and_old_attempts_pruned(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            with patch.object(api_module, "_TASK_HISTORY_CAP", 3):
                for _ in range(5):
                    _record_task_attempt(db, "tester", "g1|0", "2026-11-12", True)
            raw = db.get_meta("task_history_tester")
        entries = json.loads(raw)
        self.assertEqual(len(entries), 3)
        self.assertEqual([item["attempt"] for item in entries], [3, 4, 5])

    def test_corrupt_history_is_tolerated(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.set_meta("task_history_tester", "{not-json")
            self.assertEqual(_load_task_history(db, "tester"), [])
            db.set_meta("task_history_tester", json.dumps({"not": "a-list"}))
            self.assertEqual(_load_task_history(db, "tester"), [])
            db.set_meta("task_history_tester", json.dumps([{"bogus": True}, 7, None]))
            self.assertEqual(_load_task_history(db, "tester"), [])


class TaskPoolApiTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._set_current("tester")
        self._seed_analyzed("tester", mkgame("g1", created_at=1_700_000_000_000), stale_analysis("g1"))
        self._seed_analyzed("tester", mkgame("g2", created_at=1_700_100_000_000), stale_analysis("g2"))

    def test_attempt_starts_at_one(self) -> None:
        resp = self.client.get("/api/task/today")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["attempts"], 0)
        self.assertFalse(payload["solved_before"])
        self.assertIn(payload["task_key"], {"g1|0", "g2|0"})

    def test_correct_answer_records_attempt_and_marks_done(self) -> None:
        resp = self.client.post("/api/task/answer", data={"answer": "Nf3"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["correct"])
        self.assertEqual(payload["attempt"], 1)
        with Database(self.db_path) as db:
            db.init_db()
            history = _load_task_history(db, "tester")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["correct"])

    def test_wrong_answer_records_attempt_but_not_done(self) -> None:
        resp = self.client.post("/api/task/answer", data={"answer": "a3"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertFalse(payload["correct"])
        self.assertEqual(payload["attempt"], 1)

        with Database(self.db_path) as db:
            db.init_db()
            self.assertIsNone(db.get_meta(f"daily_done_{_today_app()}_tester"))
            history = _load_task_history(db, "tester")
        self.assertEqual([item["correct"] for item in history], [False])

    def test_solved_position_is_suppressed_next_days(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            # Решаем g1|0 сегодня корректным ответом.
            _record_task_attempt(db, "tester", "g1|0", "2026-11-12", True)
        with Database(self.db_path) as db:
            db.init_db()
            day = "2026-11-13"
            suppressed = _suppressed_task_keys(_load_task_history(db, "tester"), day, 7)
            self.assertIn("g1|0", suppressed)
            picked = _pick_daily_task(db, "tester", day)
            self.assertEqual(picked["task_key"], "g2|0")
            self.assertEqual(picked["suppressed_count"], 1)
            self.assertFalse(picked["solved_before"])

    def test_explicit_repeat_request_bypasses_window(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            _record_task_attempt(db, "tester", "g1|0", "2026-11-12", True)
            _record_task_attempt(db, "tester", "g2|0", "2026-11-12", True)
            day = "2026-11-13"
            picked = _pick_daily_task(db, "tester", day, repeat=True)
            self.assertTrue(picked["repeat_requested"])
        resp = self.client.get("/api/task/today?repeat=1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["repeat_requested"])

    def test_all_solved_requires_explicit_repeat(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            _record_task_attempt(db, "tester", "g1|0", "2026-11-12", True)
            _record_task_attempt(db, "tester", "g2|0", "2026-11-12", True)
            with self.assertRaises(RuntimeError):
                _pick_daily_task(db, "tester", "2026-11-13")
            picked = _pick_daily_task(db, "tester", "2026-11-13", repeat=True)
            self.assertTrue(picked["repeat_requested"])
            self.assertEqual(picked["attempt"], 2)
            self.assertTrue(picked["solved_before"])
        # Пусто без явного запроса только до первого выбора дня; после repeat=1
        # выбор зафиксирован в meta и отдаётся по стабильности дня.
        day = _today_app()
        with Database(self.db_path) as db:
            db.init_db()
            # попытка номер 2 для каждой позиции (первая была 2026-11-12)
            _record_task_attempt(db, "tester", "g1|0", day, True)
            _record_task_attempt(db, "tester", "g2|0", day, True)
            db.set_meta(f"daily_task_{day}_tester", "")  # сброс зафиксированного выбора
        resp = self.client.get("/api/task/today")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("уже решались", resp.json()["detail"])
        resp = self.client.get("/api/task/today?repeat=1")
        self.assertEqual(resp.status_code, 200)
        repeat_body = resp.json()
        self.assertEqual(repeat_body["attempt"], 3)
        self.assertTrue(repeat_body["solved_before"])
        self.assertTrue(repeat_body["repeat_requested"])
        # После явного запроса повтор сохранён как выбор дня — стабильность.
        resp = self.client.get("/api/task/today")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["task_key"], repeat_body["task_key"])
        self.assertFalse(resp.json()["repeat_requested"])

    def test_today_pick_is_stable_across_calls(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            first = _pick_daily_task(db, "tester", "2026-11-13")
            second = _pick_daily_task(db, "tester", "2026-11-13")
        self.assertEqual(first["task_key"], second["task_key"])

    def test_pick_prefers_fresher_candidate_weight(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            pool = api_module._task_pool(db, "tester")
        by_key = {item["task_key"]: item for item in pool}
        now_ms = 1_700_200_000_000  # вскоре после g2
        self.assertGreater(
            _task_weight(by_key["g2|0"], now_ms=now_ms),
            _task_weight(by_key["g1|0"], now_ms=now_ms),
        )


if __name__ == "__main__":
    unittest.main()
