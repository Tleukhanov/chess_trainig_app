"""Тесты API «Тренера миттельшпиля»: сессии, план, ходы Maia, фоновый разбор.

Всё офлайн: соперник — заглушка политики (шов ``app.api._opening_policy``),
Stockfish и LLM заменены фейками. Ни модель, ни движок, ни сеть не
поднимаются.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
from fastapi.testclient import TestClient

from app.api import (
    _JOBS,
    _MIDDLEGAME_JOBS,
    CoachJob,
    _register_job,
    create_api,
)
from app.db import Database
from app.games import Game
from app.middlegame import (
    MAX_PLIES,
    POSITIONS,
    SOURCES,
    MiddlegameSession,
    board_of,
)

PLAN = "Захватить линию c, поставить ладью на c7 и атаковать короля."


def _safe_move(board: chess.Board) -> chess.Move:
    """Первый легальный ход, после которого партия ещё не окончена."""
    fallback: chess.Move | None = None
    for move in board.legal_moves:
        if fallback is None:
            fallback = move
        board.push(move)
        over = board.is_game_over(claim_draw=False)
        board.pop()
        if not over:
            return move
    if fallback is not None:
        return fallback
    raise AssertionError("в позиции нет легальных ходов")


def _safe_san(board: chess.Board) -> str:
    return board.san(_safe_move(board))


class StubPolicy:
    """Политика Maia-заглушка: один детерминированный безопасный ход."""

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        return [(_safe_san(board), 1.0)]


class FakeEngine:
    """Фейковый Stockfish: контекстный менеджер и оценка по очереди хода."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeEngine":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def analyze_position(self, board: chess.Board) -> list[dict]:
        if board.is_game_over(claim_draw=False):
            return []
        cp = 20 if board.turn == chess.WHITE else -20
        return [
            {
                "pv": [_safe_san(board)],
                "eval": {"cp": cp, "mate": None},
                "win": 50.0,
                "multipv": 1,
            }
        ]


class BlockingEngine(FakeEngine):
    """FakeEngine, который висит на входе, пока тест не отпустит событие."""

    release: threading.Event

    def __enter__(self) -> "BlockingEngine":
        self.release.wait(timeout=15)
        return self


_REPORT_JSON = json.dumps(
    {
        "verdict": "good",
        "summary": "План был верным, линия c вскрыта.",
        "plan_comment": "План соответствовал позиции.",
        "moments": [
            {
                "ply": 1,
                "move_san": "Nf3",
                "title": "Точный ход",
                "comment": "Контроль центра",
                "alt_san": None,
                "eval_before": "+0.20",
                "eval_after": "+0.15",
            }
        ],
        "lesson": "Сначала занимай открытую линию.",
    },
    ensure_ascii=False,
)


class MiddlegameApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        _JOBS.clear()
        _MIDDLEGAME_JOBS.clear()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        self.client2 = TestClient(self.app)

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

    def _create(self, client: TestClient | None = None) -> dict:
        client = client or self.client
        resp = client.post("/api/middlegame/session")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["session"]

    def _plan(
        self,
        session_id: str,
        *,
        plan: str = PLAN,
        strength: str | None = "equal",
        client: TestClient | None = None,
    ) -> dict:
        client = client or self.client
        payload: dict = {"plan": plan}
        if strength is not None:
            payload["strength"] = strength
        resp = client.post(
            f"/api/middlegame/session/{session_id}/plan", json=payload
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["session"]

    def _seed_session(
        self,
        sid: str,
        player: str,
        *,
        position_key: str | None = None,
        plies: int = 0,
        status: str = "playing",
        result: str | None = None,
        report: dict | None = None,
        opp_elo: int = 1600,
    ) -> MiddlegameSession:
        position = (
            POSITIONS[0]
            if position_key is None
            else next(p for p in POSITIONS if p.key == position_key)
        )
        session = MiddlegameSession(
            id=sid,
            user=player,
            position_key=position.key,
            fen=position.fen,
            plan=PLAN,
            status=status,
            result=result,
            report=report,
            opp_elo=opp_elo,
        )
        board = chess.Board(session.fen)
        for _ in range(min(plies, MAX_PLIES)):
            if board.is_game_over(claim_draw=False):
                break
            move = _safe_move(board)
            board.push(move)
            session.moves.append(move.uci())
        with Database(self.db_path) as db:
            db.init_db()
            db.create_middlegame_session(session)
        return session

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

    def _analyze(self, sid: str) -> str:
        resp = self.client.post(f"/api/middlegame/session/{sid}/analyze")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["job_id"]


class MiddlegameAuthTests(MiddlegameApiTestBase):
    def test_all_endpoints_require_auth(self):
        cases = [
            self.client.get("/api/middlegame/meta"),
            self.client.post("/api/middlegame/session"),
            self.client.post(
                "/api/middlegame/session/x/plan", json={"plan": PLAN}
            ),
            self.client.post(
                "/api/middlegame/session/x/move", json={"move": "e2e4"}
            ),
            self.client.post("/api/middlegame/session/x/reply"),
            self.client.post("/api/middlegame/session/x/analyze"),
            self.client.get("/api/middlegame/session/x"),
        ]
        for resp in cases:
            with self.subTest(path=str(resp.request.url)):
                self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(cases[0].json()["detail"], "Нужен вход")


class MiddlegameMetaTests(MiddlegameApiTestBase):
    def test_meta_totals_and_sources(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/middlegame/meta")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], len(POSITIONS))
        self.assertEqual(data["played"], 0)
        self.assertEqual(
            data["sources"],
            [
                {"id": s["id"], "title": s["title"], "count": s["count"]}
                for s in SOURCES
            ],
        )

    def test_meta_played_counts_sessions(self):
        self._register(self.client, "tester")
        self._create()
        self._create()
        self.assertEqual(self.client.get("/api/middlegame/meta").json()["played"], 2)


class MiddlegameCreateTests(MiddlegameApiTestBase):
    def test_create_session_shape(self):
        self._register(self.client, "tester")
        session = self._create()
        self.assertEqual(session["status"], "plan")
        self.assertIsNone(session["plan"])
        self.assertIsNone(session["opp_elo"])
        self.assertEqual(session["moves"], [])
        self.assertEqual(session["side"], "white")
        self.assertTrue(chess.Board(session["fen"]).is_valid())
        self.assertIn(session["position_key"], {p.key for p in POSITIONS})
        self.assertIn(session["n"], {position.n for position in POSITIONS})

    def test_create_17_without_repeats(self):
        self._register(self.client, "tester")
        keys = [self._create()["position_key"] for _ in range(17)]
        self.assertEqual(len(keys), 17)
        self.assertEqual(len(set(keys)), 17)
        self.assertEqual(
            self.client.get("/api/middlegame/meta").json()["played"], 17
        )

    def test_get_other_user_404(self):
        self._register(self.client, "alice")
        session = self._create()
        self._register(self.client2, "bob")
        resp = self.client2.get(f"/api/middlegame/session/{session['id']}")
        self.assertEqual(resp.status_code, 404)


class MiddlegamePlanTests(MiddlegameApiTestBase):
    def _seed_rating(self, rating: int) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(
                [
                    Game(
                        id="g1",
                        moves=["e4"],
                        user_color="white",
                        user_rating=rating,
                        speed="rapid",
                        created_at=100,
                    )
                ],
                "tester",
            )

    def test_plan_validation_error(self):
        self._register(self.client, "tester")
        session = self._create()
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/plan",
            json={"plan": "коротко"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("коротк", resp.json()["detail"])

    def test_plan_invalid_strength_400(self):
        self._register(self.client, "tester")
        session = self._create()
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/plan",
            json={"plan": PLAN, "strength": "plus300"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_plan_sets_opp_elo_from_rating(self):
        self._register(self.client, "tester")
        self._seed_rating(2100)
        for strength, expected in (
            ("equal", 2100),
            ("plus100", 2200),
            ("plus200", 2300),
        ):
            with self.subTest(strength=strength):
                session = self._create()
                planned = self._plan(session["id"], strength=strength)
                self.assertEqual(planned["status"], "playing")
                self.assertEqual(planned["plan"], PLAN)
                self.assertEqual(planned["opp_elo"], expected)
                stored = self.client.get(
                    f"/api/middlegame/session/{session['id']}"
                ).json()["session"]
                self.assertEqual(stored["opp_elo"], expected)

    def test_plan_default_strength_is_equal(self):
        self._register(self.client, "tester")
        session = self._create()
        planned = self._plan(session["id"], strength=None)
        self.assertEqual(planned["opp_elo"], 1500)

    def test_plan_twice_409(self):
        self._register(self.client, "tester")
        session = self._create()
        self._plan(session["id"])
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/plan",
            json={"plan": PLAN},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_plan_missing_404(self):
        self._register(self.client, "tester")
        resp = self.client.post(
            "/api/middlegame/session/nope/plan", json={"plan": PLAN}
        )
        self.assertEqual(resp.status_code, 404)


class MiddlegameMoveTests(MiddlegameApiTestBase):
    def test_move_only_user_move_and_need_reply(self):
        self._register(self.client, "tester")
        session = self._create()
        self._plan(session["id"])
        board = chess.Board(session["fen"])
        uci = _safe_move(board).uci()
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/move",
            json={"move": uci},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["session"]["moves"], [uci])
        self.assertTrue(data["need_reply"])
        self.assertEqual(data["session"]["status"], "playing")

    def test_move_illegal_400(self):
        self._register(self.client, "tester")
        session = self._create()
        self._plan(session["id"])
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/move",
            json={"move": "a1a8"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_move_out_of_turn_400(self):
        self._register(self.client, "tester")
        session = self._seed_session("s1", "tester", plies=1)
        resp = self.client.post(
            "/api/middlegame/session/s1/move", json={"move": "a2a3"}
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_move_before_plan_409(self):
        self._register(self.client, "tester")
        session = self._create()
        board = chess.Board(session["fen"])
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/move",
            json={"move": _safe_move(board).uci()},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_move_after_analyzed_409(self):
        self._register(self.client, "tester")
        session = self._seed_session(
            "s1", "tester", plies=2, status="analyzed", result="*"
        )
        board = board_of(session)
        resp = self.client.post(
            "/api/middlegame/session/s1/move",
            json={"move": _safe_move(board).uci()},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_reply_idempotent_and_opponent_move(self):
        self._register(self.client, "tester")
        session = self._create()
        self._plan(session["id"])
        board = chess.Board(session["fen"])
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/move",
            json={"move": _safe_move(board).uci()},
        )
        self.assertTrue(resp.json()["need_reply"])

        first = self.client.post(
            f"/api/middlegame/session/{session['id']}/reply"
        )
        self.assertEqual(first.status_code, 200, first.text)
        moves = first.json()["session"]["moves"]
        self.assertEqual(len(moves), 2)

        second = self.client.post(
            f"/api/middlegame/session/{session['id']}/reply"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["session"]["moves"], moves)

    def test_reply_before_move_is_noop(self):
        self._register(self.client, "tester")
        session = self._create()
        self._plan(session["id"])
        resp = self.client.post(
            f"/api/middlegame/session/{session['id']}/reply"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["session"]["moves"], [])

    def test_move_other_user_404(self):
        self._register(self.client, "alice")
        session = self._create()
        self._plan(session["id"])
        self._register(self.client2, "bob")
        resp = self.client2.post(
            f"/api/middlegame/session/{session['id']}/move",
            json={"move": "a2a3"},
        )
        self.assertEqual(resp.status_code, 404)


class MiddlegameAnalyzeTests(MiddlegameApiTestBase):
    def test_analyze_happy_path_saves_report(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=6, result="*")
        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia") as unload, \
                patch(
                    "app.api.mg.build_report_prompt",
                    wraps=__import__(
                        "app.middlegame", fromlist=["build_report_prompt"]
                    ).build_report_prompt,
                ) as prompt:
            job_id = self._analyze("s1")
            job = self._wait_job(job_id)
            unload.assert_called_once()
            prompt.assert_called_once()
            facts = prompt.call_args.kwargs["engine_facts"]

        self.assertEqual(job["status"], "done", job.get("error"))
        report = job["result"]["report"]
        self.assertEqual(report["verdict"], "good")
        self.assertEqual(report["summary"], "План был верным, линия c вскрыта.")
        self.assertEqual(report["moments"][0]["move_san"], facts[0]["san"])
        self.assertEqual(report["moments"][0]["eval_before"], "+0.20")
        self.assertEqual(report["moments"][0]["eval_after"], "-0.20")
        self.assertEqual(len(facts), 6)
        for fact in facts:
            self.assertEqual(
                set(fact),
                {
                    "ply",
                    "san",
                    "uci",
                    "eval_before",
                    "eval_after",
                    "delta",
                    "best_san",
                    "second_san",
                    "pv",
                    "pv_after",
                },
            )

        with Database(self.db_path) as db:
            db.init_db()
            row = db.get_middlegame_session("s1")
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "analyzed")
        self.assertEqual(row.result, "*")
        self.assertEqual(row.report["verdict"], "good")

        state = self.client.get("/api/middlegame/session/s1").json()["session"]
        self.assertEqual(state["status"], "analyzed")
        self.assertIsNotNone(state["report"])
        self.assertEqual(state["report"]["summary"], report["summary"])

        _JOBS.clear()
        snapshot = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(snapshot.json()["status"], "done")
        self.assertEqual(snapshot.json()["result"]["report"]["verdict"], "good")

    def test_analyze_drops_moments_for_unplayed_moves(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=4)
        data = json.loads(_REPORT_JSON)
        data["moments"].append(
            {"ply": 99, "move_san": "Qh8#", "title": "фантом", "comment": "нет"}
        )
        llm = MagicMock()
        llm.chat.return_value = json.dumps(data, ensure_ascii=False)
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia"):
            job = self._wait_job(self._analyze("s1"))

        self.assertEqual(job["status"], "done", job.get("error"))
        moments = job["result"]["report"]["moments"]
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["ply"], 1)

    def test_analyze_retries_without_reasoning(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=4)
        llm = MagicMock()
        llm.chat.side_effect = [
            RuntimeError("LLM: пустой ответ (finish_reason=length)"),
            _REPORT_JSON,
        ]
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia"):
            job = self._wait_job(self._analyze("s1"))

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(llm.chat.call_count, 2)
        first, second = llm.chat.call_args_list
        self.assertTrue(first.kwargs.get("json_mode"))
        self.assertIsNone(first.kwargs.get("reasoning_max_tokens"))
        self.assertEqual(first.kwargs.get("max_tokens"), 7000)
        self.assertTrue(second.kwargs.get("json_mode"))
        self.assertEqual(second.kwargs.get("reasoning_max_tokens"), 0)
        self.assertEqual(second.kwargs.get("max_tokens"), 7000)

    def test_analyze_third_attempt_without_hints_succeeds(self):
        """Повтор без reasoning тоже пустой: третья обычная попытка спасает."""
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=4)
        llm = MagicMock()
        llm.chat.side_effect = [
            RuntimeError("LLM: пустой ответ (finish_reason=length)"),
            RuntimeError("LLM: пустой ответ (finish_reason=length)"),
            _REPORT_JSON,
        ]
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia"):
            job = self._wait_job(self._analyze("s1"))

        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["result"]["report"]["verdict"], "good")
        self.assertEqual(llm.chat.call_count, 3)
        third = llm.chat.call_args_list[2]
        self.assertIsNone(third.kwargs.get("reasoning_max_tokens"))

    def test_analyze_llm_failure_keeps_session(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=4)
        llm = MagicMock()
        llm.chat.return_value = "это не JSON"
        with patch("app.api.Engine", FakeEngine), \
                patch("app.api.LLMClient", return_value=llm), \
                patch("app.api._unload_maia"):
            job = self._wait_job(self._analyze("s1"))

        self.assertEqual(job["status"], "error")
        self.assertIn("JSON", job["error"])
        with Database(self.db_path) as db:
            db.init_db()
            row = db.get_middlegame_session("s1")
        self.assertEqual(row.status, "playing")
        self.assertIsNone(row.report)

    def test_analyze_no_moves_409(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=0)
        resp = self.client.post("/api/middlegame/session/s1/analyze")
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_analyze_analyzed_409(self):
        self._register(self.client, "tester")
        self._seed_session(
            "s1", "tester", plies=2, status="analyzed", result="*"
        )
        resp = self.client.post("/api/middlegame/session/s1/analyze")
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_analyze_missing_404(self):
        self._register(self.client, "tester")
        resp = self.client.post("/api/middlegame/session/nope/analyze")
        self.assertEqual(resp.status_code, 404)

    def test_analyze_duplicate_running_409(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=4)
        llm = MagicMock()
        llm.chat.return_value = _REPORT_JSON
        release = threading.Event()
        BlockingEngine.release = release
        try:
            with patch("app.api.Engine", BlockingEngine), \
                    patch("app.api.LLMClient", return_value=llm), \
                    patch("app.api._unload_maia"):
                job_id = self._analyze("s1")
                resp = self.client.post("/api/middlegame/session/s1/analyze")
                self.assertEqual(resp.status_code, 409, resp.text)
                release.set()
                job = self._wait_job(job_id)
        finally:
            release.set()
        self.assertEqual(job["status"], "done", job.get("error"))

    def test_analyze_per_user_limit_503(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=2)
        _register_job(CoachJob(id="busy", user="tester", status="running"))
        resp = self.client.post("/api/middlegame/session/s1/analyze")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertIn("уже выполняется", resp.json()["detail"])

    def test_analyze_global_limit_503(self):
        self._register(self.client, "tester")
        self._seed_session("s1", "tester", plies=2)
        _register_job(CoachJob(id="busy", user="other", status="running"))
        resp = self.client.post("/api/middlegame/session/s1/analyze")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertIn("других игроков", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
