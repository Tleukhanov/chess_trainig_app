"""Тесты веб-API (FastAPI TestClient).

Бэкенд не трогает реальную БД: create_api(db_path=..., data_dir=...)
получает временные пути. Движок и сеть не запускаются.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import chess

from app.api import create_api
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

    def test_delete_user_leaves_other(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": ""})
        self.client.post("/api/user/add", data={"nick": "bob", "fide": ""})
        resp = self.client.post("/api/user/delete", data={"nick": "ada"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        users = self.client.get("/api/users").json()["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["nick"], "bob")

    def test_delete_last_user_allowed(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": ""})
        resp = self.client.post("/api/user/delete", data={"nick": "ada"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/users").json()["users"], [])

    def test_delete_current_user_clears_current(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": ""})
        self.client.post("/api/user/add", data={"nick": "bob", "fide": ""})
        # add закрепляет последнего добавленного — текущий bob
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.json()["user"]["nick"], "bob")
        resp = self.client.post("/api/user/delete", data={"nick": "bob"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/user/current")
        self.assertEqual(resp.status_code, 404)

    def test_delete_user_removes_games_and_analyses(self):
        self._seed_user("tester", "t")
        self._set_current("tester")
        resp = self.client.post("/api/user/delete", data={"nick": "tester"})
        self.assertEqual(resp.status_code, 200)
        with Database(self.db_path) as db:
            db.init_db()
            self.assertEqual(db.get_games("tester"), [])
            self.assertFalse(db.has_analysis("t1"))
            self.assertIsNone(db.get_user("tester"))
        self.assertEqual(self.client.get("/api/user/current").status_code, 404)

    def test_delete_unknown_user_still_ok(self):
        resp = self.client.post("/api/user/delete", data={"nick": "ghost"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_fide_set_updates_profile(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": "42"})
        resp = self.client.post(
            "/api/user/fide", data={"nick": "ada", "fide_id": "4100000"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["fide_id"], "4100000")
        users = self.client.get("/api/users").json()["users"]
        self.assertEqual(users[0]["fide_id"], "4100000")

    def test_fide_empty_clears_profile(self):
        self.client.post("/api/user/add", data={"nick": "ada", "fide": "42"})
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