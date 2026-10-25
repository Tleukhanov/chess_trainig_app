"""Тесты веб-API (FastAPI TestClient).

Бэкенд не трогает реальную БД: create_api(db_path=..., data_dir=...)
получает временные пути. Движок и сеть не запускаются.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import chess

from app.api import create_api, _plan_llm_input, _task_done_key, _today, _JOBS, CoachJob
from app.db import Database
from app.fide import FidePlayer, FideRatings
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

    def _register(
        self,
        nick: str,
        password: str = "abcd",
        lichess: str | None = None,
        fide: str = "",
    ) -> None:
        """Регистрация через /api/auth/register (ставит cookie сессии)."""
        resp = self.client.post(
            "/api/auth/register",
            data={
                "login": nick,
                "password": password,
                "lichess": lichess or nick,
                "fide": fide,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class UserApiTests(ApiTestBase):
    """Тесты /api/user/* и /api/users.

    Legacy-мутации (add/switch/delete) требуют сессию — пользователи
    создаются через /api/auth/register (он же ставит cookie).
    """

    def test_current_user_404_when_none(self):
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 404)

    def test_add_user_sets_current(self):
        self._register("tester")
        resp = self.client.post("/api/user/add", data={"nick": "Petya", "fide": "42"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["nick"], "Petya")
        # add закрепляет Petya в meta; сессия приоритетнее, поэтому проверяем
        # через клиента без cookie (meta-fallback).
        fresh = TestClient(self.app)
        resp = fresh.get("/api/user/current")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["nick"], "Petya")

    def test_add_user_invalid_nick(self):
        self._register("tester")
        resp = self.client.post("/api/user/add", data={"nick": "x", "fide": ""})
        self.assertEqual(resp.status_code, 400)

    def test_legacy_mutations_require_session(self):
        fresh = TestClient(self.app)
        self.assertEqual(
            fresh.post("/api/user/add", data={"nick": "ada"}).status_code, 401
        )
        self.assertEqual(
            fresh.post("/api/user/switch", data={"nick": "ada"}).status_code, 401
        )
        self.assertEqual(
            fresh.post("/api/user/delete", data={"nick": "ada"}).status_code, 401
        )
        # С сессией — работает.
        self._register("ada")
        self.assertEqual(
            self.client.post("/api/user/add", data={"nick": "bob", "fide": ""}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/user/switch", data={"nick": "bob"}).status_code, 200
        )
        self.assertEqual(
            self.client.post("/api/user/delete", data={"nick": "bob"}).status_code, 200
        )

    def test_switch_user(self):
        self._register("ada")
        self._register("bob")
        resp = self.client.post("/api/user/switch", data={"nick": "ada"})
        self.assertEqual(resp.status_code, 200)
        # Сессия приоритетна над meta, поэтому проверяем закреплённого
        # через клиента без cookie (meta-fallback) и напрямую в БД.
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(db.get_current_user()["nick"], "ada")
        fresh = TestClient(self.app)
        resp = fresh.get("/api/user/current")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["nick"], "ada")

    def test_list_users(self):
        self._register("ada")
        self._register("bob")
        resp = self.client.get("/api/users")
        self.assertEqual(resp.status_code, 200)
        users = resp.json()["users"]
        self.assertEqual(len(users), 2)

    def test_users_no_password_hash(self):
        self._register("ada", fide="42")
        users = self.client.get("/api/users").json()["users"]
        self.assertEqual(len(users), 1)
        self.assertNotIn("password_hash", users[0])
        for key in ("nick", "lichess", "fide_id", "games", "analyses", "last_seen_at"):
            self.assertIn(key, users[0])
        self.assertEqual(users[0]["fide_id"], "42")

    def test_current_user_limited_fields(self):
        self._register("ada", fide="42")
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 200)
        user = resp.json()["user"]
        self.assertEqual(set(user), {"nick", "lichess", "fide_id"})
        self.assertNotIn("password_hash", user)
        self.assertEqual(user["nick"], "ada")
        self.assertEqual(user["lichess"], "ada")
        self.assertEqual(user["fide_id"], "42")

    def test_delete_user_leaves_other(self):
        self._register("ada")
        self._register("bob")
        resp = self.client.post("/api/user/delete", data={"nick": "ada"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        users = self.client.get("/api/users").json()["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["nick"], "bob")

    def test_delete_last_user_allowed(self):
        self._register("ada")
        resp = self.client.post("/api/user/delete", data={"nick": "ada"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/users").json()["users"], [])

    def test_delete_current_user_clears_current(self):
        self._register("ada")
        self._register("bob")
        # switch закрепляет bob в meta
        self.assertEqual(
            self.client.post("/api/user/switch", data={"nick": "bob"}).status_code, 200
        )
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(db.get_current_user()["nick"], "bob")
        resp = self.client.post("/api/user/delete", data={"nick": "bob"})
        self.assertEqual(resp.status_code, 200)
        # сессия bob мертва (профиль удалён), meta очищен delete_user
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 404)

    def test_delete_user_removes_games_and_analyses(self):
        self._seed_user("tester", "t")
        self._register("tester")  # claim: профиль уже есть без пароля
        resp = self.client.post("/api/user/delete", data={"nick": "tester"})
        self.assertEqual(resp.status_code, 200)
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(db.get_games("tester"), [])
            self.assertFalse(db.has_analysis("t1"))
            self.assertIsNone(db.get_user("tester"))
        self.assertEqual(self.client.get("/api/user/current").status_code, 404)

    def test_delete_unknown_user_still_ok(self):
        self._register("ada")
        resp = self.client.post("/api/user/delete", data={"nick": "ghost"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_fide_set_updates_profile(self):
        self._register("ada", fide="42")
        resp = self.client.post(
            "/api/user/fide", data={"nick": "ada", "fide_id": "4100000"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["fide_id"], "4100000")
        users = self.client.get("/api/users").json()["users"]
        self.assertEqual(users[0]["fide_id"], "4100000")

    def test_fide_empty_clears_profile(self):
        self._register("ada", fide="42")
        resp = self.client.post(
            "/api/user/fide", data={"nick": "ada", "fide_id": ""}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["fide_id"])
        users = self.client.get("/api/users").json()["users"]
        self.assertIsNone(users[0]["fide_id"])


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


class FakeEngine:
    """Заглушка Stockfish для /api/coach/run: без процесса и реального анализа.

    Журналы ``calls``/``game_ids`` — на классе, тесты их сбрасывают сами.
    ``on_game`` позволяет смоделировать отмену посреди цикла.
    """

    calls: list[tuple[int, int]] = []
    game_ids: list[str] = []
    on_game = None

    def __init__(self, depth: int = 14, multipv: int = 3, **kwargs) -> None:
        type(self).calls.append((depth, multipv))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def analyze_game(self, game):
        type(self).game_ids.append(game.id)
        if type(self).on_game is not None:
            type(self).on_game(game)
        fake = MagicMock()
        fake.summary.return_value = {"game_id": game.id}
        return fake


class CoachApiTests(ApiTestBase):
    """Тесты /api/coach/run и /api/jobs/{id}."""

    def setUp(self) -> None:
        super().setUp()
        _JOBS.clear()
        self._seed_user("tester", "t")
        self._set_current("tester")

    def tearDown(self) -> None:
        deadline = time.time() + 30.0
        while time.time() < deadline:
            running = [
                j for j in _JOBS.values()
                if j.user == "tester" and j.status == "running"
            ]
            if not running:
                break
            time.sleep(0.2)
        _JOBS.clear()

    def _engine_patch(self, path: str):
        """Патчит settings.stockfish_path (pre-flight движка в coach/run)."""
        return patch("app.api.settings", stockfish_path=path)

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        """Опрос /api/jobs/{id}, пока job не перестанет быть running."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.02)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    def _reset_fake_engine(self) -> None:
        FakeEngine.calls = []
        FakeEngine.game_ids = []
        FakeEngine.on_game = None

    def test_coach_run_creates_job(self):
        with tempfile.NamedTemporaryFile() as fh:
            with self._engine_patch(fh.name):
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

    def test_coach_run_validation_400(self):
        self._register("tester")
        bad = [
            {"max": "0"}, {"max": "201"}, {"max": "-5"},
            {"depth": "0"}, {"depth": "31"}, {"depth": "-1"},
            {"multipv": "0"}, {"multipv": "11"}, {"multipv": "-3"},
        ]
        for data in bad:
            with self.subTest(data=data):
                resp = self.client.post("/api/coach/run", data=data)
                self.assertEqual(resp.status_code, 400)

    def test_coach_run_bounds_ok(self):
        self._register("tester")
        with tempfile.NamedTemporaryFile() as fh:
            with self._engine_patch(fh.name):
                resp = self.client.post(
                    "/api/coach/run",
                    data={"max": "200", "depth": "30", "multipv": "10"},
                )
                self.assertEqual(resp.status_code, 200)

    def test_coach_run_no_engine_503(self):
        self._register("tester")
        with self._engine_patch(r"C:\nonexistent\stockfish.exe"):
            resp = self.client.post("/api/coach/run", data={})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("Stockfish", resp.json()["detail"])

    def test_coach_profile_presets(self):
        """profile=quick/standard/deep задаёт depth/multipv; пустой — как передали."""
        self._register("tester")
        cases = [
            ({"profile": "quick"}, (10, 1)),
            ({"profile": "standard"}, (12, 2)),
            ({"profile": "deep"}, (14, 3)),
            ({"depth": "7", "multipv": "2"}, (7, 2)),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                self._reset_fake_engine()
                with tempfile.NamedTemporaryFile() as fh:
                    with self._engine_patch(fh.name), patch(
                        "app.api.Engine", FakeEngine
                    ):
                        resp = self.client.post(
                            "/api/coach/run",
                            data={
                                "max": "10",
                                "cached_only": "on",
                                "refresh": "on",
                                **extra,
                            },
                        )
                        self.assertEqual(resp.status_code, 200, resp.text)
                        job = self._wait_job(resp.json()["job_id"])
                self.assertEqual(job["status"], "done")
                self.assertEqual(FakeEngine.calls, [expected])

    def test_coach_profile_unknown_400(self):
        resp = self.client.post(
            "/api/coach/run", data={"profile": "turbo", "cached_only": "on"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("profile", resp.json()["detail"])
        # Плохой профиль не должен создавать задачу.
        self.assertFalse([j for j in _JOBS.values() if j.status == "running"])

    def test_coach_newest_first_and_partial_results(self):
        """target сортируется по created_at DESC, анализ сохраняется по партиям."""
        self._register("tester")
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves("old", ["e4"], created_at=100),
                    mkgame_moves("newest", ["e4"], created_at=300),
                    mkgame_moves("mid", ["e4"], created_at=200),
                ],
                "tester",
            )
        self._reset_fake_engine()
        with tempfile.NamedTemporaryFile() as fh:
            with self._engine_patch(fh.name), patch("app.api.Engine", FakeEngine):
                resp = self.client.post(
                    "/api/coach/run",
                    data={"max": "10", "cached_only": "on", "refresh": "on"},
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                job_id = resp.json()["job_id"]
                job = self._wait_job(job_id)
        self.assertEqual(job["status"], "done")
        # t1 из setUp имеет created_at=0 — он последний.
        self.assertEqual(FakeEngine.game_ids, ["newest", "mid", "old", "t1"])
        with Database(self.db_path) as db:
            db.init_db()
            for game_id in ("newest", "mid", "old", "t1"):
                self.assertIsNotNone(
                    db.get_analysis(game_id), f"нет анализа для {game_id}"
                )


class JobCancelApiTests(ApiTestBase):
    """POST /api/jobs/{id}/cancel: только владелец, идемпотентность, остановка."""

    def setUp(self) -> None:
        super().setUp()
        _JOBS.clear()

    def tearDown(self) -> None:
        _JOBS.clear()

    def _tokens(self) -> dict[str, str]:
        conn = Database(self.db_path).connect()
        try:
            rows = conn.execute("SELECT nick, token FROM sessions").fetchall()
            return {row["nick"]: row["token"] for row in rows}
        finally:
            conn.close()

    def _as(self, token: str) -> None:
        self.client.cookies.set("ct_session", token)

    def _insert_job(self, nick: str, job_id: str, status: str = "running") -> CoachJob:
        job = CoachJob(id=job_id, user=nick)
        job.status = status
        _JOBS[job_id] = job
        return job

    def test_cancel_own_running_idempotent(self):
        self._register("alice")
        job = self._insert_job("alice", "job-cancel-1")
        resp = self.client.post("/api/jobs/job-cancel-1/cancel")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.finished)
        # Повторная отмена не падает и отдаёт текущий статус.
        resp = self.client.post("/api/jobs/job-cancel-1/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "cancelled"})

    def test_cancel_done_is_idempotent(self):
        self._register("alice")
        self._insert_job("alice", "job-done-1", status="done")
        resp = self.client.post("/api/jobs/job-done-1/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "done"})

    def test_cancel_other_user_404(self):
        self._register("alice")
        self._register("bob")
        tokens = self._tokens()
        job = self._insert_job("alice", "job-alice-1")
        self._as(tokens["bob"])
        resp = self.client.post("/api/jobs/job-alice-1/cancel")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(job.status, "running")

    def test_cancel_stops_analysis_loop(self):
        """Статус cancelled в цикле _run_coach_job останавливает новые партии."""
        self._register("tester")
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves("c1", ["e4"], created_at=10),
                    mkgame_moves("c2", ["e4"], created_at=20),
                    mkgame_moves("c3", ["e4"], created_at=30),
                ],
                "tester",
            )

        def _cancel(game) -> None:
            for job in _JOBS.values():
                if job.status == "running":
                    job.status = "cancelled"

        FakeEngine.calls = []
        FakeEngine.game_ids = []
        FakeEngine.on_game = _cancel
        try:
            with tempfile.NamedTemporaryFile() as fh:
                with patch("app.api.settings", stockfish_path=fh.name), patch(
                    "app.api.Engine", FakeEngine
                ):
                    resp = self.client.post(
                        "/api/coach/run",
                        data={"max": "10", "cached_only": "on", "refresh": "on"},
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    job_id = resp.json()["job_id"]
                    deadline = time.time() + 15.0
                    data = None
                    while time.time() < deadline:
                        data = self.client.get(f"/api/jobs/{job_id}").json()
                        if data["status"] == "cancelled" and data["finished"]:
                            break
                        time.sleep(0.02)
        finally:
            FakeEngine.on_game = None

        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["done"], 1)
        self.assertEqual(len(FakeEngine.game_ids), 1)


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

    def test_broken_moves_json_404(self):
        """Битый moves_json в БД → 404 (JSONDecodeError — ValueError), а не 500."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame("b1")], "tester")
            db.set_current_user("tester")
            with db._connection() as conn:
                conn.execute(
                    "UPDATE games SET moves_json = ? WHERE id = ?", ("{bad json", "b1")
                )
        resp = self.client.get("/api/games")
        self.assertEqual(resp.status_code, 404)

    def test_list_games_no_user(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.get("/api/games")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/games/g1")
        self.assertEqual(resp.status_code, 404)


class JobAccessTests(ApiTestBase):
    """GET /api/jobs/{id}: чужая задача не читается (404), своя — 200.

    Два пользователя с разными сессиями; job создаёт один, читает — другой.
    """

    def _tokens(self) -> dict[str, str]:
        conn = Database(self.db_path).connect()
        try:
            rows = conn.execute("SELECT nick, token FROM sessions").fetchall()
            return {row["nick"]: row["token"] for row in rows}
        finally:
            conn.close()

    def _as(self, token: str) -> None:
        self.client.cookies.set("ct_session", token)

    def test_job_cross_user_404(self):
        self._register("alice")
        self._register("bob")
        tokens = self._tokens()

        # alice создаёт задачу (plan/generate dry_run — без сети и движка)
        self._as(tokens["alice"])
        resp = self.client.post("/api/plan/generate", data={"dry_run": "on"})
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]

        # bob читает чужую задачу → 404
        self._as(tokens["bob"])
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

        # alice читает свою → 200
        self._as(tokens["alice"])
        resp = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], job_id)

        # Без сессии — meta-fallback: закрепляем alice напрямую → 200 как раньше
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("alice")
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 200)


class ToolsApiTests(ApiTestBase):
    """Тесты инструментов: репертуар, соперник, человечность, турнир, FIDE."""

    def setUp(self) -> None:
        super().setUp()
        self._seed_user("tester", "t")
        self._set_current("tester")

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    def _seed_line(self) -> None:
        """Партия с ходами и анализом: белые [e4], чтобы были линии."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves("r1", ["e4", "e5"], user_color="white", result_for_user="win")],
                "tester",
            )
            db.save_analysis(
                "r1",
                {
                    "moves": [
                        {"san": "e4", "classification": "best", "drop": 0.0},
                        {"san": "e5", "classification": "good", "drop": 0.0},
                    ]
                },
                depth=8,
            )

    # --- repertoire ---

    def test_repertoire_lines(self):
        self._seed_line()
        resp = self.client.get("/api/repertoire?color=white&max_depth=16")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games"], 2)  # t1 + r1
        self.assertEqual(data["color"], "white")
        self.assertIn("openings", data)
        self.assertIn("weak", data)
        self.assertEqual(data["weak"]["white"], 0)
        line = data["lines"][0]
        self.assertEqual(line["moves"], ["e4"])
        self.assertEqual(line["count"], 1)
        self.assertEqual(line["ok"], 1)
        self.assertEqual(line["bad"], 0)
        self.assertAlmostEqual(line["path_ok_rate"], 1.0)
        openings = data["openings"]
        self.assertTrue(openings)
        self.assertEqual(openings[0]["opening"], "Sicilian Defense")

    def test_repertoire_both_colors(self):
        resp = self.client.get("/api/repertoire?color=both")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("lines", data)
        for line in data["lines"]:
            self.assertIn(line["color"], ("white", "black"))
        self.assertIn("white", data["weak"])
        self.assertIn("black", data["weak"])

    def test_repertoire_bad_color_400(self):
        resp = self.client.get("/api/repertoire?color=green")
        self.assertEqual(resp.status_code, 400)

    def test_repertoire_bad_depth_400(self):
        resp = self.client.get("/api/repertoire?max_depth=0")
        self.assertEqual(resp.status_code, 400)

    def test_repertoire_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.get("/api/repertoire")
        self.assertEqual(resp.status_code, 404)

    def test_repertoire_no_data_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("empty")
        resp = self.client.get("/api/repertoire")
        self.assertEqual(resp.status_code, 404)

    # --- prepare (лист подготовки к сопернику) ---

    def test_prepare_run_creates_job(self):
        opp_game = mkgame_moves(
            "opp1",
            ["e4", "e5"],
            user_color="white",
            result_for_user="win",
            opponent="rival",
        )
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([opp_game], "rival")
            db.save_analysis(
                "opp1",
                {
                    "moves": [
                        {"san": "e4", "classification": "best", "drop": 0.0},
                        {"san": "e5", "classification": "good", "drop": 0.0},
                    ]
                },
                depth=8,
            )
        with patch("app.api.fetch_user_games", return_value=[opp_game]) as fetch:
            resp = self.client.post(
                "/api/prepare/run",
                data={"opponent": "rival", "max": "5", "perf": "rapid"},
            )
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
            fetch.assert_called_once()

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertIn("profile", job["result"])
        self.assertIn("confrontations", job["result"])
        profile = job["result"]["profile"]
        self.assertNotIn("progress", profile)
        self.assertEqual(profile["games"], 1)
        self.assertEqual(profile["opponent"], "rival")
        self.assertIsInstance(job["result"]["confrontations"], list)

    def test_prepare_run_empty_opponent_400(self):
        resp = self.client.post("/api/prepare/run", data={"opponent": ""})
        self.assertEqual(resp.status_code, 400)

    def test_prepare_run_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.post("/api/prepare/run", data={"opponent": "rival"})
        self.assertEqual(resp.status_code, 404)

    # --- humanize ---

    def test_humanize_no_cache_404(self):
        resp = self.client.get("/api/humanize")
        self.assertEqual(resp.status_code, 404)

    def test_humanize_cache(self):
        path = self.data_dir / "tester" / "humanity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "total_bad": 2,
                    "verdicts": {"natural": 1, "borderline": 1, "unnatural": 0, "no-data": 0},
                    "avg_beta": -1.5,
                    "items": [
                        {"game_id": "g1", "ply": 4, "san": "Nf3", "classification": "mistake",
                         "drop": 12.0, "beta": -2.0, "verdict": "natural"},
                        {"game_id": "g2", "ply": 6, "san": "Qh5", "classification": "blunder",
                         "drop": 30.0, "beta": -4.0, "verdict": "unnatural"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        resp = self.client.get("/api/humanize")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["natural"], 1)
        self.assertEqual(data["summary"]["unnatural"], 0)
        self.assertEqual(data["avg_beta"], -1.5)
        self.assertEqual(len(data["items"]), 2)

    def test_humanize_refresh_job_without_games_errors(self):
        # у ghost нет партий — job падает до запуска движка/сети
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("ghost")
        resp = self.client.get("/api/humanize?refresh=1")
        self.assertEqual(resp.status_code, 200)
        job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("проанализированных", job["error"])

    def test_humanize_refresh_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.get("/api/humanize?refresh=1")
        self.assertEqual(resp.status_code, 404)

    # --- tournament ---

    def test_tournament_ok(self):
        resp = self.client.post(
            "/api/tournament/run",
            data={
                "games": "Ivan:white:win:2100\nPetrov:black:draw:2000",
                "initial": "2000",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["games"], 2)
        self.assertEqual(data["points"], 1.5)
        self.assertIsNotNone(data["performance"])
        self.assertIsInstance(data["delta"], int)
        self.assertEqual(len(data["rows"]), 2)
        row = data["rows"][0]
        self.assertEqual(row["opponent"], "Ivan")
        self.assertEqual(row["color"], "белые")
        self.assertEqual(row["result"], "победа")
        self.assertEqual(row["points"], 1.0)

    def test_tournament_empty_400(self):
        resp = self.client.post("/api/tournament/run", data={"games": ""})
        self.assertEqual(resp.status_code, 400)

    def test_tournament_missing_field_400(self):
        resp = self.client.post("/api/tournament/run", data={})
        self.assertIn(resp.status_code, (400, 422))

    def test_tournament_bad_color_400(self):
        resp = self.client.post("/api/tournament/run", data={"games": "X:green:win:1500"})
        self.assertEqual(resp.status_code, 400)

    def test_tournament_bad_initial_400(self):
        resp = self.client.post(
            "/api/tournament/run",
            data={"games": "Ivan:white:win:2100", "initial": "abc"},
        )
        self.assertEqual(resp.status_code, 400)

    # --- fide ---

    def test_fide_empty_id_400(self):
        resp = self.client.post("/api/fide/run", data={"id": ""})
        self.assertEqual(resp.status_code, 400)

    def test_fide_run_creates_job(self):
        player = FidePlayer(
            id="42",
            name="Test Player",
            federation="RUS",
            birth_year=1990,
            standard=2000,
            rapid=1900,
            blitz=1800,
        )
        ratings = FideRatings(
            history={
                "standard": [(202301, 2000), (202302, 2010), (202303, 2020), (202304, 2030)]
            }
        )
        with patch("app.api.fetch_fide_player", return_value=player) as fp, \
                patch("app.api.fetch_fide_ratings", return_value=ratings) as fr:
            resp = self.client.post("/api/fide/run", data={"id": "42", "windows": "2"})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
            fp.assert_called_once()
            fr.assert_called_once()

        self.assertEqual(job["status"], "done", job.get("error"))
        result = job["result"]
        self.assertEqual(result["id"], "42")
        self.assertEqual(result["player"]["name"], "Test Player")
        self.assertEqual(result["player"]["standard"], 2000)
        controls = result["trend"]["controls"]
        self.assertEqual(len(controls), 3)
        standard = controls[0]
        self.assertEqual(standard["key"], "standard")
        self.assertEqual(len(standard["rows"]), 2)
        self.assertEqual(standard["rows"][0]["delta"], 10)
        self.assertEqual(standard["delta"], 30)
        self.assertEqual(standard["direction"], "up")


class LlmToolApiTests(ApiTestBase):
    """Тесты LLM-инструментов: /api/review/run и /api/mentor/run.

    Сеть не используется: либо мок функции вызова LLM, либо dry-run.
    """

    def setUp(self) -> None:
        super().setUp()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves("rv1", ["e4", "e5", "Nf3", "Nf6"])], "tester"
            )
            db.save_analysis(
                "rv1",
                {
                    "user_color": "black",
                    "result_for_user": "loss",
                    "avg_win_loss": 12.5,
                    "avg_win_before": 70.0,
                    "blunders": [3],
                    "mistakes": [],
                    "missed_wins": [],
                    "moves": [
                        {"san": "e4", "classification": "good", "drop": 0.0,
                         "win_before": 55.0, "win_after": 55.0},
                        {"san": "e5", "classification": "best", "drop": 0.0,
                         "win_before": 45.0, "win_after": 45.0},
                        {"san": "Nf3", "classification": "good", "drop": 0.0,
                         "win_before": 50.0, "win_after": 50.0},
                        {"san": "Nf6", "classification": "blunder", "drop": 25.0,
                         "win_before": 60.0, "win_after": 35.0},
                    ],
                },
                depth=8,
            )
            db.set_current_user("tester")

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    # --- review ---

    def test_review_run_creates_job(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.run_coach",
                      return_value=({3: "не увидел угрозу на f6"}, "потерял инициативу")):
            resp = self.client.post("/api/review/run", data={"max": "3"})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])

        self.assertEqual(job["status"], "done", job.get("error"))
        result = job["result"]
        self.assertEqual(len(result["games"]), 1)
        game = result["games"][0]
        self.assertEqual(game["game_id"], "rv1")
        self.assertEqual(game["result"], "loss")
        self.assertEqual(game["white"], "A")
        self.assertEqual(game["black"], "B")
        self.assertEqual(game["avg_win_loss"], 12.5)
        self.assertEqual(len(game["moments"]), 1)
        moment = game["moments"][0]
        self.assertEqual(moment["ply"], 4)
        self.assertEqual(moment["san"], "Nf6")
        self.assertEqual(moment["classification"], "blunder")
        self.assertEqual(moment["drop"], 25.0)
        self.assertEqual(moment["text"], "не увидел угрозу на f6")
        self.assertEqual(game["summary"], "потерял инициативу")

    def test_review_dry_run_no_llm(self):
        resp = self.client.post("/api/review/run", data={"max": "3", "dry_run": "on"})
        self.assertEqual(resp.status_code, 200)
        job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        result = job["result"]
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["games"]), 1)
        self.assertTrue(result["prompts"])
        user_msg = result["prompts"][1]
        self.assertEqual(user_msg["role"], "user")
        self.assertIn("Момент 1", user_msg["content"])

    def test_review_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.post("/api/review/run", data={"max": "3"})
        self.assertEqual(resp.status_code, 404)

    def test_review_no_analyzed_games_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("ghost")
        resp = self.client.post("/api/review/run", data={"max": "3"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("проанализированных", resp.json()["detail"])

    def test_review_no_llm_key_job_error(self):
        with patch.dict(os.environ, {"LLM_API_KEY": ""}):
            resp = self.client.post("/api/review/run", data={"max": "3"})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("LLM_API_KEY", job["error"])

    # --- mentor ---

    def test_mentor_run_creates_job(self):
        import textwrap
        reply = textwrap.dedent("""\
            1) Дебют — закрепить e4-e5.
            2) Узоры — вилка на f6.
            Итог через месяц: ...
        """)
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.run_mentor", return_value=reply) as rm:
            resp = self.client.post(
                "/api/mentor/run", data={"notes": "Хочу эндшпиль"}
            )
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
            rm.assert_called_once()

        self.assertEqual(job["status"], "done", job.get("error"))
        result = job["result"]
        self.assertEqual(result["user"], "tester")
        self.assertEqual(result["notes"], "Хочу эндшпиль")
        self.assertEqual(result["reply"], reply.rstrip())
        self.assertIsNotNone(result.get("model"))

    def test_mentor_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.post("/api/mentor/run", data={"notes": "x"})
        self.assertEqual(resp.status_code, 404)


class TaskApiTests(ApiTestBase):
    """Тесты «Задачи дня»: /api/task/today, /answer, /explain.

    Сид повторяет формат analysis из analyzer.GameAnalysis.summary():
    moves[i] = {san, win_before, win_after, drop, classification,
    best_move_san, best_line, ...}, индексы blunders/mistakes — ply.
    Движок и сеть не запускаются.
    """

    @staticmethod
    def _move(
        san: str,
        classification: str,
        drop: float,
        win_before: float,
        win_after: float,
        best: str,
        best_line: list[str] | None = None,
    ) -> dict:
        return {
            "san": san,
            "before": {"cp": 20.0, "mate": None},
            "after": {"cp": 5.0, "mate": None},
            "win_before": win_before,
            "win_after": win_after,
            "drop": drop,
            "classification": classification,
            "best_move_san": best,
            "best_eval": {"cp": 20.0, "mate": None},
            "best_win": win_before,
            "clock_used": None,
            "time_pressure": None,
            "best_line": best_line or [],
            "played_line": [],
        }

    def _seed_tasks(self) -> None:
        """Две партии с ошибками пользователя (белые) — пул из 2 кандидатов."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "tk1",
                        ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
                        user_color="white",
                        result_for_user="loss",
                        opponent="Rival1",
                        created_at=100,
                    ),
                    mkgame_moves(
                        "tk2",
                        ["d4", "d5", "Nf3"],
                        user_color="white",
                        result_for_user="win",
                        opponent="Rival2",
                        created_at=200,
                    ),
                ],
                "tester",
            )
            db.save_analysis(
                "tk1",
                {
                    "game_id": "tk1",
                    "user_color": "white",
                    "result_for_user": "loss",
                    "opponent": "Rival1",
                    "avg_win_loss": 12.5,
                    "avg_win_before": 55.0,
                    "blunders": [4],
                    "mistakes": [],
                    "inaccuracies": [],
                    "missed_wins": [],
                    "time_pressure_blunders": [],
                    "moves": [
                        self._move("e4", "best", 0.0, 55.0, 55.0, "e4"),
                        self._move("e5", "good", 0.0, 45.0, 45.0, "e5"),
                        self._move("Nf3", "good", 0.0, 50.0, 50.0, "Nf3"),
                        self._move("Nc6", "good", 0.0, 50.0, 50.0, "Nc6"),
                        self._move(
                            "Bb5", "blunder", 25.0, 60.0, 35.0, "Bc4", ["c4", "Nf6"]
                        ),
                        self._move("a6", "good", 0.0, 40.0, 40.0, "a6"),
                    ],
                },
                depth=8,
            )
            db.save_analysis(
                "tk2",
                {
                    "game_id": "tk2",
                    "user_color": "white",
                    "result_for_user": "win",
                    "opponent": "Rival2",
                    "avg_win_loss": 6.0,
                    "avg_win_before": 55.0,
                    "blunders": [],
                    "mistakes": [2],
                    "inaccuracies": [],
                    "missed_wins": [],
                    "time_pressure_blunders": [],
                    "moves": [
                        self._move("d4", "best", 0.0, 55.0, 55.0, "d4"),
                        self._move("d5", "good", 0.0, 45.0, 45.0, "d5"),
                        self._move("Nf3", "mistake", 12.0, 62.0, 50.0, "c4"),
                    ],
                },
                depth=8,
            )
            db.set_current_user("tester")

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    def _wrong_answer(self, payload: dict) -> str:
        """Легальный ход из позиции задачи, отличный от лучшего."""
        board = chess.Board(payload["fen"])
        best = board.parse_san(payload["best_san"])
        for move in board.legal_moves:
            if move != best:
                return board.san(move)
        self.fail("нет легального хода, отличного от лучшего")

    # --- today ---

    def test_today_deterministic(self):
        self._seed_tasks()
        first = self.client.get("/api/task/today")
        second = self.client.get("/api/task/today")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        a, b = first.json(), second.json()
        self.assertEqual(a["game_id"], b["game_id"])
        self.assertEqual(a["ply"], b["ply"])
        self.assertEqual(a["fen"], b["fen"])
        self.assertEqual(a["task_key"], b["task_key"])

    def test_today_payload_structure(self):
        self._seed_tasks()
        resp = self.client.get("/api/task/today")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in (
            "date", "game_id", "ply", "fen", "san_played", "best_san",
            "classification", "drop", "win_before", "win_after", "opening",
            "eco", "result_for_user", "opponent", "user_color",
            "moves_before", "solution", "explanation", "explained", "done",
        ):
            self.assertIn(key, data)
        board = chess.Board(data["fen"])
        self.assertTrue(board.is_valid())
        self.assertTrue(board.turn)  # ход белых — позиция ДО хода игрока
        self.assertIsInstance(data["san_played"], str)
        self.assertTrue(data["san_played"])
        self.assertTrue(data["best_san"])
        # Конкретная линия для просмотра: UCI ходов и FEN после каждого полухода.
        self.assertRegex(data["best_uci"], r"^[a-h][1-8][a-h][1-8]$")
        self.assertRegex(data["played_uci"], r"^[a-h][1-8][a-h][1-8]$")
        self.assertIsInstance(data["best_line_fens"], list)
        for fen in data["best_line_fens"]:
            self.assertTrue(chess.Board(fen).is_valid())
        self.assertIn(data["classification"], ("blunder", "mistake"))
        self.assertIsInstance(data["drop"], (int, float))
        self.assertIsInstance(data["moves_before"], list)
        self.assertLessEqual(len(data["moves_before"]), 6)
        self.assertEqual(
            set(data["solution"]), {"san_played", "best_san", "drop"}
        )
        self.assertEqual(data["solution"]["best_san"], data["best_san"])
        self.assertFalse(data["explained"])
        self.assertIsNone(data["explanation"])
        # позиция действительно ДО сыгранного хода
        board.parse_san(data["san_played"])

    def test_today_choice_stored_in_meta(self):
        self._seed_tasks()
        data = self.client.get("/api/task/today").json()
        with Database(self.db_path) as db:
            db.init_db()
            stored = db.get_meta(f"daily_task_{data['date']}_tester")
        self.assertEqual(stored, data["task_key"])

    # --- legal ---

    def test_legal_moves(self):
        self._seed_tasks()
        today = self.client.get("/api/task/today").json()
        resp = self.client.get("/api/task/legal")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["task_key"], today["task_key"])
        self.assertEqual(data["fen"], today["fen"])
        self.assertTrue(data["moves"])
        board = chess.Board(data["fen"])
        valid = set(chess.SQUARE_NAMES)
        for m in data["moves"]:
            self.assertIn(m["from"], valid)
            self.assertIn(m["to"], valid)
            board.parse_san(m["san"])
        self.assertIn(today["best_san"], [m["san"] for m in data["moves"]])

    def test_legal_no_user_404(self):
        self._seed_tasks()
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        self.assertEqual(self.client.get("/api/task/legal").status_code, 404)

    def test_legal_no_errors_404(self):
        self._seed_user("tester", "t")  # анализ без ошибок
        self._set_current("tester")
        self.assertEqual(self.client.get("/api/task/legal").status_code, 404)

    # --- answer ---

    def test_answer_wrong_move(self):
        self._seed_tasks()
        payload = self.client.get("/api/task/today").json()
        wrong = self._wrong_answer(payload)
        resp = self.client.post("/api/task/answer", data={"answer": wrong})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["correct"])
        self.assertEqual(data["expected"], payload["best_san"])
        self.assertEqual(data["drop"], payload["drop"])
        self.assertEqual(data["win_before"], payload["win_before"])
        self.assertEqual(data["win_after"], payload["win_after"])
        self.assertEqual(data["san_played"], payload["san_played"])
        # решение записано: повторный запрос + ключ в meta
        again = self.client.get("/api/task/today").json()
        self.assertTrue(again["done"])
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(
                db.get_meta(_task_done_key(payload["date"], "tester")), "1"
            )

    def test_answer_correct_move(self):
        self._seed_tasks()
        payload = self.client.get("/api/task/today").json()
        resp = self.client.post(
            "/api/task/answer", data={"answer": payload["best_san"]}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["correct"])
        self.assertEqual(data["expected"], payload["best_san"])

    def test_answer_garbage_400(self):
        self._seed_tasks()
        self.client.get("/api/task/today")
        resp = self.client.post("/api/task/answer", data={"answer": "Qz9"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("нет такого хода", resp.json()["detail"])

    # --- explain ---

    def test_explain_then_cached(self):
        self._seed_tasks()
        good = (
            "Твой ход Bb5 — ошибка: поле c4 осталось без контроля. "
            "Лучше было Bc4 с давлением на центр. "
            "В похожих позициях сначала считай ответы соперника."
        )
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api._task_explain_llm",
                      return_value=good) as llm:
            resp = self.client.post("/api/task/explain")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["cached"])
            job = self._wait_job(data["job_id"])
            llm.assert_called_once()

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["result"]["explanation"], good)
        payload = self.client.get("/api/task/today").json()
        self.assertTrue(payload["explained"])
        self.assertEqual(payload["explanation"], good)

        with patch("app.api._task_explain_llm") as llm2:
            resp = self.client.post("/api/task/explain")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["cached"])
            self.assertEqual(data["explanation"], good)
            llm2.assert_not_called()

    def test_explain_no_llm_key_job_error(self):
        self._seed_tasks()
        with patch.dict(os.environ, {"LLM_API_KEY": ""}):
            resp = self.client.post("/api/task/explain")
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("LLM_API_KEY", job["error"])

    def test_explain_poison_cache_ignored_and_overwritten(self):
        """thinking-мусор в БД: today врёт explained=false, повтор лечит."""
        self._seed_tasks()
        today = self.client.get("/api/task/today").json()
        garbage = (
            "Here's a thinking process: 1. Analyze User Input. "
            "Position (FEN): rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR. "
            "Facts from data. Constraints: blunder. Step 1: My move Rh1."
        )
        with Database(self.db_path) as db:
            db.init_db()
            db.save_task_explanation(
                today["task_key"], "tester", today["game_id"], today["ply"], garbage
            )
        poisoned = self.client.get("/api/task/today").json()
        self.assertFalse(poisoned["explained"])
        self.assertIsNone(poisoned["explanation"])

        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api._task_explain_llm",
                      return_value=(
                          "Твой ход Nf3 — неточность: ты отдал центр без борьбы. "
                          "Сильнее было c4 с давлением на поле d5. "
                          "В похожих позициях сначала занимай центр пешками."
                      )) as llm:
            resp = self.client.post("/api/task/explain")
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()["cached"])
            job = self._wait_job(resp.json()["job_id"])
            llm.assert_called_once()
        self.assertEqual(job["status"], "done", job.get("error"))
        fixed = self.client.get("/api/task/today").json()
        self.assertTrue(fixed["explained"])
        self.assertEqual(fixed["explanation"], job["result"]["explanation"])

    def test_explain_safety_stub_ignored(self):
        """safety-заглушка в БД: today отдаёт explained=false (без сети)."""
        self._seed_tasks()
        today = self.client.get("/api/task/today").json()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_task_explanation(
                today["task_key"], "tester", today["game_id"], today["ply"],
                "User Safety: safe",
            )
        poisoned = self.client.get("/api/task/today").json()
        self.assertFalse(poisoned["explained"])
        self.assertIsNone(poisoned["explanation"])

    # --- пустой пул / нет пользователя ---

    def test_today_no_errors_404(self):
        self._seed_user("tester", "t")  # анализ без ошибок
        self._set_current("tester")
        resp = self.client.get("/api/task/today")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("запусти анализ", resp.json()["detail"])

    def test_task_endpoints_no_user_404(self):
        self._seed_tasks()
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        self.assertEqual(self.client.get("/api/task/today").status_code, 404)
        self.assertEqual(
            self.client.post("/api/task/answer", data={"answer": "e4"}).status_code,
            404,
        )
        self.assertEqual(self.client.post("/api/task/explain").status_code, 404)


class TrackApiTests(ApiTestBase):
    """Тесты мини-трекера времени: /api/track/ping и /api/track/today.

    Время патчится через app.api._now — сеть и реальные часы не участвуют.
    """

    def setUp(self) -> None:
        super().setUp()
        self._set_current("tester")

    def test_ping_accumulates_site_time(self):
        clock = {"t": 5_000_000.0}
        with patch("app.api._now", new=lambda: clock["t"]):
            first = self.client.post("/api/track/ping")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), {"ok": True, "total": 0})

            clock["t"] += 120
            second = self.client.post("/api/track/ping")
            self.assertEqual(second.json()["total"], 120)

            data = self.client.get("/api/track/today").json()
        self.assertEqual(data["date"], _today())
        self.assertEqual(data["site_sec"], 120)
        self.assertEqual(data["games_today"], 0)
        self.assertEqual(data["lichess_sec"], 0)
        self.assertEqual(data["total_sec"], 120)

    def test_ping_gap_over_cap_not_credited(self):
        clock = {"t": 5_000_000.0}
        with patch("app.api._now", new=lambda: clock["t"]):
            self.client.post("/api/track/ping")  # создаёт сессию

            clock["t"] += 1000  # gap > 300с — не начисляем
            resp = self.client.post("/api/track/ping")
            self.assertEqual(resp.json()["total"], 0)

            clock["t"] += 60  # last обновлён — этот gap начисляется
            resp = self.client.post("/api/track/ping")
            self.assertEqual(resp.json()["total"], 60)
            data = self.client.get("/api/track/today").json()
        self.assertEqual(data["site_sec"], 60)

    def test_today_counts_lichess_games(self):
        today_ms = int(time.time() * 1000)
        yesterday_ms = int((time.time() - 86_400) * 1000)
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "tr1",
                        ["e4", "e5"],
                        created_at=today_ms,
                        clocks=[600.0, 598.0, 596.0, 590.0],
                    ),
                    mkgame_moves(
                        "tr2",
                        ["d4", "d5"],
                        created_at=yesterday_ms,
                        clocks=[600.0, 590.0],
                    ),
                ],
                "tester",
            )
        data = self.client.get("/api/track/today").json()
        self.assertEqual(data["games_today"], 1)
        # убывания 600→598=2, 598→596=2, 596→590=6
        self.assertEqual(data["lichess_sec"], 10)
        self.assertEqual(data["site_sec"], 0)
        self.assertEqual(data["total_sec"], data["site_sec"] + data["lichess_sec"])

    def test_total_sec_sums_site_and_lichess(self):
        today_ms = int(time.time() * 1000)
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "tr3",
                        ["e4"],
                        created_at=today_ms,
                        clocks=[600.0, 590.0],
                    ),
                ],
                "tester",
            )
        clock = {"t": 5_000_000.0}
        with patch("app.api._now", new=lambda: clock["t"]):
            self.client.post("/api/track/ping")
            clock["t"] += 90
            self.client.post("/api/track/ping")
            data = self.client.get("/api/track/today").json()
        self.assertEqual(data["site_sec"], 90)
        self.assertEqual(data["lichess_sec"], 10)
        self.assertEqual(data["total_sec"], 100)

    def test_empty_clocks_count_as_zero(self):
        today_ms = int(time.time() * 1000)
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves("tr4", ["e4"], created_at=today_ms, clocks=[])],
                "tester",
            )
        data = self.client.get("/api/track/today").json()
        self.assertEqual(data["games_today"], 1)
        self.assertEqual(data["lichess_sec"], 0)

    def test_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        self.assertEqual(self.client.post("/api/track/ping").status_code, 404)
        self.assertEqual(self.client.get("/api/track/today").status_code, 404)

    def test_ping_stores_valid_json_meta(self):
        clock = {"t": 5_000_000.0}
        with patch("app.api._now", new=lambda: clock["t"]):
            self.client.post("/api/track/ping")
        with Database(self.db_path) as db:
            db.init_db()
            raw = db.get_meta(f"site_time_{_today()}_tester")
        self.assertIsNotNone(raw)
        stored = json.loads(raw)
        self.assertEqual(stored["last"], 5_000_000.0)
        self.assertEqual(stored["total"], 0)

    def test_manual_minutes_accumulate_and_add_to_total(self):
        before = self.client.get("/api/track/today").json()["total_sec"]

        first = self.client.post(
            "/api/track/lichess_manual", data={"minutes": "30"}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            first.json(), {"manual_sec": 1800, "date": _today()}
        )

        second = self.client.post(
            "/api/track/lichess_manual", data={"minutes": "15"}
        )
        self.assertEqual(second.json()["manual_sec"], 45 * 60)

        data = self.client.get("/api/track/today").json()
        self.assertEqual(data["manual_sec"], 45 * 60)
        self.assertEqual(data["total_sec"], before + 45 * 60)
        self.assertEqual(
            data["total_sec"],
            data["site_sec"] + data["lichess_sec"] + data["manual_sec"],
        )

    def test_manual_minutes_out_of_range_400(self):
        for bad in ("0", "601", "-5", "abc", "30.5", ""):
            with self.subTest(minutes=bad):
                resp = self.client.post(
                    "/api/track/lichess_manual", data={"minutes": bad}
                )
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            self.client.post("/api/track/lichess_manual").status_code, 400
        )
        data = self.client.get("/api/track/today").json()
        self.assertEqual(data["manual_sec"], 0)

    def test_manual_minutes_no_user_404(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        resp = self.client.post(
            "/api/track/lichess_manual", data={"minutes": "30"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_manual_yesterday_not_in_today_total(self):
        yesterday = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - 86_400)
        )
        with Database(self.db_path) as db:
            db.init_db()
            db.set_meta(f"lichess_manual_{yesterday}_tester", str(30 * 60))
        before = self.client.get("/api/track/today").json()["total_sec"]

        self.client.post("/api/track/lichess_manual", data={"minutes": "10"})

        data = self.client.get("/api/track/today").json()
        self.assertEqual(data["manual_sec"], 10 * 60)
        self.assertEqual(data["total_sec"], before + 10 * 60)


class PlanGoalTests(ApiTestBase):
    """GET/POST /api/plan/goal: цель, минуты, фокус."""

    def _add_user(self) -> None:
        self._register("tester")

    def test_goal_defaults(self):
        self._add_user()
        resp = self.client.get("/api/plan/goal")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "goal": None,
                "minutes": 30,
                "focus": "auto",
                "goal_type": "fix_weak",
                "tournament_date": None,
                "target_rating": None,
                "weeks_left": None,
            },
        )

    def test_goal_set_and_get(self):
        self._add_user()
        resp = self.client.post(
            "/api/plan/goal",
            data={"goal": "Стать 2000", "minutes": "45", "focus": "middlegame"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "ok": True,
                "goal": "Стать 2000",
                "minutes": 45,
                "focus": "middlegame",
                "goal_type": "fix_weak",
                "tournament_date": None,
                "target_rating": None,
            },
        )
        self.assertEqual(
            self.client.get("/api/plan/goal").json(),
            {
                "goal": "Стать 2000",
                "minutes": 45,
                "focus": "middlegame",
                "goal_type": "fix_weak",
                "tournament_date": None,
                "target_rating": None,
                "weeks_left": None,
            },
        )

    def test_goal_validation(self):
        self._add_user()
        bad = [
            {"goal": "x", "minutes": "0", "focus": "auto"},
            {"goal": "x", "minutes": "1000", "focus": "auto"},
            {"goal": "x", "minutes": "30", "focus": "мусор"},
            {"goal": "", "minutes": "30", "focus": "auto"},
            {"goal": "y" * 201, "minutes": "30", "focus": "auto"},
        ]
        for data in bad:
            with self.subTest(data=data):
                self.assertEqual(
                    self.client.post("/api/plan/goal", data=data).status_code, 400
                )

    def test_goal_no_user_404(self):
        self.assertEqual(self.client.get("/api/plan/goal").status_code, 404)
        resp = self.client.post(
            "/api/plan/goal",
            data={"goal": "x", "minutes": "30", "focus": "auto"},
        )
        self.assertEqual(resp.status_code, 404)


class PlanWeekTests(ApiTestBase):
    """GET /api/plan/week: детерминированный план из живых данных."""

    @staticmethod
    def _move(
        san: str,
        classification: str,
        drop: float,
        win_before: float,
        win_after: float,
        best: str,
    ) -> dict:
        return {
            "san": san,
            "before": {"cp": 20.0, "mate": None},
            "after": {"cp": 5.0, "mate": None},
            "win_before": win_before,
            "win_after": win_after,
            "drop": drop,
            "classification": classification,
            "best_move_san": best,
            "best_eval": {"cp": 20.0, "mate": None},
            "best_win": win_before,
            "clock_used": None,
            "time_pressure": None,
            "best_line": [],
            "played_line": [],
        }

    def _seed_week(self) -> None:
        """Испанка: ошибка в дебюте (ply 4, −10) и грубая в миттельшпиле (ply 12, −30)."""
        sans = [
            "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6",
            "O-O", "Be7", "Re1", "b5", "Bb3", "d6",
        ]
        entries = []
        for i, san in enumerate(sans):
            if i == 4:
                entries.append(self._move(san, "mistake", 10.0, 55.0, 45.0, "Bc4"))
            elif i == 12:
                entries.append(self._move(san, "blunder", 30.0, 60.0, 30.0, "Bc2"))
            else:
                entries.append(self._move(san, "best", 0.0, 55.0, 55.0, san))
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "wk1",
                        sans,
                        user_color="white",
                        result_for_user="loss",
                        opponent="Rival",
                        opening="Ruy Lopez",
                        eco="C70",
                        created_at=100,
                    ),
                ],
                "tester",
            )
            db.save_analysis(
                "wk1",
                {
                    "game_id": "wk1",
                    "user_color": "white",
                    "result_for_user": "loss",
                    "opponent": "Rival",
                    "avg_win_loss": 20.0,
                    "avg_win_before": 55.0,
                    "blunders": [12],
                    "mistakes": [4],
                    "inaccuracies": [],
                    "missed_wins": [],
                    "time_pressure_blunders": [],
                    "moves": entries,
                },
                depth=8,
            )
            db.set_current_user("tester")

    def _get_week(self) -> dict:
        self._seed_week()
        resp = self.client.get("/api/plan/week")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_week_structure(self):
        data = self._get_week()
        for key in (
            "goal", "minutes_per_day", "focus", "week_tasks", "today",
            "human_summary",
        ):
            self.assertIn(key, data)
        self.assertEqual(set(data["focus"]), {"phase", "reason"})
        self.assertIsInstance(data["week_tasks"], list)
        self.assertTrue(data["week_tasks"])
        for task in data["week_tasks"]:
            self.assertEqual(set(task), {"key", "text", "detail"})
        self.assertEqual(
            set(data["today"]),
            {"task_done", "time_sec", "drills_today", "drills_total"},
        )
        self.assertIsInstance(data["human_summary"], str)
        self.assertTrue(data["human_summary"])

    def test_week_focus_auto_picks_weakest_phase(self):
        data = self._get_week()
        # миттельшпиль −30 против дебюта −10: слабейшая фаза по avg_drop.
        self.assertEqual(data["focus"]["phase"], "middlegame")

    def test_week_human_summary_russian_and_digits(self):
        data = self._get_week()
        self.assertIn("миттельшпиль", data["human_summary"])
        self.assertRegex(data["human_summary"], r"\d")

    def test_week_drills_bound(self):
        data = self._get_week()
        self.assertLessEqual(data["today"]["drills_today"], 20)
        self.assertEqual(data["today"]["drills_total"], 0)
        self.assertEqual(data["today"]["drills_today"], 5)

    def test_week_explicit_focus_wins(self):
        self._seed_week()
        resp = self.client.post(
            "/api/plan/goal",
            data={"goal": "Эндшпиль!", "minutes": "45", "focus": "endgame"},
        )
        self.assertEqual(resp.status_code, 200)
        data = self.client.get("/api/plan/week").json()
        self.assertEqual(data["focus"]["phase"], "endgame")
        self.assertEqual(data["minutes_per_day"], 45)
        self.assertEqual(data["goal"], "Эндшпиль!")

    def test_week_no_user_404(self):
        self._seed_week()
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        self.assertEqual(self.client.get("/api/plan/week").status_code, 404)


class PlanLlmTests(ApiTestBase):
    """Опросник цели + POST /api/plan/generate и GET /api/plan/llm.

    LLM мокается patch'ем app.api.LLMClient (как run_coach/run_mentor
    в LlmToolApiTests). Сети и движка нет.
    """

    PLAN_TEXT = (
        "Неделя 1: упор на эндшпили + твоё самое слабое место\n"
        "Режим: обычный\n"
        "Норма дня (30 мин):\n"
        "- Задачи — 10 мин (20 качественных)\n"
        "- Эндшпиль — 10 мин (ладейные окончания)\n"
        "- Партии — 10 мин (1 партия в день)\n"
        "Итог недели: держим темп, считаем до конца."
    )

    def _add_user(self) -> None:
        self._register("plantester")

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    def _mock_llm(self, text: str) -> MagicMock:
        fake = MagicMock()
        fake.chat.return_value = text
        fake.model = "test-model"
        return fake

    def setUp(self) -> None:
        super().setUp()
        _JOBS.clear()
        # Герметичность: live-рейтинг Lichess по умолчанию выключен
        # (fallback на БД), иначе тесты ходили бы в реальную сеть.
        # Live-тесты ниже переопределяют мок своим patch.
        _live = patch("app.api.fetch_lichess_rating", return_value=None)
        _live.start()
        self.addCleanup(_live.stop)

    def tearDown(self) -> None:
        deadline = time.time() + 30.0
        while time.time() < deadline:
            running = [
                j for j in _JOBS.values()
                if j.user == "plantester" and j.status == "running"
            ]
            if not running:
                break
            time.sleep(0.2)
        _JOBS.clear()

    def test_live_rating_beats_db(self):
        """Live 1900 побеждает DB 2100: entry берёт rating/speed из live."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    "lv1", ["e4"], created_at=100,
                    user_rating=2100, speed="rapid",
                )],
                "plantester",
            )
        live = {"rating": 1900, "speed": "blitz"}
        with patch("app.api.fetch_lichess_rating", return_value=live):
            with Database(self.db_path) as db:
                db.init_db()
                entry = _plan_llm_input(db, "plantester", self.data_dir)
            self.assertEqual(entry["rating"], 1900)
            self.assertEqual(entry["speed"], "blitz")
            prompts = self._dry_run_prompts()
        user = self._prompt_by_role(prompts, "user")
        self.assertIn("1900", user)
        self.assertIn("блиц", user)
        self.assertNotIn("2100", user)

    def test_live_none_falls_back_to_db(self):
        """Live None (сеть упала) → fallback на DB-рейтинг."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    "lv2", ["e4"], created_at=100,
                    user_rating=2100, speed="rapid",
                )],
                "plantester",
            )
        with patch("app.api.fetch_lichess_rating", return_value=None):
            with Database(self.db_path) as db:
                db.init_db()
                entry = _plan_llm_input(db, "plantester", self.data_dir)
        self.assertEqual(entry["rating"], 2100)
        self.assertEqual(entry["speed"], "rapid")

    def test_live_none_no_games_rating_null(self):
        """Live None + партий нет → rating null, generate dry_run ок."""
        self._add_user()
        with patch("app.api.fetch_lichess_rating", return_value=None):
            with Database(self.db_path) as db:
                db.init_db()
                entry = _plan_llm_input(db, "plantester", self.data_dir)
            self.assertIsNone(entry["rating"])
            prompts = self._dry_run_prompts()
        user = self._prompt_by_role(prompts, "user")
        self.assertIn("неизвестен", user)

    def test_live_garbage_falls_back_to_db(self):
        """Мусор из live-пути (perfs нет / games нули → fetch дал None,
        либо битый dict) → fallback на DB-рейтинг."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    "lv3", ["e4"], created_at=100,
                    user_rating=2100, speed="rapid",
                )],
                "plantester",
            )
        garbage_cases = (
            None,
            {"rating": None, "speed": None},
            {"rating": "мусор", "speed": "blitz"},
            {"rating": 0, "speed": "blitz"},
            {"rating": True, "speed": "blitz"},
            {"foo": 1},
        )
        for garbage in garbage_cases:
            with self.subTest(garbage=garbage):
                with patch("app.api.fetch_lichess_rating", return_value=garbage):
                    with Database(self.db_path) as db:
                        db.init_db()
                        entry = _plan_llm_input(db, "plantester", self.data_dir)
                self.assertEqual(entry["rating"], 2100)
                self.assertEqual(entry["speed"], "rapid")

    def test_questionnaire_save_load(self):
        self._add_user()
        resp = self.client.post(
            "/api/plan/goal",
            data={
                "goal": "Турнир летом",
                "minutes": "60",
                "focus": "endgame",
                "goal_type": "tournament",
                "tournament_date": "2030-06-01",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["goal_type"], "tournament")
        self.assertEqual(body["tournament_date"], "2030-06-01")
        self.assertIsNone(body["target_rating"])
        got = self.client.get("/api/plan/goal").json()
        self.assertEqual(got["goal_type"], "tournament")
        self.assertEqual(got["tournament_date"], "2030-06-01")
        self.assertIsInstance(got["weeks_left"], int)
        self.assertGreater(got["weeks_left"], 0)

    def test_questionnaire_validation(self):
        self._add_user()
        base = {
            "goal": "x",
            "minutes": "30",
            "focus": "auto",
            "goal_type": "fix_weak",
        }
        bad_dates = [{**base, "tournament_date": "мусор"},
                     {**base, "tournament_date": "01.02.2030"},
                     {**base, "tournament_date": "2030-13-01"}]
        for data in bad_dates:
            with self.subTest(data=data):
                self.assertEqual(
                    self.client.post("/api/plan/goal", data=data).status_code, 400
                )
        bad_ratings = [{**base, "target_rating": "5000"},
                       {**base, "target_rating": "799"},
                       {**base, "target_rating": "abc"}]
        for data in bad_ratings:
            with self.subTest(data=data):
                self.assertEqual(
                    self.client.post("/api/plan/goal", data=data).status_code, 400
                )
        self.assertEqual(
            self.client.post(
                "/api/plan/goal", data={**base, "goal_type": "мусор"}
            ).status_code,
            400,
        )
        # Пустые новые поля — валидны (опциональны).
        ok = self.client.post(
            "/api/plan/goal",
            data={**base, "tournament_date": "", "target_rating": ""},
        )
        self.assertEqual(ok.status_code, 200)

    def test_generate_saves_and_get(self):
        self._add_user()
        self.client.post(
            "/api/plan/goal",
            data={
                "goal": "Поднять рейтинг",
                "minutes": "30",
                "focus": "auto",
                "goal_type": "rating",
                "target_rating": "2100",
            },
        )
        self.assertEqual(
            self.client.get("/api/plan/llm").json(), {"text": None}
        )
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.PLAN_TEXT)):
            resp = self.client.post("/api/plan/generate", data={})
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()["cached"])
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        plan = job["result"]["plan"]
        self.assertIn("Неделя 1", plan["text"])
        got = self.client.get("/api/plan/llm").json()
        self.assertEqual(got["text"], plan["text"])
        self.assertIn("created_at", got)
        self.assertIn("weeks_left", got)

    def test_generate_cached_without_changes(self):
        self._add_user()
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.PLAN_TEXT)):
            first = self.client.post("/api/plan/generate", data={})
            self._wait_job(first.json()["job_id"])
        second = self.client.post("/api/plan/generate", data={})
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["cached"])
        self.assertNotIn("job_id", body)
        self.assertIn("Неделя 1", body["plan"]["text"])

    def test_generate_new_job_after_minutes_change(self):
        self._add_user()
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.PLAN_TEXT)):
            first = self.client.post("/api/plan/generate", data={})
            self._wait_job(first.json()["job_id"])
        resp = self.client.post(
            "/api/plan/goal",
            data={"goal": "Качать тактику", "minutes": "60", "focus": "auto"},
        )
        self.assertEqual(resp.status_code, 200)
        again = self.client.post("/api/plan/generate", data={})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["cached"])
        self.assertIn("job_id", again.json())

    def test_generate_dry_run_no_key(self):
        self._add_user()
        with patch.dict(os.environ, {"LLM_API_KEY": ""}):
            resp = self.client.post(
                "/api/plan/generate", data={"dry_run": "on"}
            )
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()["cached"])
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        result = job["result"]
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["prompts"])
        system = result["prompts"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("Неделя", system["content"])
        # dry_run ничего не сохраняет.
        self.assertEqual(
            self.client.get("/api/plan/llm").json(), {"text": None}
        )

    def test_generate_thinking_goes_error(self):
        self._add_user()
        thinking = (
            "THINKING ANALYZE CONSTRAINTS STEP 1 FACTS FROM DATA "
            "thinking analyze constraints step 2 facts from data"
        )
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(thinking)):
            resp = self.client.post("/api/plan/generate", data={})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("разбор", job["error"])

    def test_plan_llm_no_user_404(self):
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.clear_current_user()
        # Сессия от register жива — проверяем через клиента без cookie.
        fresh = TestClient(self.app)
        self.assertEqual(fresh.get("/api/plan/llm").status_code, 404)
        self.assertEqual(
            fresh.post("/api/plan/generate", data={}).status_code, 404
        )

    def _dry_run_prompts(self) -> list[dict]:
        """dry_run generate без сети/LLM: возвращает промпты плана."""
        resp = self.client.post("/api/plan/generate", data={"dry_run": "on"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["cached"])
        job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        return job["result"]["prompts"]

    def _prompt_by_role(self, prompts: list[dict], role: str) -> str:
        for item in prompts:
            if item["role"] == role:
                return item["content"]
        self.fail(f"нет промпта с ролью {role}")

    def test_generate_prompt_contains_rating_and_calibration(self):
        """Вход содержит rating из последней партии; user-промпт — «2100»,
        системный — калибровку («1800»/«оппозиц»). Без сети/движка."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "rl1", ["e4"], created_at=100,
                        user_rating=1500, speed="rapid",
                    ),
                    mkgame_moves(
                        "rl2", ["e4"], created_at=200,
                        user_rating=2100, speed="rapid",
                    ),
                ],
                "plantester",
            )
        with Database(self.db_path) as db:
            db.init_db()
            entry = _plan_llm_input(db, "plantester", self.data_dir)
        self.assertEqual(entry["rating"], 2100)
        self.assertEqual(entry["speed"], "rapid")
        prompts = self._dry_run_prompts()
        user = self._prompt_by_role(prompts, "user")
        system = self._prompt_by_role(prompts, "system")
        self.assertIn("2100", user)
        self.assertIn("Уровень", user)
        self.assertIn("1800", system)
        self.assertIn("оппозиц", system)

    def test_generate_new_job_after_rating_change(self):
        """Смена только рейтинга меняет hash: второй generate — cached,
        после более новой партии с другим рейтингом — cached=false."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    "rt1", ["e4"], created_at=100,
                    user_rating=2100, speed="rapid",
                )],
                "plantester",
            )
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.PLAN_TEXT)):
            first = self.client.post("/api/plan/generate", data={})
            self.assertEqual(first.status_code, 200)
            self.assertFalse(first.json()["cached"])
            self._wait_job(first.json()["job_id"])
            second = self.client.post("/api/plan/generate", data={})
            self.assertTrue(second.json()["cached"])
            # Только рейтинг другой — партия новее, анализа у неё нет,
            # остальные цифры входа не меняются.
            with Database(self.db_path) as db:
                db.init_db()
                db.save_games(
                    [mkgame_moves(
                        "rt2", ["e4"], created_at=200,
                        user_rating=1900, speed="rapid",
                    )],
                    "plantester",
                )
            third = self.client.post("/api/plan/generate", data={})
            self.assertEqual(third.status_code, 200)
            self.assertFalse(third.json()["cached"])
            self.assertIn("job_id", third.json())
            job = self._wait_job(third.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))

    def test_generate_no_games_rating_null(self):
        """Без партий rating null, но generate работает (dry_run)."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            entry = _plan_llm_input(db, "plantester", self.data_dir)
        self.assertIsNone(entry["rating"])
        prompts = self._dry_run_prompts()
        user = self._prompt_by_role(prompts, "user")
        self.assertIn("неизвестен", user)
        self.assertIn("средняя потеря", user)

    def test_plan_llm_input_zero_rating_is_null(self):
        """user_rating 0 трактуется как отсутствие рейтинга."""
        self._add_user()
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    "rz1", ["e4"], created_at=100, user_rating=0,
                )],
                "plantester",
            )
            db.init_db()
            entry = _plan_llm_input(db, "plantester", self.data_dir)
        self.assertIsNone(entry["rating"])
        prompts = self._dry_run_prompts()
        user = self._prompt_by_role(prompts, "user")
        self.assertIn("неизвестен", user)

    BASIC_PLAN_TEXT = (
        "Неделя 1: базовая техника эндшпиля\n"
        "Режим: обычный\n"
        "Норма дня (30 мин):\n"
        "- Эндшпиль — 30 мин (учим оппозицию и правило квадрата, "
        "разбираем король и пешка против короля)\n"
        "Итог недели: освоим оппозицию и перестанем зевать."
    )

    def _seed_rating(self, rating: int) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves(
                    f"rl{rating}", ["e4"], created_at=100,
                    user_rating=rating, speed="rapid",
                )],
                "plantester",
            )

    def test_generate_basic_topics_rejected_for_advanced(self):
        """Рейтинг 2100 + детсад в плане → job error «базовые темы», в meta пусто."""
        self._add_user()
        self._seed_rating(2100)
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.BASIC_PLAN_TEXT)):
            resp = self.client.post("/api/plan/generate", data={})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("базовые темы", job.get("error") or "")
        self.assertEqual(
            self.client.get("/api/plan/llm").json(), {"text": None}
        )

    def test_generate_basic_topics_allowed_below_1800(self):
        """Рейтинг 1500: тот же текст — годный план, сохраняется."""
        self._add_user()
        self._seed_rating(1500)
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient",
                      return_value=self._mock_llm(self.BASIC_PLAN_TEXT)):
            resp = self.client.post("/api/plan/generate", data={})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(
            self.client.get("/api/plan/llm").json()["text"],
            self.BASIC_PLAN_TEXT,
        )

    def test_generate_basic_retry_then_good(self):
        """Рейтинг 2100: первая попытка с детсадом, повтор хороший → done."""
        self._add_user()
        self._seed_rating(2100)
        fake = MagicMock()
        fake.chat.side_effect = [self.BASIC_PLAN_TEXT, self.PLAN_TEXT]
        fake.model = "test-model"
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                patch("app.api.LLMClient", return_value=fake):
            resp = self.client.post("/api/plan/generate", data={})
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["result"]["plan"]["text"], self.PLAN_TEXT)


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

    def test_index_refs_versioned_assets(self):
        """Cache-busting: index.html ссылается на app.js/style.css с ?v=."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("app.js?v=", html)
        self.assertIn("style.css?v=", html)
        self.assertIn("v0.7.1", html)

    def test_api_version(self):
        self.assertEqual(self.app.version, "0.4.8")


class AuthApiTests(ApiTestBase):
    """Тесты регистрации/входа: /api/auth/*, сессии, задел под доступ."""

    def _fresh_client(self) -> TestClient:
        """Клиент без cookie (чистая сессия)."""
        return TestClient(self.app)

    def _wait_job(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail(f"job {job_id} не завершился за {timeout}с")

    def test_register_new_and_status(self):
        resp = self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "abcd", "lichess": "NeoChess"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "nick": "neo"})
        status = self.client.get("/api/auth/status").json()
        self.assertEqual(status, {"logged_in": True, "nick": "neo"})
        with Database(self.db_path) as db:
            db.init_db()
            rec = db.get_user_record("neo")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["lichess"], "NeoChess")
        self.assertTrue(rec["password_hash"])
        self.assertEqual(rec["role"], "user")
        self.assertEqual(int(rec["is_active"]), 1)

    def test_register_validation(self):
        bad = [
            {"login": "", "password": "abcd", "lichess": "X"},
            {"login": "x" * 65, "password": "abcd", "lichess": "X"},
            {"login": "neo", "password": "abc", "lichess": "X"},
            {"login": "neo", "password": "abcd", "lichess": ""},
        ]
        for data in bad:
            with self.subTest(data=data):
                self.assertEqual(
                    self.client.post("/api/auth/register", data=data).status_code,
                    400,
                )

    def test_register_duplicate_409(self):
        data = {"login": "neo", "password": "abcd", "lichess": "NeoChess"}
        self.assertEqual(self.client.post("/api/auth/register", data=data).status_code, 200)
        resp = self.client.post("/api/auth/register", data=data)
        self.assertEqual(resp.status_code, 409)

    def test_register_claim_preserves_games(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user("OldNick")
            db.save_games([mkgame("c1")], "OldNick")
            db.save_analysis("c1", {"summary": []}, depth=8)
        resp = self.client.post(
            "/api/auth/register",
            data={"login": "OldNick", "password": "abcd", "lichess": "OldLichess"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "nick": "OldNick"})
        with Database(self.db_path) as db:
            db.init_db()
            rec = db.get_user_record("oldnick")
            self.assertTrue(rec["password_hash"])
            self.assertEqual(rec["lichess"], "OldLichess")
            self.assertEqual(len(db.get_games("OldNick")), 1)
            self.assertTrue(db.has_analysis("c1"))

    def test_login_wrong_and_unknown_401(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "abcd", "lichess": "NeoChess"},
        )
        fresh = self._fresh_client()
        resp = fresh.post(
            "/api/auth/login", data={"login": "neo", "password": "wrong"}
        )
        self.assertEqual(resp.status_code, 401)
        resp = fresh.post(
            "/api/auth/login", data={"login": "ghost", "password": "abcd"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_login_ok_and_logout(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "abcd", "lichess": "NeoChess"},
        )
        fresh = self._fresh_client()
        resp = fresh.post(
            "/api/auth/login", data={"login": "neo", "password": "abcd"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fresh.get("/api/auth/status").json()["logged_in"], True)
        resp = fresh.post("/api/auth/logout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            fresh.get("/api/auth/status").json(),
            {"logged_in": False, "nick": None},
        )

    def test_inactive_blocks(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "abcd", "lichess": "NeoChess"},
        )
        with Database(self.db_path) as db:
            db.init_db()
            db.set_active("neo", False)
        fresh = self._fresh_client()
        resp = fresh.post(
            "/api/auth/login", data={"login": "neo", "password": "abcd"}
        )
        self.assertEqual(resp.status_code, 403)
        # старая сессия тоже гаснет
        self.assertEqual(
            self.client.get("/api/auth/status").json()["logged_in"], False
        )
        # данные через сессию — 404
        self.assertEqual(self.client.get("/api/track/today").status_code, 404)
        self.assertEqual(self.client.get("/api/plan/goal").status_code, 404)

    def test_password_change(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "oldpw", "lichess": "NeoChess"},
        )
        resp = self.client.post(
            "/api/auth/password", data={"old": "wrong", "new": "newpw"}
        )
        self.assertEqual(resp.status_code, 401)
        resp = self.client.post(
            "/api/auth/password", data={"old": "oldpw", "new": "newpw"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        fresh = self._fresh_client()
        self.assertEqual(
            fresh.post(
                "/api/auth/login", data={"login": "neo", "password": "oldpw"}
            ).status_code,
            401,
        )
        self.assertEqual(
            fresh.post(
                "/api/auth/login", data={"login": "neo", "password": "newpw"}
            ).status_code,
            200,
        )

    def test_password_requires_session(self):
        self.assertEqual(
            self._fresh_client().post(
                "/api/auth/password", data={"old": "a", "new": "bbbb"}
            ).status_code,
            401,
        )

    def test_register_claim_inactive_403(self):
        """Claim ника без пароля закрыт для неактивного пользователя (403)."""
        with Database(self.db_path) as db:
            db.init_db()
            db.upsert_user("OldNick")
            db.set_active("OldNick", False)
        resp = self.client.post(
            "/api/auth/register",
            data={"login": "OldNick", "password": "abcd", "lichess": "OldLichess"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["detail"], "Доступ ограничен")

    def test_password_change_kills_sessions(self):
        """После смены пароля старая сессия больше не валидна."""
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "oldpw", "lichess": "NeoChess"},
        )
        self.assertEqual(
            self.client.get("/api/auth/status").json()["logged_in"], True
        )
        resp = self.client.post(
            "/api/auth/password", data={"old": "oldpw", "new": "newpw"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.get("/api/auth/status").json(),
            {"logged_in": False, "nick": None},
        )

    def test_lichess_change(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "neo", "password": "abcd", "lichess": "NeoChess"},
        )
        resp = self.client.post("/api/auth/lichess", data={"lichess": "NewNick"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "lichess": "NewNick"})
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(db.get_user_record("neo")["lichess"], "NewNick")

    def test_coach_fetch_uses_lichess(self):
        self.client.post(
            "/api/auth/register",
            data={"login": "coachuser", "password": "abcd", "lichess": "ForeignNick"},
        )
        with tempfile.NamedTemporaryFile() as fh, \
                patch("app.api.fetch_user_games", return_value=[]) as fetch:
            with patch("app.api.settings", stockfish_path=fh.name):
                resp = self.client.post(
                    "/api/coach/run",
                    data={"max": "10", "perf": "rapid", "depth": "8", "multipv": "1"},
                )
                self.assertEqual(resp.status_code, 200)
                job = self._wait_job(resp.json()["job_id"])
        fetch.assert_called_once()
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(fetch.call_args[0][0], "ForeignNick")


class GameLinkTests(ApiTestBase):
    """Сквозной link: БД roundtrip, API-пейлоады, backfill-миграция.

    Где линка нет (None) — API отдаёт null, а не конструирует URL из id.
    """

    def _seed_two(self) -> None:
        """Партия со ссылкой + партия без ссылки, обе с ошибками белых."""
        mv = TaskApiTests._move
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves(
                        "lnk1",
                        ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
                        user_color="white",
                        result_for_user="loss",
                        opponent="Rival1",
                        created_at=100,
                        link="https://lichess.org/abcd1234",
                    ),
                    mkgame_moves(
                        "lnk2",
                        ["d4", "d5", "Nf3"],
                        user_color="white",
                        result_for_user="win",
                        opponent="Rival2",
                        created_at=200,
                    ),
                ],
                "plantester",
            )
            db.save_analysis(
                "lnk1",
                {
                    "game_id": "lnk1",
                    "user_color": "white",
                    "result_for_user": "loss",
                    "opponent": "Rival1",
                    "avg_win_loss": 12.5,
                    "avg_win_before": 55.0,
                    "blunders": [4],
                    "mistakes": [],
                    "inaccuracies": [],
                    "missed_wins": [],
                    "time_pressure_blunders": [],
                    "moves": [
                        mv("e4", "best", 0.0, 55.0, 55.0, "e4"),
                        mv("e5", "good", 0.0, 45.0, 45.0, "e5"),
                        mv("Nf3", "good", 0.0, 50.0, 50.0, "Nf3"),
                        mv("Nc6", "good", 0.0, 50.0, 50.0, "Nc6"),
                        mv("Bb5", "blunder", 25.0, 60.0, 35.0, "Bc4", ["c4", "Nf6"]),
                        mv("a6", "good", 0.0, 40.0, 40.0, "a6"),
                    ],
                },
                depth=8,
            )
            db.save_analysis(
                "lnk2",
                {
                    "game_id": "lnk2",
                    "user_color": "white",
                    "result_for_user": "win",
                    "opponent": "Rival2",
                    "avg_win_loss": 6.0,
                    "avg_win_before": 55.0,
                    "blunders": [2],
                    "mistakes": [],
                    "inaccuracies": [],
                    "missed_wins": [],
                    "time_pressure_blunders": [],
                    "moves": [
                        mv("d4", "best", 0.0, 55.0, 55.0, "d4"),
                        mv("d5", "good", 0.0, 45.0, 45.0, "d5"),
                        mv("Nf3", "blunder", 20.0, 65.0, 45.0, "c4"),
                    ],
                },
                depth=8,
            )
            db.set_current_user("plantester")

    def test_db_roundtrip(self):
        self._seed_two()
        with Database(self.db_path) as db:
            games = {g.id: g for g in db.get_games("plantester")}
        self.assertEqual(games["lnk1"].link, "https://lichess.org/abcd1234")
        self.assertIsNone(games["lnk2"].link)
        with Database(self.db_path) as db:
            self.assertEqual(
                db.get_game("lnk1", "plantester").link, "https://lichess.org/abcd1234"
            )

    def test_api_summary_has_link(self):
        self._seed_two()
        items = {g["id"]: g for g in self.client.get("/api/games").json()["games"]}
        self.assertEqual(items["lnk1"]["link"], "https://lichess.org/abcd1234")
        self.assertIsNone(items["lnk2"]["link"])

    def test_api_detail_has_link(self):
        self._seed_two()
        data = self.client.get("/api/games/lnk1").json()
        self.assertEqual(data["game"]["link"], "https://lichess.org/abcd1234")
        data = self.client.get("/api/games/lnk2").json()
        self.assertIsNone(data["game"]["link"])

    def test_api_drills_have_link(self):
        self._seed_two()
        items = {d["game_id"]: d for d in self.client.get("/api/drills").json()["items"]}
        self.assertEqual(items["lnk1"]["link"], "https://lichess.org/abcd1234")
        self.assertIsNone(items["lnk2"]["link"])

    def test_api_task_has_link(self):
        self._seed_two()
        payload = self.client.get("/api/task/today").json()
        expected = (
            "https://lichess.org/abcd1234" if payload["game_id"] == "lnk1" else None
        )
        self.assertIn("link", payload)
        self.assertEqual(payload["link"], expected)

    def test_api_report_worst_have_link(self):
        self._seed_two()
        worst = {
            w["game_id"]: w for w in self.client.get("/api/report").json()["worst_games"]
        }
        self.assertEqual(worst["lnk1"]["link"], "https://lichess.org/abcd1234")
        self.assertIsNone(worst["lnk2"]["link"])

    def test_backfill_fills_only_lichess_ids(self):
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    mkgame_moves("abcd1234", ["e4"], link=None),
                    mkgame_moves("abcdefg", ["e4"], link=None),
                    mkgame_moves("my-game!!", ["e4"], link=None),
                    mkgame_moves(
                        "qwer1234", ["e4"], link="https://example.com/x"
                    ),
                ],
                "plantester",
            )
            db.init_db()  # повторный прогон: backfill старых строк
            games = {g.id: g for g in db.get_games("plantester")}
        self.assertEqual(games["abcd1234"].link, "https://lichess.org/abcd1234")
        self.assertIsNone(games["abcdefg"].link)
        self.assertIsNone(games["my-game!!"].link)
        self.assertEqual(games["qwer1234"].link, "https://example.com/x")


class JobLimitTests(ApiTestBase):
    """Лимиты фоновых задач: 1 на юзера, 3 глобально (503 с дружелюбным текстом)."""

    def setUp(self) -> None:
        super().setUp()
        _JOBS.clear()

    def tearDown(self) -> None:
        _JOBS.clear()

    def _login(self, nick: str = "limiter") -> None:
        self.client.post(
            "/api/auth/register",
            data={"login": nick, "password": "pass1234", "lichess": nick},
        )

    def _insert_running_job(self, nick: str, job_id: str) -> None:
        from app.api import CoachJob

        job = CoachJob(id=job_id, user=nick, phase="Тест")
        job.status = "running"
        _JOBS[job_id] = job

    def test_per_user_limit(self):
        self._login()
        self._insert_running_job("limiter", "job-running-1")
        try:
            resp = self.client.post(
                "/api/coach/run",
                data={"max": "10", "perf": "rapid", "depth": "5",
                      "multipv": "1", "cached_only": "on"},
            )
            self.assertEqual(resp.status_code, 503)
            self.assertIn("уже выполняется", resp.json()["detail"])
        finally:
            _JOBS.clear()

    def test_global_limit(self):
        self._login()
        for i in range(3):
            self._insert_running_job(f"other{i}", f"job-global-{i}")
        try:
            resp = self.client.post(
                "/api/coach/run",
                data={"max": "10", "perf": "rapid", "depth": "5",
                      "multipv": "1", "cached_only": "on"},
            )
            self.assertEqual(resp.status_code, 503)
            self.assertIn("других игроков", resp.json()["detail"])
        finally:
            _JOBS.clear()

    def test_light_jobs_not_limited(self):
        """Лёгкие задачи (fide) не упираются в лимит."""
        self._login()
        for i in range(3):
            self._insert_running_job(f"other{i}", f"job-global-{i}")
        try:
            fide = self.client.post(
                "/api/fide/run", data={"id": "12345", "windows": "4"}
            )
            self.assertEqual(fide.status_code, 200)
            self.assertIn("job_id", fide.json())
        finally:
            _JOBS.clear()

    def test_active_job_roundtrip(self):
        """GET /api/jobs/active: своя running видна, чужая — нет, пусто — 404."""
        self._login()
        try:
            resp = self.client.get("/api/jobs/active")
            self.assertEqual(resp.status_code, 404)
            self._insert_running_job("limiter", "job-active-1")
            resp = self.client.get("/api/jobs/active")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["id"], "job-active-1")
            self._insert_running_job("stranger", "job-stranger-1")
            resp = self.client.get("/api/jobs/active")
            self.assertEqual(resp.json()["id"], "job-active-1")
            _JOBS["job-active-1"].status = "done"
            resp = self.client.get("/api/jobs/active")
            self.assertEqual(resp.status_code, 404)
        finally:
            _JOBS.clear()

    def test_limit_released_after_done(self):
        self._login()
        self._insert_running_job("limiter", "job-running-1")
        _JOBS["job-running-1"].status = "done"
        try:
            resp = self.client.post(
                "/api/coach/run",
                data={"max": "10", "perf": "rapid", "depth": "5",
                      "multipv": "1", "cached_only": "on"},
            )
            self.assertEqual(resp.status_code, 200)
        finally:
            _JOBS.clear()


class GameCausesNotesApiTests(ApiTestBase):
    """Причины ошибок и заметки к ходам: /api/games/{id}/causes|notes.

    Партия реплеится по game.moves; причины считаются только для ходов с
    classification blunder/mistake, фаза — по позиции перед ходом.
    """

    # 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O Be7 6.Re1 b5 7.Bb3 d6
    MOVES = [
        "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4",
        "Nf6", "O-O", "Be7", "Re1", "b5", "Bb3", "d6",
    ]

    @staticmethod
    def _entry(san: str, classification: str = "good", drop: float = 0.0) -> dict:
        return {
            "san": san,
            "classification": classification,
            "drop": drop,
            "time_pressure": None,
        }

    def _analysis(self) -> dict:
        entries = [self._entry(san) for san in self.MOVES]
        # ply 2 (Nf3) — ошибка в дебюте: стратегическая.
        entries[2] = self._entry("Nf3", "mistake", 5.0)
        # ply 4 (Bb5) — дебют, даже большая потеря остаётся стратегической.
        entries[4] = self._entry("Bb5", "blunder", 25.0)
        # ply 12 (Bb3) — миттельшпиль, потеря >= 20 win.%: тактический зевок.
        entries[12] = self._entry("Bb3", "blunder", 25.0)
        return {"moves": entries}

    def _seed_game(self, gid: str = "cn1", nick: str = "tester", analysis: bool = True) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame_moves(gid, self.MOVES, user_color="white")], nick)
            if analysis:
                db.save_analysis(gid, self._analysis(), depth=8)
        self._set_current(nick)

    def test_causes_only_errors_with_labels(self):
        self._seed_game()
        resp = self.client.get("/api/games/cn1/causes")
        self.assertEqual(resp.status_code, 200, resp.text)
        causes = resp.json()["causes"]
        # Только blunder/mistake; хорошие ходы (0, 1, 3, ...) не попадают.
        self.assertEqual(set(causes), {"2", "4", "12"})
        self.assertEqual(causes["2"]["cause"], "strategic")
        self.assertEqual(causes["2"]["label"], "стратегическая ошибка")
        self.assertTrue(causes["2"]["explanation"])
        self.assertIn("дебюте", causes["2"]["explanation"])
        # Дебютная потеря >= 20 win.% не становится «тактикой».
        self.assertEqual(causes["4"]["cause"], "strategic")
        self.assertEqual(causes["12"]["cause"], "tactical_oversight")
        self.assertEqual(causes["12"]["label"], "тактический зевок")
        self.assertIn("тактика", causes["12"]["explanation"])

        # Без анализа — пустой словарь, а не 500.
        self._seed_game("cn_plain", analysis=False)
        resp = self.client.get("/api/games/cn_plain/causes")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"causes": {}})

    def test_review_payload(self):
        self._seed_game()
        resp = self.client.get("/api/games/cn1/review")
        self.assertEqual(resp.status_code, 200, resp.text)
        review = resp.json()["review"]
        self.assertIsNotNone(review)
        self.assertIn("user", review["accuracy"])
        self.assertIn("opponent", review["accuracy"])
        self.assertEqual(set(review["phases"]), {"opening", "middlegame", "endgame"})
        self.assertEqual(set(review["categories"]), {"opening", "middlegame", "endgame", "tactics", "strategy", "positional"})
        self.assertTrue(review["summary"])
        self.assertEqual(len(review["moves"]), len(self.MOVES))
        # 1.e4 — теоретический ход из ECO-книги.
        self.assertEqual(review["moves"][0]["badge"], "book")
        self.assertTrue(review["moves"][0]["comment"])
        # Ошибки получают бейдж не «good».
        self.assertNotIn(review["moves"][12]["badge"], ("good", "excellent", "best", "book"))
        # Без анализа — review = null, а не 500.
        self._seed_game("cn_plain2", analysis=False)
        self.assertEqual(self.client.get("/api/games/cn_plain2/review").status_code, 200)
        self.assertIsNone(self.client.get("/api/games/cn_plain2/review").json()["review"])

    def test_causes_404_for_unknown_and_foreign_game(self):
        self._seed_game()
        self.assertEqual(self.client.get("/api/games/nope/causes").status_code, 404)
        # Партия другого игрока не видна: get_game сужается по пользователю.
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves("cn_other", self.MOVES, user_color="white")], "other"
            )
            db.save_analysis("cn_other", self._analysis(), depth=8)
        self.assertEqual(
            self.client.get("/api/games/cn_other/causes").status_code, 404
        )

    def test_notes_roundtrip_and_delete(self):
        self._seed_game()
        resp = self.client.post(
            "/api/games/cn1/notes", json={"ply": 4, "intent": "  идея: рокировка  "}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"ok": True})
        resp = self.client.get("/api/games/cn1/notes")
        self.assertEqual(resp.status_code, 200)
        # intent сохранён без окружающих пробелов, ply — строковый ключ.
        self.assertEqual(resp.json(), {"notes": {"4": "идея: рокировка"}})

        self.client.post("/api/games/cn1/notes", json={"ply": 6, "intent": "план"})
        self.client.post("/api/games/cn1/notes", json={"ply": 4, "intent": "новый"})
        notes = self.client.get("/api/games/cn1/notes").json()["notes"]
        self.assertEqual(notes, {"4": "новый", "6": "план"})

        # Пустой intent удаляет заметку (только указанный ply).
        resp = self.client.post("/api/games/cn1/notes", json={"ply": 4, "intent": "   "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.get("/api/games/cn1/notes").json(), {"notes": {"6": "план"}}
        )

    def test_notes_validation_and_foreign_404(self):
        self._seed_game()
        self.assertEqual(
            self.client.post(
                "/api/games/cn1/notes", json={"ply": -1, "intent": "x"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/games/cn1/notes", json={"ply": 0, "intent": "x" * 501}
            ).status_code,
            400,
        )
        # Ровно 500 символов — допустимо.
        self.assertEqual(
            self.client.post(
                "/api/games/cn1/notes", json={"ply": 0, "intent": "x" * 500}
            ).status_code,
            200,
        )

        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [mkgame_moves("cn_other", self.MOVES, user_color="white")], "other"
            )
        # Чужая партия: и чтение, и запись — 404 (в т.ч. заметки другого игрока).
        self.assertEqual(
            self.client.get("/api/games/cn_other/notes").status_code, 404
        )
        self.assertEqual(
            self.client.post(
                "/api/games/cn_other/notes", json={"ply": 2, "intent": "чужая"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/games/cn1/notes").json()["notes"].keys(), {"0"}
        )


class ChessUtilsApiTests(ApiTestBase):
    """POST /api/chess/{legal,move,eval}: stateless-разбор позиции.

    Stockfish не поднимается: для eval подменяем ``app.api.Engine`` фейком.
    """

    class FakePositionEngine:
        """Фейк Engine: запоминает параметры и отдаёт одну линию."""

        calls: list[dict] = []

        def __init__(self, depth=14, multipv=3, time_ms=None, **kwargs):
            type(self).calls.append(
                {"depth": depth, "multipv": multipv, "time_ms": time_ms}
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def analyze_position(self, board):
            return [
                {
                    "pv": ["Nf3", "Nc6", "Bb5"],
                    "eval": {"cp": 31, "mate": None},
                    "win": 54.5,
                    "multipv": 1,
                }
            ]

    def setUp(self) -> None:
        super().setUp()
        type(self).FakePositionEngine.calls = []
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("tester")

    def test_legal_starting_position_has_20_moves(self):
        resp = self.client.post(
            "/api/chess/legal", json={"fen": chess.STARTING_FEN}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        moves = resp.json()["moves"]
        self.assertEqual(len(moves), 20)
        e2e4 = next(m for m in moves if m["from"] == "e2" and m["to"] == "e4")
        self.assertEqual(e2e4["san"], "e4")
        self.assertIsNone(e2e4["promotion"])

    def test_move_applies_e2e4(self):
        resp = self.client.post(
            "/api/chess/move",
            json={"fen": chess.STARTING_FEN, "from": "e2", "to": "e4", "promotion": None},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["san"], "e4")
        expected = chess.Board()
        expected.push_san("e4")
        self.assertEqual(data["fen"], expected.fen())
        self.assertFalse(data["check"])
        self.assertFalse(data["game_over"])

    def test_illegal_move_400(self):
        resp = self.client.post(
            "/api/chess/move",
            json={"fen": chess.STARTING_FEN, "from": "e2", "to": "e5"},
        )
        self.assertEqual(resp.status_code, 400)
        # Невалидный UCI (нет такой клетки) — тоже 400, а не 500.
        resp = self.client.post(
            "/api/chess/move",
            json={"fen": chess.STARTING_FEN, "from": "zz", "to": "e4"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_eval_returns_white_win_and_best_line(self):
        fake = type(self).FakePositionEngine
        with patch("app.api.Engine", fake):
            resp = self.client.post(
                "/api/chess/eval",
                json={"fen": chess.STARTING_FEN, "depth": 10},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["depth"], 10)
        self.assertGreaterEqual(data["win"], 0.0)
        self.assertLessEqual(data["win"], 100.0)
        self.assertTrue(data["best_san"])
        self.assertEqual(data["best_san"], "Nf3")
        self.assertEqual(data["best_line"][0], "Nf3")
        self.assertEqual(
            fake.calls, [{"depth": 10, "multipv": 1, "time_ms": 800}]
        )

    def test_invalid_fen_400(self):
        for path, payload in (
            ("/api/chess/legal", {"fen": "not a fen"}),
            ("/api/chess/move", {"fen": "not a fen", "from": "e2", "to": "e4"}),
            ("/api/chess/eval", {"fen": "not a fen", "depth": 10}),
        ):
            with self.subTest(path=path):
                resp = self.client.post(path, json=payload)
                self.assertEqual(resp.status_code, 400, resp.text)


class GameImportApiTests(ApiTestBase):
    """Импорт PGN: POST /api/games/import и появление партии в списке."""

    PGN = """[Event "Import test"]
[Site "https://lichess.org/imp12345"]
[Date "2026.10.25"]
[White "Importer"]
[Black "Rival"]
[Result "1-0"]
[WhiteElo "2200"]
[BlackElo "2100"]
[ECO "C60"]
[Opening "Ruy Lopez"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0
"""

    def test_import_text_and_list(self) -> None:
        self._register("importer")
        resp = self.client.post("/api/games/import", data={"pgn": self.PGN})
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["imported"], 1)
        self.assertEqual(payload["ids"], ["imp12345"])
        games = self.client.get("/api/games").json()["games"]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["id"], "imp12345")
        self.assertEqual(games[0]["opponent"], "Rival")
        self.assertEqual(games[0]["user_color"], "white")
        self.assertEqual(games[0]["result_for_user"], "win")
        self.assertFalse(games[0]["analyzed"])

    def test_import_forced_black(self) -> None:
        self._register("importer")
        resp = self.client.post(
            "/api/games/import", data={"pgn": self.PGN, "user_color": "black"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        games = self.client.get("/api/games").json()["games"]
        self.assertEqual(games[0]["user_color"], "black")
        self.assertEqual(games[0]["opponent"], "Importer")
        self.assertEqual(games[0]["result_for_user"], "loss")

    def test_import_empty_and_garbage(self) -> None:
        self._register("importer")
        self.assertEqual(
            self.client.post("/api/games/import", data={"pgn": ""}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/games/import", data={"pgn": "not a pgn"}).status_code,
            400,
        )

    def test_import_requires_auth(self) -> None:
        fresh = TestClient(self.app)
        self.assertEqual(
            fresh.post("/api/games/import", data={"pgn": self.PGN}).status_code, 404
        )


if __name__ == "__main__":
    unittest.main()