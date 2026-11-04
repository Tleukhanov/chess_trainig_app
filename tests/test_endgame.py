"""Тесты тренера эндшпиля: модуль app.endgame и API /api/endgame/*.

Всё офлайн: движок не поднимается — судью ходов в API подменяет заглушка,
а в модульных тестах Engine передаётся фабрикой-фейком. Ни сеть, ни Maia
не используются.

Игровая фикс­тура — «Партия в опере» (Морфи, 1858), проверенная
python-chess: 33 полухода, на полухода 32 (ход белых) фаза эндшпиль:
npp=6, ферзь ещё не разменён (порог npp<=6 по app.patterns.phase).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chess
from fastapi.testclient import TestClient

from app import endgame as eg
from app.api import create_api
from app.db import Database
from app.games import Game

MOVES = [
    "e4", "e5", "Nf3", "d6", "d4", "Bg4", "dxe5", "Bxf3",
    "Qxf3", "dxe5", "Bc4", "Nf6", "Qb3", "Qe7", "Nc3", "c6",
    "Bg5", "b5", "Nxb5", "cxb5", "Bxb5+", "Nbd7", "O-O-O", "Rd8",
    "Rxd7", "Rxd7", "Rd1", "Qe6", "Bxd7+", "Nxd7", "Qb8+", "Nxb8",
    "Rd8#",
]
MISTAKE_PLY = 32
OWN_TASK_KEY = "g1|32"
PLAYED_SAN = "Rd8#"
BEST_SAN = "Rd2"  # легальная альтернатива в позиции перед 17.Rd8# (проверено)


def _analysis(win_before: float, win_after: float) -> dict:
    """Фейковый summary анализа: один mistake в эндшпиле на полуходе 32."""
    moves = [{"san": san, "classification": "good"} for san in MOVES]
    moves[MISTAKE_PLY] = {
        "san": PLAYED_SAN,
        "classification": "mistake",
        "drop": round(win_before - win_after, 2),
        "win_before": win_before,
        "win_after": win_after,
        "best_move_san": BEST_SAN,
        "best_line": ["Kd2"],
    }
    return {"game_id": "g1", "user_color": "white", "moves": moves}


def _game(game_id: str, opponent: str | None, created_at: int) -> Game:
    return Game(
        id=game_id,
        moves=list(MOVES),
        user_color="white",
        user_rating=1500,
        speed="rapid",
        created_at=created_at,
        opponent=opponent,
    )


class FakeEngine:
    """Фейковый Stockfish: контекстный менеджер и одна фиксированная линия."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeEngine":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def analyze_position(self, board: chess.Board) -> list[dict]:
        return [
            {
                "pv": ["Nf3"],
                "eval": {"cp": 30, "mate": None},
                "win": 54.0,
                "multipv": 1,
            }
        ]


