"""Тесты API «Тренера дебютов»: сессии, ходы, фоновый анализ.

Всё офлайн: соперник — заглушка политики (шов ``app.api._opening_policy``),
движок и LLM заменены фейками. Ни модель, ни Stockfish, ни сеть не
поднимаются.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
from fastapi.testclient import TestClient

from app.api import _JOBS, create_api
from app.db import Database
from app.games import Game
from app.openings import STYLES


# --------------------------------------------------------------------------- #
# Заглушки: соперник-политика и движок
# --------------------------------------------------------------------------- #
def _toggle_san(board: chess.Board) -> str:
    """Детерминированный «ход-туда-обратно» конём: гарантирует 40 полуходов.

    Белые гоняют коня g1<->f3, чёрные g8<->f6. Ни взятий, ни шахов — партия
    не может закончиться (мат/пат) раньше лимита полуходов.
    """
    pairs = {chess.WHITE: ("Nf3", "Ng1"), chess.BLACK: ("Nf6", "Ng8")}
    for san in pairs[board.turn]:
        try:
            board.parse_san(san)
            return san
        except ValueError:
            continue
    return board.san(next(iter(board.legal_moves)))


def _toggle_moves(plies: int = 40) -> list[str]:
    """Полная легальная последовательность на ``plies`` полуходов."""
    board = chess.Board()
    moves: list[str] = []
    for _ in range(plies):
        san = _toggle_san(board)
        board.push_san(san)
        moves.append(san)
    return moves


class StubPolicy:
    """Политика соперника-заглушка: один детерминированный легальный ход."""

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        return [(_toggle_san(board), 1.0)]


class _FakeGameAnalysis:
    def __init__(self, summary: dict) -> None:
        self._summary = summary

    def summary(self) -> dict:
        return self._summary


class FakeEngine:
    """Фейковый Stockfish: контекстный менеджер и правдоподобный summary."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeEngine":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def analyze_game(self, game: Game) -> _FakeGameAnalysis:
        moves = [
            {
                "san": san,
                "classification": "good",
                "drop": 0.0,
                "win_before": 50.0,
                "win_after": 50.0,
                "best_move_san": san,
            }
            for san in game.moves
        ]
        return _FakeGameAnalysis(
            {
                "moves": moves,
                "blunders": [],
                "mistakes": [],
                "inaccuracies": [],
                "avg_win_loss": 0.0,
            }
        )


_REPORT_JSON = json.dumps(
    {
        "summary": "вышел из дебюта нормально",
        "opening": "Дебют четырех коней",
        "moments": [
            {"ply": 1, "san": "Nf3", "kind": "good", "text": "нормально", "better": "e4"}
        ],
        "opponent": "играл в заданном стиле",
        "recommendation": "тренируй тактику",
    },
    ensure_ascii=False,
)


