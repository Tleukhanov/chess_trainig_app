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