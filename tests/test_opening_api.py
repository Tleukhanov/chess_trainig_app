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
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
from fastapi.testclient import TestClient

from app.api import _JOBS, CoachJob, _build_opening_report, _persist_job, create_api
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
        # Тесты проверяют policy-путь; книгу отключаем, иначе ход берётся из ECO.
        theory = patch("app.openings.theory_moves", return_value=[])
        theory.start()
        self.addCleanup(theory.stop)

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
            if state["need_reply"]:
                resp = self.client.post(
                    "/api/opening/reply", json={"session_id": state["id"]}
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
    def test_move_shows_user_move_without_reply(self):
        """Ход игрока виден сразу: ответ соперника ещё не посчитан."""
        self._register(self.client, "tester")
        state = self._start("white")
        resp = self.client.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "e4"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["moves"], ["e4"])  # только ход игрока
        self.assertNotIn("last_reply", data)
        self.assertNotIn("engine", data)
        self.assertEqual(data["turn"], "black")  # теперь очередь соперника
        self.assertTrue(data["need_reply"])
        self.assertFalse(data["finished"])

    def test_reply_applies_opponent_move(self):
        self._register(self.client, "tester")
        state = self._start("white")
        after_move = self.client.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "e4"}
        ).json()
        resp = self.client.post(
            "/api/opening/reply", json={"session_id": state["id"]}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["moves"], ["e4", "Nf6"])  # ответ соперника-чёрных
        self.assertEqual(data["last_reply"]["san"], "Nf6")
        self.assertTrue(data["last_reply"]["reason"])
        self.assertEqual(data["engine"], "maia2")
        self.assertEqual(data["turn"], "white")  # снова ход игрока
        self.assertFalse(data["need_reply"])
        self.assertFalse(data["finished"])
        self.assertEqual(after_move["moves"], ["e4"])

    def test_reply_idempotent_user_turn(self):
        """Не очередь соперника — ответ не считаем и не падаем."""
        self._register(self.client, "tester")
        state = self._start("white")  # ход белых (игрока)
        resp = self.client.post(
            "/api/opening/reply", json={"session_id": state["id"]}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["moves"], [])
        self.assertNotIn("last_reply", data)
        self.assertFalse(data["need_reply"])

    def test_reply_idempotent_finished(self):
        self._register(self.client, "tester")
        self._seed_session(
            "s1", "tester", moves=_toggle_moves(40), status="done", result="*"
        )
        resp = self.client.post(
            "/api/opening/reply", json={"session_id": "s1"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(len(data["moves"]), 40)  # ход не добавился
        self.assertNotIn("last_reply", data)
        self.assertFalse(data["need_reply"])
        self.assertTrue(data["finished"])

    def test_user_move_finishing_game_needs_no_reply(self):
        """Ход игрока, доигравший лимит полуходов: need_reply=false, ответ не нужен."""
        self._register(self.client, "tester")
        # 39 полуходов: сейчас очередь чёрных (игрок — чёрные).
        self._seed_session("s1", "tester", moves=_toggle_moves(39), color="black")
        board = chess.Board()
        for san in _toggle_moves(39):
            board.push_san(san)
        resp = self.client.post(
            "/api/opening/move",
            json={"session_id": "s1", "san": _toggle_san(board)},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["finished"])
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["result"], "*")
        self.assertFalse(data["need_reply"])
        self.assertEqual(len(data["moves"]), 40)
        # Ответ после финала — идемпотентно, без нового хода.
        again = self.client.post(
            "/api/opening/reply", json={"session_id": "s1"}
        ).json()
        self.assertEqual(len(again["moves"]), 40)
        self.assertNotIn("last_reply", again)

    def test_user_move_checkmate_needs_no_reply(self):
        """Мат ходом игрока: need_reply=false даже до лимита полуходов."""
        self._register(self.client, "tester")
        self._seed_session(
            "s1",
            "tester",
            moves=["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6"],
            color="white",
        )
        resp = self.client.post(
            "/api/opening/move", json={"session_id": "s1", "san": "Qxf7#"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["finished"])  # мат — партия завершена до лимита
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["result"], "1-0")
        self.assertFalse(data["need_reply"])  # соперник ответить не может
        again = self.client.post(
            "/api/opening/reply", json={"session_id": "s1"}
        ).json()
        self.assertEqual(len(again["moves"]), 7)
        self.assertNotIn("last_reply", again)

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

    def test_reply_ownership_404(self):
        self._register(self.client, "alice")
        state = self._start("white", client=self.client)
        self._register(self.client2, "bob")
        resp = self.client2.post(
            "/api/opening/reply", json={"session_id": state["id"]}
        )
        self.assertEqual(resp.status_code, 404)

    def test_reply_missing_session_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/opening/reply", json={"session_id": "nope"}
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

    def test_analyze_retries_without_reasoning_on_empty(self):
        """Пустой ответ reasoning-модели: повтор без reasoning даёт отчёт."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.side_effect = [
            RuntimeError(
                "LLM: пустой ответ (finish_reason=length — модель потратила "
                "лимит на размышление, увеличьте лимит токенов)"
            ),
            _REPORT_JSON,
        ]
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api._ensure_llm_key"), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "done", job.get("error"))
        report = job["result"]["report"]
        self.assertEqual(report["summary"], "вышел из дебюта нормально")
        self.assertEqual(llm.chat.call_count, 2)
        first, second = llm.chat.call_args_list
        self.assertTrue(first.kwargs.get("json_mode"))
        self.assertIsNone(first.kwargs.get("reasoning_max_tokens"))
        self.assertTrue(second.kwargs.get("json_mode"))
        self.assertEqual(second.kwargs.get("reasoning_max_tokens"), 0)

    def test_analyze_third_attempt_without_hints_succeeds(self):
        """Повтор без reasoning тоже пустой: третья обычная попытка спасает."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.side_effect = [
            RuntimeError("LLM: пустой ответ (finish_reason=length)"),
            RuntimeError("LLM: пустой ответ (finish_reason=length)"),
            _REPORT_JSON,
        ]
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api._ensure_llm_key"), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["result"]["report"]["summary"],
                         "вышел из дебюта нормально")
        self.assertEqual(llm.chat.call_count, 3)
        third = llm.chat.call_args_list[2]
        self.assertIsNone(third.kwargs.get("reasoning_max_tokens"))

    def test_analyze_single_call_happy_path(self):
        """Успешный ответ с первого раза: повтор не делается."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api._ensure_llm_key"), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(llm.chat.call_count, 1)
        self.assertEqual(
            job["result"]["report"]["summary"], "вышел из дебюта нормально"
        )

    def test_analyze_all_attempts_fail_job_error(self):
        """Все три вызова падают: job в error, сессия и отчёт не тронуты."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError(
            "LLM: пустой ответ (finish_reason=length)"
        )
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api._ensure_llm_key"), \
                patch("app.api.LLMClient", return_value=llm):
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)

        self.assertEqual(job["status"], "error")
        self.assertEqual(llm.chat.call_count, 3)
        with Database(self.db_path) as db:
            db.init_db()
            row = db.get_opening_session("s1")
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["report"])

    def test_build_opening_report_retry_exhausted_raises(self):
        """``_build_opening_report`` бросает RuntimeError, если все попытки пусты."""
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("LLM: пустой ответ")
        session = {
            "color": "white",
            "style": "universal",
            "my_elo": 1500,
            "opp_elo": 1600,
        }
        user_entries = [
            {
                "ply": 1,
                "san": "e4",
                "classification": "good",
                "drop": 0.0,
                "win_before": 50.0,
                "win_after": 50.0,
                "best": "e4",
            }
        ]
        with patch("app.api._ensure_llm_key"), \
                patch("app.api.LLMClient", return_value=llm):
            with self.assertRaises(RuntimeError):
                _build_opening_report(
                    session, ["e4", "e5"], user_entries, ("Дебют", "C20"), {}
                )
        self.assertEqual(llm.chat.call_count, 3)

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

    # --- переживание перезапуска (снапшоты в meta) -------------------------- #

    def _meta(self, job_id: str) -> dict | None:
        with Database(self.db_path) as db:
            db.init_db()
            raw = db.get_meta(f"job:{job_id}")
        return json.loads(raw) if raw else None

    def _wait_meta(self, job_id: str, timeout: float = 10.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._meta(job_id)
            if data is not None:
                return data
            time.sleep(0.02)
        return None

    def test_persisted_running_job_without_owner_is_error(self):
        """Job в meta «running», но процесса-владельца нет — как после рестарта."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", moves=_toggle_moves(40), status="done")
        release = threading.Event()

        class BlockingEngine(FakeEngine):
            def analyze_game(self, game):
                release.wait(timeout=15)
                return super().analyze_game(game)

        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        try:
            with patch("app.api.Engine", BlockingEngine), \
                    patch("app.api.LLMClient", return_value=llm):
                job_id = self._analyze("s1")
                # Ждём, пока фоновый поток запишет running-снапшот фазы.
                self.assertIsNotNone(self._wait_meta(job_id))
                _JOBS.clear()  # имитируем перезапуск: память пуста
                resp = self.client.get(f"/api/jobs/{job_id}")
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["status"], "error")
                self.assertIn("перезапуст", data["error"])
                # Хранимый снапшот не мутирован — в meta всё ещё running.
                self.assertEqual(self._meta(job_id)["status"], "running")
        finally:
            release.set()

    def test_persisted_running_job_ownership_404(self):
        """Чужой running-снапшот из meta не отдаём (как и in-memory job)."""
        self._register(self.client, "alice")
        _persist_job(
            self.db_path, CoachJob(id="ajob", user="alice", status="running")
        )
        self._register(self.client2, "bob")
        resp = self.client2.get("/api/jobs/ajob")
        self.assertEqual(resp.status_code, 404)

    def test_persisted_done_snapshot_returned_as_is(self):
        """Снапшот done из meta (не в _JOBS) отдаётся без перезаписи статуса."""
        self._register(self.client, "tester")
        job = CoachJob(
            id="donejob",
            user="tester",
            status="done",
            phase="Готово",
            summary="отчёт готов",
            result={"report": {"summary": "вышел из дебюта нормально"}},
        )
        _persist_job(self.db_path, job, session_id="s1")
        self.assertNotIn("donejob", _JOBS)
        resp = self.client.get("/api/jobs/donejob")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["summary"], "отчёт готов")
        self.assertEqual(
            data["result"]["report"]["summary"], "вышел из дебюта нормально"
        )

    def test_missing_job_still_404(self):
        self._register(self.client, "tester")
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)


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
            "my_elo", "opp_elo", "plies", "finished", "need_reply",
            "result", "report",
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

    def test_legal_opponent_turn_after_move_empty(self):
        """После хода игрока (до ответа) ходы недоступны — очередь соперника."""
        self._register(self.client, "tester")
        state = self._start("white")
        after = self.client.post(
            "/api/opening/move", json={"session_id": state["id"], "san": "e4"}
        ).json()
        self.assertTrue(after["need_reply"])
        resp = self.client.get(f"/api/opening/{state['id']}/legal")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["your_turn"])
        self.assertEqual(data["moves"], [])
        # После ответа соперника ход снова за игроком.
        self.client.post("/api/opening/reply", json={"session_id": state["id"]})
        resp = self.client.get(f"/api/opening/{state['id']}/legal")
        data = resp.json()
        self.assertTrue(data["your_turn"])
        self.assertTrue(data["moves"])

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