# --------------------------------------------------------------------------- #
# База
# --------------------------------------------------------------------------- #
class OpeningApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        _JOBS.clear()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        self.client2 = TestClient(self.app)

        # Герметичность: без сети и без модели/Stockfish.
        live = patch("app.api.fetch_lichess_rating", return_value=None)
        live.start()
        self.addCleanup(live.stop)
        policy = patch(
            "app.api._opening_policy",
            side_effect=lambda opp_elo, my_elo: (StubPolicy(), "maia2"),
        )
        policy.start()
        self.addCleanup(policy.stop)

    def _register(self, client: TestClient, nick: str) -> None:
        resp = client.post(
            "/api/auth/register",
            data={"login": nick, "password": "abcd", "lichess": nick},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _seed_session(
        self,
        sid: str,
        player: str,
        *,
        moves: list[str] | None = None,
        status: str = "in_progress",
        color: str = "white",
        style: str = "universal",
        my_elo: int = 1500,
        opp_elo: int = 1600,
        result: str | None = None,
        report: dict | None = None,
    ) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.save_opening_session(
                sid,
                player,
                my_elo=my_elo,
                opp_elo=opp_elo,
                style=style,
                color=color,
                moves=moves or [],
                status=status,
                result=result,
                report=report,
            )

    def _start(self, color: str = "white", client: TestClient | None = None, **body):
        client = client or self.client
        payload = {"style": "universal", "color": color}
        payload.update(body)
        resp = client.post("/api/opening/start", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _play_to_done(self, color: str = "white", plies: int = 40) -> dict:
        state = self._start(color)
        guard = 0
        while not state["finished"]:
            guard += 1
            self.assertLess(guard, 100, "партия не завершилась")
            board = chess.Board(state["fen"])
            resp = self.client.post(
                "/api/opening/move",
                json={"session_id": state["id"], "san": _toggle_san(board)},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            state = resp.json()
        return state

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


# --------------------------------------------------------------------------- #
# Стили
# --------------------------------------------------------------------------- #
class OpeningStylesTests(OpeningApiTestBase):
    def test_styles_offline(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/opening/styles")
        self.assertEqual(resp.status_code, 200)
        styles = resp.json()["styles"]
        self.assertEqual(len(styles), len(STYLES))
        self.assertEqual({s["key"] for s in styles}, set(STYLES))
        for style in styles:
            self.assertTrue(style["label"])
            self.assertTrue(style["description"])

    def test_styles_requires_auth(self):
        self.assertEqual(self.client.get("/api/opening/styles").status_code, 404)


# --------------------------------------------------------------------------- #
# Старт
# --------------------------------------------------------------------------- #
class OpeningStartTests(OpeningApiTestBase):
    def test_start_white(self):
        self._register(self.client, "tester")
        state = self._start("white", my_elo=1500, opp_elo=1600)
        self.assertEqual(state["color"], "white")
        self.assertEqual(state["status"], "in_progress")
        self.assertEqual(state["moves"], [])
        self.assertEqual(state["turn"], "white")
        self.assertEqual(state["fen"], chess.STARTING_FEN)
        self.assertEqual(state["plies"], 40)
        self.assertFalse(state["finished"])
        self.assertIsNone(state["result"])
        self.assertIsNone(state["report"])
        self.assertEqual(state["my_elo"], 1500)
        self.assertEqual(state["opp_elo"], 1600)

    def test_start_black_opponent_first(self):
        self._register(self.client, "tester")
        state = self._start("black", my_elo=1500, opp_elo=1600)
        self.assertEqual(state["color"], "black")
        self.assertEqual(len(state["moves"]), 1)
        self.assertEqual(state["moves"][0], "Nf3")  # ход соперника-белых
        self.assertEqual(state["turn"], "black")  # теперь очередь пользователя
        self.assertEqual(state["engine"], "maia2")
        self.assertEqual(state["last_reply"]["san"], "Nf3")
        self.assertTrue(state["last_reply"]["reason"])
        self.assertFalse(state["finished"])

    def test_start_validation(self):
        self._register(self.client, "tester")
        bad = [
            {"style": "nope", "color": "white"},
            {"style": "solid", "color": "green"},
            {"style": "solid", "color": "white", "opp_elo": 0},
            {"style": "solid", "color": "white", "my_elo": -5},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                resp = self.client.post("/api/opening/start", json=payload)
                self.assertEqual(resp.status_code, 400, resp.text)

    def test_start_default_elos(self):
        self._register(self.client, "tester")
        state = self._start("white")
        self.assertEqual(state["my_elo"], 1500)
        self.assertEqual(state["opp_elo"], 1600)

    def test_start_default_elos_from_rating(self):
        self._register(self.client, "tester")
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    Game(
                        id="g1",
                        moves=["e4"],
                        user_color="white",
                        user_rating=2100,
                        speed="rapid",
                        created_at=100,
                    )
                ],
                "tester",
            )
        state = self._start("white")
        self.assertEqual(state["my_elo"], 2100)
        self.assertEqual(state["opp_elo"], 2200)
        explicit = self._start("white", opp_elo=1800)
        self.assertEqual(explicit["opp_elo"], 1800)

    def test_start_requires_auth(self):
        resp = self.client.post(
            "/api/opening/start", json={"style": "solid", "color": "white"}
        )
        self.assertEqual(resp.status_code, 404)


# --------------------------------------------------------------------------- #
# Ходы
# --------------------------------------------------------------------------- #
class OpeningMoveTests(OpeningApiTestBase):
    def test_move_happy_path(self):
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "e4"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["moves"], ["e4", "Nf6"])  # ответ соперника-чёрных
        self.assertEqual(data["engine"], "maia2")
        self.assertEqual(data["last_reply"]["san"], "Nf6")
        self.assertTrue(data["last_reply"]["reason"])
        self.assertEqual(data["turn"], "white")
        self.assertFalse(data["finished"])

    def test_move_illegal_san_400(self):
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "Zz9"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_move_out_of_turn_400(self):
        self._register(self.client, "tester")
        # Сессия белых после их хода: сейчас очередь соперника.
        self._seed_session("s1", "tester", moves=["e4"], color="white")
        resp = self.client.post(
            "/api/opening/move", json={"session_id": "s1", "san": "e5"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_move_after_done_400(self):
        self._register(self.client, "tester")
        self._seed_session(
            "s1", "tester", moves=_toggle_moves(40), status="done", result="*"
        )
        resp = self.client.post(
            "/api/opening/move", json={"session_id": "s1", "san": "Nf3"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_move_ownership_404(self):
        self._register(self.client, "alice")
        state = self._start("white", client=self.client)
        self._register(self.client2, "bob")
        resp = self.client2.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "e4"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_move_missing_session_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/opening/move", json={"session_id": "nope", "san": "e4"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_reach_40_plies_done_white(self):
        self._register(self.client, "tester")
        state = self._play_to_done("white")
        self.assertTrue(state["finished"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "*")
        self.assertEqual(len(state["moves"]), 40)

    def test_reach_40_plies_done_black(self):
        self._register(self.client, "tester")
        state = self._play_to_done("black")
        self.assertTrue(state["finished"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "*")
        self.assertEqual(len(state["moves"]), 40)


# --------------------------------------------------------------------------- #
# Фоновый анализ
# --------------------------------------------------------------------------- #
class OpeningAnalyzeTests(OpeningApiTestBase):
    def _analyze(self, sid: str) -> str:
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}):
            resp = self.client.post(
                "/api/opening/analyze", json={"session_id": sid}
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["status"], "started")
        return data["job_id"]

    def test_analyze_requires_done_400(self):
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.post(
            "/api/opening/analyze", json={"session_id": state["id"]}
        )
        self.assertEqual(resp.status_code, 400)

    def test_analyze_ownership_404(self):
        self._register(self.client, "alice")
        self._seed_session("s1", "alice", moves=_toggle_moves(40), status="done")
        self._register(self.client2, "bob")
        resp = self.client2.post(
            "/api/opening/analyze", json={"session_id": "s1"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_analyze_missing_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/opening/analyze", json={"session_id": "nope"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_analyze_job_completes_and_stores_report(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia") as unload:
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)
            unload.assert_called_once()

        self.assertEqual(job["status"], "done", job.get("error"))
        report = job["result"]["report"]
        self.assertEqual(report["summary"], "вышел из дебюта нормально")
        self.assertEqual(report["recommendation"], "тренируй тактику")
        self.assertEqual(report["opponent"], "играл в заданном стиле")
        self.assertIsInstance(report["moments"], list)
        self.assertTrue(report["moments"])
        self.assertEqual(report["engine"], "stockfish")
        self.assertTrue(report["opening"]["name"])

        # Отчёт сохранён в сессии и отдаётся через GET.
        with Database(self.db_path) as db:
            db.init_db()
            row = db.get_opening_session("s1")
        self.assertEqual(row["status"], "analyzed")
        self.assertEqual(row["report"]["summary"], "вышел из дебюта нормально")
        state = self.client.get("/api/opening/s1").json()
        self.assertIsNotNone(state["report"])
        self.assertTrue(state["finished"])

    def test_analyze_unload_before_engine(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        calls: list[str] = []

        def make_engine(*args, **kwargs):
            calls.append("engine")
            return FakeEngine()

        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        with patch("app.api._unload_maia", side_effect=lambda: calls.append("unload")), \
                patch("app.api.Engine", side_effect=make_engine), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(calls, ["unload", "engine"])

    def test_analyze_llm_failure_job_error_session_unchanged(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.return_value = "это не JSON, извини"
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "error")
        self.assertIn("JSON", job["error"])
        with Database(self.db_path) as db:
            db.init_db()
            row = db.get_opening_session("s1")
        self.assertEqual(row["status"], "done")  # сессия не тронута
        self.assertIsNone(row["report"])

    def test_analyze_no_llm_key_job_error(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        with patch("app.api.Engine", FakeEngine), \
                patch.dict(os.environ, {"LLM_API_KEY": ""}):
            resp = self.client.post(
                "/api/opening/analyze", json={"session_id": "s1"}
            )
            self.assertEqual(resp.status_code, 200)
            job = self._wait_job(resp.json()["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("LLM_API_KEY", job["error"])


# --------------------------------------------------------------------------- #
# Состояние и список
# --------------------------------------------------------------------------- #
class OpeningStateTests(OpeningApiTestBase):
    def test_state_shape(self):
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.get(f"/api/opening/{state['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in (
            "id", "status", "moves", "fen", "turn", "color", "style",
            "my_elo", "opp_elo", "plies", "finished", "result", "report",
        ):
            self.assertIn(key, data)
        self.assertTrue(chess.Board(data["fen"]).is_valid())

    def test_get_missing_404(self):
        self._register(self.client, "tester")
        self.assertEqual(self.client.get("/api/opening/nope").status_code, 404)

    def test_get_other_user_404(self):
        self._register(self.client, "alice")
        state = self._start("white", client=self.client)
        self._register(self.client2, "bob")
        resp = self.client2.get(f"/api/opening/{state['id']}")
        self.assertEqual(resp.status_code, 404)

    def test_list_recent_newest_first(self):
        self._register(self.client, "tester")
        first = self._start("white")
        second = self._start("black")
        resp = self.client.get("/api/opening?limit=10")
        self.assertEqual(resp.status_code, 200)
        ids = [s["id"] for s in resp.json()["sessions"]]
        self.assertEqual(ids[0], second["id"])  # новые первыми
        self.assertIn(first["id"], ids)

    def test_legal_your_turn(self):
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.get(f"/api/opening/{state['id']}/legal")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["your_turn"])
        self.assertEqual(data["turn"], "white")
        self.assertTrue(data["moves"])
        board = chess.Board(data["fen"])
        for mv in data["moves"]:
            parsed = board.parse_san(mv["san"])
            self.assertEqual(chess.square_name(parsed.from_square), mv["from"])
            self.assertEqual(chess.square_name(parsed.to_square), mv["to"])

    def test_legal_after_finished_empty(self):
        self._register(self.client, "tester")
        state = self._play_to_done("white")
        resp = self.client.get(f"/api/opening/{state['id']}/legal")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["your_turn"])
        self.assertEqual(data["moves"], [])

    def test_legal_missing_404(self):
        self._register(self.client, "tester")
        self.assertEqual(
            self.client.get("/api/opening/nope/legal").status_code, 404
        )

    def test_endpoints_require_auth(self):
        self.assertEqual(self.client.get("/api/opening").status_code, 404)
        self.assertEqual(self.client.get("/api/opening/x").status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/opening/analyze", json={"session_id": "x"}
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
