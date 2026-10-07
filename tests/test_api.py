"""Тесты веб-API (FastAPI TestClient).

Бэкенд не трогает реальную БД: create_api(db_path=..., data_dir=...)
получает временные пути. Движок и сеть не запускаются.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import chess

from app.api import create_api
from app.db import Database
from app.games import Game


def mkgame(id: str) -> Game:
    return Game(
        id=id,
        white={"name": "A"},
        black={"name": "B"},
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner=None,
        opening="Sicilian Defense",
        eco="B20",
        moves=["e4"],
        clocks=[],
        user_color="black",
        opponent="B",
        user_rating=2100,
        user_rating_diff=0,
        result_for_user="loss",
    )


def mkgame_moves(id: str, moves: list[str], **overrides) -> Game:
    """Партия из mkgame с заменёнными ходами (Game frozen, строим заново)."""
    base = mkgame(id)
    kwargs = dict(
        id=base.id,
        rated=base.rated,
        speed=base.speed,
        created_at=base.created_at,
        status=base.status,
        winner=base.winner,
        white=base.white,
        black=base.black,
        opening=base.opening,
        eco=base.eco,
        moves=moves,
        clocks=base.clocks,
        user_color=base.user_color,
        opponent=base.opponent,
        user_rating=base.user_rating,
        user_rating_diff=base.user_rating_diff,
        result_for_user=base.result_for_user,
    )
    kwargs.update(overrides)
    return Game(**kwargs)


class ApiTestBase(unittest.TestCase):
    """Базовый класс: временная БД + TestClient."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)

    def _seed_user(self, nick: str, prefix: str) -> None:
        """Одна проанализированная партия."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame(f"{prefix}1")], nick)
            db.save_analysis(f"{prefix}1", {"summary": []}, depth=8)

    def _set_current(self, nick: str) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user(nick)


class UserApiTests(ApiTestBase):
    """Тесты /api/user/* и /api/users."""

    def test_current_user_404_when_none(self):
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 404)

    def test_add_user_sets_current(self):
        resp = self.client.post("/api/user/add", data={"nick": "Petya", "fide": "42"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["nick"], "Petya")
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["nick"], "Petya")

    def test_add_user_invalid_nick(self):
        resp = self.client.post("/api/user/add", data={"nick": "x", "fide": ""})
        self.assertEqual(resp.status_code, 400)

    def test_switch_user(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": ""})
        self.client.post("/api/user/add", data={"nick": "bob", "fide": ""})
        resp = self.client.post("/api/user/switch", data={"nick": "bob"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.json()["user"]["nick"], "bob")

    def test_list_users(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": ""})
        self.client.post("/api/user/add", data={"nick": "bob", "fide": ""})
        resp = self.client.get("/api/users")
        self.assertEqual(resp.status_code, 200)
        users = resp.json()["users"]
        self.assertEqual(len(users), 2)


class DataApiTests(ApiTestBase):
    """Тесты /api/overview, /api/plan, /api/progress, /api/drills, /api/report."""

    def setUp(self) -> None:
        super().setUp()
        self._seed_user("tester", "t")
        self._set_current("tester")

    def test_overview(self):
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games"], 1)
        self.assertIn("branches", data)
        self.assertIn("openings", data)

    def test_plan(self):
        resp = self.client.get("/api/plan")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games"], 1)
        self.assertIn("repertoire", data)
        self.assertIn("patterns", data)

    def test_progress(self):
        resp = self.client.get("/api/progress?windows=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games"], 1)
        self.assertIn("windows_rows", data)

    def test_drills(self):
        resp = self.client.get("/api/drills")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertIn("summary", data)

    def test_report(self):
        resp = self.client.get("/api/report")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games_analyzed"], 1)
        self.assertIn("score", data)
        self.assertIn("total", data)

    def test_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 404)


class CoachApiTests(ApiTestBase):
    """Тесты /api/coach/run и /api/jobs/{id}."""

    def setUp(self) -> None:
        super().setUp()
        self._seed_user("tester", "t")
        self._set_current("tester")

    def test_coach_run_creates_job(self):
        resp = self.client.post("/api/coach/run", data={"max": "10", "perf": "rapid", "depth": "8", "multipv": "1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("job_id", data)
        job_id = data["job_id"]
        resp = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        job = resp.json()
        self.assertEqual(job["id"], job_id)
        self.assertIn(job["status"], ("running", "done", "error"))

    def test_job_not_found(self):
        resp = self.client.get("/api/jobs/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_coach_run_no_user(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.post("/api/coach/run", data={"max": "10", "perf": "rapid", "depth": "8", "multipv": "1"})
        self.assertEqual(resp.status_code, 404)


class GameApiTests(ApiTestBase):
    """Тесты /api/games и /api/games/{id} (список + разбор партии)."""

    def setUp(self) -> None:
        super().setUp()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame("g1")], "tester")
            db.save_analysis("g1", {"summary": [], "moves": []}, depth=8)
            db.save_games(
                [mkgame_moves("g2", ["e4", "e5", "Nf3"], created_at=1)],
                "tester",
            )
            db.set_current_user("tester")

    def test_list_games(self):
        resp = self.client.get("/api/games?limit=100")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["games"]), 2)
        g1 = data["games"][0]
        self.assertEqual(g1["id"], "g1")
        self.assertTrue(g1["analyzed"])
        self.assertEqual(g1["opponent"], "B")
        self.assertEqual(g1["user_color"], "black")
        self.assertEqual(g1["result_for_user"], "loss")
        self.assertEqual(g1["moves"], 1)
        self.assertEqual(g1["speed"], "rapid")
        self.assertTrue(g1["rated"])
        g2 = data["games"][1]
        self.assertEqual(g2["id"], "g2")
        self.assertFalse(g2["analyzed"])
        self.assertEqual(g2["moves"], 3)

    def test_list_games_empty(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("nobody")
        resp = self.client.get("/api/games?limit=100")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["games"], [])

    def test_game_detail_fens(self):
        resp = self.client.get("/api/games/g2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["game"]["id"], "g2")
        self.assertIsNone(data["analysis"])
        self.assertEqual(len(data["fens"]), 4)  # 1 + 3 хода
        self.assertEqual(data["fens"][0], chess.STARTING_FEN)
        board = chess.Board(data["fens"][-1])
        self.assertTrue(board.is_valid())
        self.assertEqual(data["game"]["moves"], ["e4", "e5", "Nf3"])

    def test_game_detail_with_analysis(self):
        resp = self.client.get("/api/games/g1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNotNone(data["analysis"])
        self.assertEqual(len(data["fens"]), 2)

    def test_game_not_found(self):
        resp = self.client.get("/api/games/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_list_games_no_user(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.get("/api/games")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/games/g1")
        self.assertEqual(resp.status_code, 404)


class FrontendTests(ApiTestBase):
    """Тесты статического фронтенда."""

    def test_index_html_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))

    def test_static_css_served(self):
        resp = self.client.get("/style.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/css", resp.headers.get("content-type", ""))

    def test_static_js_served(self):
        resp = self.client.get("/app.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()