class EndgameModuleTests(unittest.TestCase):
    """Инварианты эталонных позиций и чистые функции модуля."""

    def test_master_task_count_and_keys(self):
        self.assertGreaterEqual(len(eg.MASTER_TASKS), 8)
        self.assertLessEqual(len(eg.MASTER_TASKS), 12)
        keys = [task.key for task in eg.MASTER_TASKS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_master_fens_valid(self):
        for task in eg.MASTER_TASKS:
            with self.subTest(key=task.key):
                self.assertEqual(chess.Board(task.fen).status(), chess.STATUS_VALID)

    def test_master_moves_and_line_legal(self):
        for task in eg.MASTER_TASKS:
            with self.subTest(key=task.key):
                board = chess.Board(task.fen)
                board.push(board.parse_san(task.best_move_san))
                for san in task.best_line:
                    board.push(board.parse_san(san))
                for san in task.alt_moves:
                    chess.Board(task.fen).parse_san(san)

    def test_master_texts_present(self):
        for task in eg.MASTER_TASKS:
            with self.subTest(key=task.key):
                self.assertTrue(task.title.strip())
                self.assertTrue(task.why.strip())
                self.assertTrue(task.goal.strip())

    def test_master_payload_and_solution(self):
        task = eg.MASTER_TASK_BY_KEY["lucena"]
        payload = eg.master_payload(task)
        self.assertEqual(payload["source"], "master")
        self.assertEqual(payload["task_key"], "lucena")
        self.assertNotIn("best_move_san", payload)
        payload = eg.master_payload(task, with_solution=True)
        self.assertEqual(payload["best_move_san"], "Rd1+")
        self.assertIn("Ke7", payload["best_line"])
        self.assertTrue(payload["why"])
        self.assertIn("Rc4", eg.master_solution(task)["alt_moves_san"])

    def test_own_goal_buckets(self):
        self.assertEqual(eg._own_goal(80.0, 67.5), "Доведи до победы")
        self.assertEqual(eg._own_goal(50.0, 25.0), "Не упусти ничью")
        self.assertEqual(eg._own_goal(40.0, 39.0), "Удержи позицию")
        self.assertEqual(eg._own_goal(None, None), "Удержи позицию")

    def test_same_san(self):
        self.assertTrue(eg.same_san("Rd1+", "Rd1"))
        self.assertFalse(eg.same_san("Re1", "Rd1"))
        self.assertFalse(eg.same_san(None, "Rd1"))

    def test_parse_move_san_and_uci(self):
        board = chess.Board("1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1")
        move, san = eg.parse_move(board, "Rd1+")
        self.assertEqual(san, "Rd1+")
        move, san = eg.parse_move(board, "c1d1")
        self.assertEqual(san, "Rd1+")
        self.assertIsNone(eg.parse_move(board, "Rd19")[0])
        self.assertIsNone(eg.parse_move(board, "e5e5")[0])

    def test_judge_uses_factory(self):
        board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        judge = eg.judge_position(board, engine_factory=FakeEngine)
        self.assertEqual(judge["kind"], "stockfish")
        self.assertEqual(judge["eval"]["cp"], 30)
        self.assertEqual(judge["pv"], ["Nf3"])

    def test_judge_engine_failure_none(self):
        class Broken(FakeEngine):
            def __enter__(self):
                raise RuntimeError("Stockfish не найден")

        board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        self.assertIsNone(eg.judge_position(board, engine_factory=Broken))

    def test_syzygy_without_path_falls_to_stockfish(self):
        self.assertIsNone(eg._open_syzygy())

    def test_syzygy_path_missing_dir_none(self):
        with patch(
            "app.endgame.settings", SimpleNamespace(syzygy_path="/nonexistent/syzygy")
        ):
            self.assertIsNone(eg._open_syzygy())


class OwnTaskRowTests(unittest.TestCase):
    """Сборка own-задач из (Game, analysis) без API и движка."""

    def test_own_rows_from_analysis(self):
        pairs = [(_game("g1", "Vasya", 1000), _analysis(80.0, 67.5))]
        rows = eg.own_task_rows(pairs)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_key"], OWN_TASK_KEY)
        self.assertEqual(row["ply"], MISTAKE_PLY)
        self.assertEqual(row["classification"], "mistake")
        self.assertEqual(row["goal"], "Доведи до победы")
        self.assertEqual(row["best_move_san"], BEST_SAN)
        self.assertEqual(row["title"], "Партия против Vasya")
        board = chess.Board(row["fen"])
        self.assertEqual(board.turn, chess.WHITE)
        board.parse_san(BEST_SAN)

    def test_own_goal_draw_bucket(self):
        pairs = [(_game("g1", None, 1000), _analysis(50.0, 25.0))]
        rows = eg.own_task_rows(pairs)
        self.assertEqual(rows[0]["goal"], "Не упусти ничью")
        self.assertEqual(rows[0]["title"], "Партия g1")

    def test_own_rows_respect_limit(self):
        pairs = [(_game("g1", None, 1000), _analysis(80.0, 67.5))]
        self.assertEqual(len(eg.own_task_rows(pairs, limit=1)), 1)

    def test_own_payload_no_solution(self):
        pairs = [(_game("g1", "Vasya", 1000), _analysis(80.0, 67.5))]
        payload = eg.own_payload(eg.own_task_rows(pairs)[0])
        self.assertEqual(payload["source"], "own")
        self.assertNotIn("best_move_san", payload)
        self.assertEqual(payload["opponent"], "Vasya")

    def test_own_solution_from_facts(self):
        pairs = [(_game("g1", None, 1000), _analysis(80.0, 67.5))]
        solution = eg.own_solution(eg.own_task_rows(pairs)[0])
        self.assertEqual(solution["best_move_san"], BEST_SAN)
        self.assertEqual(solution["best_line"], ["Kd2"])
        self.assertEqual(solution["win_before"], 80.0)
        self.assertIn(PLAYED_SAN, solution["why"])
        self.assertIn(BEST_SAN, solution["why"])

    def test_non_endgame_mistakes_skipped(self):
        # В этой же партии есть ошибки в миттельшпиле (ненастоящий анализ),
        # но нас интересует только: PartyAnalysis с mistake в эндшпиле.
        analysis = {
            "game_id": "g1",
            "user_color": "white",
            "moves": [
                {"san": san, "classification": "blunder", "best_move_san": "e5"}
                for san in MOVES
            ],
        }
        pairs = [(_game("g1", None, 1000), analysis)]
        # npp на ранних полуходах велик — эндшпильных задач не должно быть,
        # кроме полухода 32 (на котором на доске 6 фигур без пешек чужих).
        rows = eg.own_task_rows(pairs)
        self.assertEqual([row["task_key"] for row in rows], [OWN_TASK_KEY])


class EndgameApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.app = create_api(db_path=self.db_path, data_dir=self.data_dir)
        self.client = TestClient(self.app)
        self.client2 = TestClient(self.app)

    def _register(self, client: TestClient, nick: str) -> None:
        resp = client.post(
            "/api/auth/register",
            data={"login": nick, "password": "abcd", "lichess": nick},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _seed_games(self) -> None:
        games = [_game("g1", "Vasya", 1000), _game("g2", None, 2000)]
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games(games, "tester")
            db.save_analysis("g1", _analysis(80.0, 67.5), depth=10)
            db.save_analysis("g2", _analysis(50.0, 25.0), depth=10)


class EndgameAuthTests(EndgameApiTestBase):
    def test_all_endpoints_require_auth(self):
        cases = [
            self.client.get("/api/endgame/meta"),
            self.client.get(
                "/api/endgame/legal", params={"source": "master", "task_key": "lucena"}
            ),
            self.client.post(
                "/api/endgame/answer",
                json={"source": "master", "task_key": "lucena", "move": "Rd1+"},
            ),
        ]
        for resp in cases:
            with self.subTest(url=str(resp.request.url)):
                self.assertEqual(resp.status_code, 401, resp.text)
                self.assertEqual(resp.json()["detail"], "Нужен вход")


class EndgameMetaTests(EndgameApiTestBase):
    def test_meta_shape(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/endgame/meta")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["sources"], ["master", "own"])
        self.assertEqual(data["progress"]["master"]["total"], len(eg.MASTER_TASKS))
        self.assertEqual(data["progress"]["master"]["solved"], 0)
        self.assertEqual(data["progress"]["own"]["total"], 0)
        self.assertEqual(data["progress"]["own"]["solved"], 0)
        self.assertEqual(len(data["master"]), len(eg.MASTER_TASKS))
        self.assertEqual(data["own"], [])
        task = data["master"][0]
        for key in (
            "source", "task_key", "fen", "goal", "title",
            "turn", "concept", "concept_title",
        ):
            self.assertIn(key, task)
        self.assertNotIn("best_move_san", data["master"][0])

    def test_meta_with_own_tasks(self):
        self._register(self.client, "tester")
        self._seed_games()
        data = self.client.get("/api/endgame/meta").json()
        self.assertEqual(data["progress"]["own"]["total"], 2)
        self.assertEqual(
            [item["task_key"] for item in data["own"]], ["g1|32", "g2|32"]
        )
        self.assertNotIn("best_move_san", data["own"][0])
        goals = {item["task_key"]: item["goal"] for item in data["own"]}
        self.assertEqual(goals["g1|32"], "Доведи до победы")
        self.assertEqual(goals["g2|32"], "Не упусти ничью")

    def test_meta_source_filter(self):
        self._register(self.client, "tester")
        self._seed_games()
        own = self.client.get("/api/endgame/meta", params={"source": "own"}).json()
        self.assertEqual(own["master"], [])
        self.assertEqual(len(own["own"]), 2)
        master = self.client.get("/api/endgame/meta", params={"source": "master"}).json()
        self.assertEqual(master["own"], [])
        self.assertEqual(len(master["master"]), len(eg.MASTER_TASKS))

    def test_meta_bad_source_400(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/endgame/meta", params={"source": "zoiches"})
        self.assertEqual(resp.status_code, 400, resp.text)


class EndgameAnswerTests(EndgameApiTestBase):
    def _answer(self, client: TestClient, **payload) -> dict:
        resp = client.post("/api/endgame/answer", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_answer_master_correct(self):
        self._register(self.client, "tester")
        with patch(
            "app.api.eg.judge_position", return_value={"kind": "stub", "wdl": 2}
        ):
            data = self._answer(
                self.client, source="master", task_key="lucena", move="Rd1+"
            )
        self.assertEqual(data["result"], "correct")
        self.assertEqual(data["task"]["best_move_san"], "Rd1+")
        self.assertEqual(data["task"]["best_line"], ["Ke7", "Rd4", "Ke6", "Kc7", "Rc2+"])
        self.assertEqual(data["judge"], {"kind": "stub", "wdl": 2})

    def test_answer_master_alt_accepted(self):
        self._register(self.client, "tester")
        with patch("app.api.eg.judge_position", return_value=None):
            data = self._answer(
                self.client, source="master", task_key="lucena", move="Rc4"
            )
        self.assertEqual(data["result"], "correct")

    def test_answer_master_wrong_move(self):
        self._register(self.client, "tester")
        with patch("app.api.eg.judge_position", return_value=None):
            data = self._answer(
                self.client, source="master", task_key="lucena", move="Rh1"
            )
        self.assertEqual(data["result"], "wrong")
        self.assertEqual(data["task"]["best_move_san"], "Rd1+")

    def test_answer_master_solved_state(self):
        self._register(self.client, "tester")
        with patch("app.api.eg.judge_position", return_value=None):
            self._answer(self.client, source="master", task_key="lucena", move="Rd1+")
            self._answer(self.client, source="master", task_key="opposition", move="Kd7")
        progress = self.client.get("/api/endgame/meta").json()["progress"]
        self.assertEqual(progress["master"]["solved"], 2)

    def test_answer_own_task_correct(self):
        self._register(self.client, "tester")
        self._seed_games()
        with patch("app.api.eg.judge_position", return_value=None):
            data = self._answer(self.client, source="own", task_key="g1|32", move=BEST_SAN)
        self.assertEqual(data["result"], "correct")
        self.assertEqual(data["task"]["task_key"], "g1|32")
        self.assertEqual(data["task"]["classification"], "mistake")
        progress = self.client.get("/api/endgame/meta").json()["progress"]
        self.assertEqual(progress["own"]["solved"], 1)

    def test_answer_own_wrong_move(self):
        self._register(self.client, "tester")
        self._seed_games()
        with patch("app.api.eg.judge_position", return_value=None):
            data = self._answer(self.client, source="own", task_key="g1|32", move="Bf6")
        self.assertEqual(data["result"], "wrong")
        progress = self.client.get("/api/endgame/meta").json()["progress"]
        self.assertEqual(progress["own"]["solved"], 0)

    def test_answer_per_user_isolation(self):
        self._register(self.client, "tester")
        self._register(self.client2, "bo")
        with patch("app.api.eg.judge_position", return_value=None):
            self._answer(self.client, source="master", task_key="lucena", move="Rd1+")
        progress = self.client2.get("/api/endgame/meta").json()["progress"]
        self.assertEqual(progress["master"]["solved"], 0)

    def test_answer_bad_source_400(self):
        self._register(self.client, "tester")
        for payload in (
            {"source": "bot", "task_key": "lucena", "move": "Rd1+"},
            {"source": "", "task_key": "lucena", "move": "Rd1+"},
        ):
            resp = self.client.post("/api/endgame/answer", json=payload)
            self.assertEqual(resp.status_code, 400, resp.text)

    def test_answer_unknown_task_404(self):
        self._register(self.client, "tester")
        for payload in (
            {"source": "master", "task_key": "zoiches", "move": "Rd1+"},
            {"source": "own", "task_key": "g1|999", "move": BEST_SAN},
        ):
            resp = self.client.post("/api/endgame/answer", json=payload)
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_answer_illegal_move_400(self):
        self._register(self.client, "tester")
        for move in ("Ke19", "e5e5", "  "):
            resp = self.client.post(
                "/api/endgame/answer",
                json={"source": "master", "task_key": "lucena", "move": move},
            )
            self.assertEqual(resp.status_code, 400, resp.text)


class EndgameLegalTests(EndgameApiTestBase):
    def test_legal_master(self):
        self._register(self.client, "tester")
        resp = self.client.get(
            "/api/endgame/legal", params={"source": "master", "task_key": "lucena"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["turn"], "white")
        self.assertIn("Rd1+", [move["san"] for move in data["moves"]])
        self.assertNotIn("best_move_san", data)

    def test_legal_own(self):
        self._register(self.client, "tester")
        self._seed_games()
        resp = self.client.get(
            "/api/endgame/legal", params={"source": "own", "task_key": "g1|32"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn(BEST_SAN, [move["san"] for move in resp.json()["moves"]])

    def test_legal_unknown_404(self):
        self._register(self.client, "tester")
        resp = self.client.get(
            "/api/endgame/legal", params={"source": "master", "task_key": "zoiches"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_legal_bad_params_400(self):
        self._register(self.client, "tester")
        resp = self.client.get("/api/endgame/legal", params={"source": "no"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/endgame/legal", params={"source": "own"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
