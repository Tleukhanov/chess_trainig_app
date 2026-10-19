"""«Тренер миттельшпиля»: позиции, сессия, отчёт и таблица БД."""

from __future__ import annotations

import random
import shutil
import tempfile
import unittest
from pathlib import Path

import chess

from app.db import Database
from app.middlegame import (
    MAX_PLIES,
    MAX_PLAN_CHARS,
    MIN_PLAN_CHARS,
    POSITION_BY_KEY,
    POSITIONS,
    SOURCES,
    MiddlegameSession,
    apply_opponent_move,
    apply_user_move,
    board_of,
    build_report_prompt,
    legal_moves,
    need_reply,
    new_session_id,
    normalize_report,
    opponent_side,
    pick_position,
    user_turn,
    validate_plan,
)

PLAN = "Захватить линию c, поставить ладью на c7 и атаковать короля на f8."


def _session(
    position_key: str = "curacao1962:12",
    *,
    status: str = "playing",
    moves: list[str] | None = None,
    fen: str | None = None,
    plan: str | None = PLAN,
) -> MiddlegameSession:
    position = POSITION_BY_KEY[position_key]
    return MiddlegameSession(
        id=new_session_id(),
        user="Alice",
        position_key=position.key,
        fen=fen or position.fen,
        plan=plan,
        moves=list(moves or []),
        status=status,
    )


def _long_sequence(plies: int) -> list[str]:
    """60 легальных полуходов без мата/пата/пятикратного повторения."""
    rng = random.Random(0)
    board = chess.Board()
    moves: list[str] = []
    while len(moves) < plies:
        candidates = list(board.legal_moves)
        rng.shuffle(candidates)
        for move in candidates:
            board.push(move)
            if not board.is_game_over(claim_draw=False):
                moves.append(move.uci())
                break
            board.pop()
        else:
            raise AssertionError("не удалось подобрать безопасный ход")
    return moves


class PositionsTests(unittest.TestCase):
    def test_bundle_loads_64_valid_positions(self) -> None:
        self.assertEqual(len(POSITIONS), 64)
        for position in POSITIONS:
            board = chess.Board(position.fen)
            self.assertTrue(board.is_valid(), position.key)
            self.assertEqual(board.turn, chess.WHITE, position.key)

    def test_sources_metadata(self) -> None:
        by_id = {source["id"]: source for source in SOURCES}
        self.assertIn("curacao1962", by_id)
        self.assertEqual(by_id["curacao1962"]["title"], "Curacao Candidates 1962")
        self.assertEqual(by_id["curacao1962"]["count"], 64)
        self.assertEqual(sum(source["count"] for source in SOURCES), len(POSITIONS))

    def test_positions_sorted_and_keys_unique(self) -> None:
        self.assertEqual(
            [position.key for position in POSITIONS],
            [position.key for position in sorted(POSITIONS, key=lambda p: (p.source_id, p.n))],
        )
        self.assertEqual(len({position.key for position in POSITIONS}), len(POSITIONS))

    def test_chapter_url_and_players(self) -> None:
        position = POSITION_BY_KEY["curacao1962:12"]
        self.assertEqual(position.n, 12)
        self.assertEqual(position.white, "Pal Benko")
        self.assertEqual(position.black, "Mikhail Tal")
        self.assertEqual(position.source_title, "Curacao Candidates 1962")
        self.assertEqual(position.source_url, "https://lichess.org/study/r4CqXLsW")
        self.assertEqual(
            position.chapter_url, "https://lichess.org/study/r4CqXLsW/M6LfYPka"
        )
        self.assertEqual(position.chapter, "M6LfYPka")

    def test_validate_plan_strips_and_checks_length(self) -> None:
        clean = validate_plan("  " + "x" * MIN_PLAN_CHARS + "  ")
        self.assertEqual(clean, "x" * MIN_PLAN_CHARS)
        with self.assertRaises(ValueError) as short:
            validate_plan("слишком коротко")
        self.assertIn("коротк", str(short.exception))
        with self.assertRaises(ValueError) as long:
            validate_plan("x" * (MAX_PLAN_CHARS + 1))
        self.assertIn("длинн", str(long.exception))

    def test_pick_position_avoids_seen_keys(self) -> None:
        target = POSITIONS[0]
        avoid = [position.key for position in POSITIONS if position.key != target.key]
        picked = pick_position("Alice", avoid_keys=avoid, rng=random.Random(1))
        self.assertEqual(picked.key, target.key)

    def test_pick_position_ignores_avoidance_when_exhausted(self) -> None:
        all_keys = [position.key for position in POSITIONS]
        picked = pick_position("Alice", avoid_keys=all_keys, rng=random.Random(2))
        self.assertIn(picked.key, all_keys)


class SessionTests(unittest.TestCase):
    def test_plan_state_and_legal_moves(self) -> None:
        session = _session(status="plan", plan=None)
        self.assertFalse(user_turn(session))
        self.assertFalse(need_reply(session))
        self.assertTrue(legal_moves(session))
        with self.assertRaises(ValueError):
            apply_user_move(session, legal_moves(session)[0])

    def test_full_flow_to_mate(self) -> None:
        session = _session("curacao1962:3")
        self.assertEqual(session.status, "playing")
        self.assertTrue(user_turn(session))
        self.assertFalse(need_reply(session))
        self.assertIn("g3c3", legal_moves(session))

        apply_user_move(session, "g3c3")
        self.assertEqual(session.moves, ["g3c3"])
        self.assertTrue(need_reply(session))
        self.assertFalse(user_turn(session))
        self.assertIsNone(session.result)

        apply_opponent_move(session, "b8a7")
        self.assertFalse(need_reply(session))
        self.assertTrue(user_turn(session))
        self.assertEqual(board_of(session).turn, chess.WHITE)

        apply_user_move(session, "c3c8")
        self.assertEqual(session.moves, ["g3c3", "b8a7", "c3c8"])
        self.assertEqual(session.result, "1-0")
        self.assertTrue(session.game_over)
        self.assertFalse(need_reply(session))
        self.assertFalse(user_turn(session))
        self.assertEqual(session.status, "playing")
        with self.assertRaises(ValueError):
            apply_user_move(session, "a2a3")

    def test_rejects_illegal_and_out_of_turn_moves(self) -> None:
        session = _session()
        with self.assertRaises(ValueError):
            apply_opponent_move(session, "d4e6")
        with self.assertRaises(ValueError):
            apply_user_move(session, "a1a8")
        with self.assertRaises(ValueError):
            board_of(_session(moves=["a1a8"]))

    def test_opponent_side_is_black(self) -> None:
        self.assertEqual(opponent_side(_session()), "black")

    def test_max_plies_truncation(self) -> None:
        session = _session(fen=chess.STARTING_FEN)
        for index, uci in enumerate(_long_sequence(MAX_PLIES)):
            if index % 2 == 0:
                apply_user_move(session, uci)
            else:
                apply_opponent_move(session, uci)
        self.assertEqual(len(session.moves), MAX_PLIES)
        self.assertEqual(session.result, "*")
        self.assertTrue(session.game_over)
        self.assertFalse(need_reply(session))
        self.assertFalse(user_turn(session))

    def test_to_dict_shape(self) -> None:
        session = _session(moves=["d4e6"])
        session.report = {"verdict": "good"}
        data = session.to_dict()
        self.assertEqual(
            set(data),
            {
                "id",
                "position_key",
                "n",
                "fen",
                "moves",
                "side",
                "status",
                "result",
                "plan",
                "source",
                "report",
                "error",
            },
        )
        self.assertEqual(data["position_key"], "curacao1962:12")
        self.assertEqual(data["n"], 12)
        self.assertEqual(data["moves"], ["d4e6"])
        self.assertEqual(data["side"], "white")
        self.assertEqual(data["result"], None)
        self.assertEqual(data["plan"], PLAN)
        self.assertEqual(data["report"], {"verdict": "good"})
        self.assertEqual(
            data["source"],
            {
                "title": "Curacao Candidates 1962",
                "url": "https://lichess.org/study/r4CqXLsW",
                "chapter_url": "https://lichess.org/study/r4CqXLsW/M6LfYPka",
                "white": "Pal Benko",
                "black": "Mikhail Tal",
            },
        )


class ReportTests(unittest.TestCase):
    def test_normalize_report_fills_defaults(self) -> None:
        report = normalize_report({})
        self.assertEqual(
            set(report), {"verdict", "summary", "plan_comment", "moments", "lesson"}
        )
        self.assertEqual(report["verdict"], "partial")
        self.assertEqual(report["moments"], [])
        self.assertTrue(report["summary"])
        self.assertTrue(report["plan_comment"])
        self.assertTrue(report["lesson"])

    def test_normalize_report_coerces_verdict(self) -> None:
        self.assertEqual(normalize_report({"verdict": " GOOD "})["verdict"], "good")
        self.assertEqual(normalize_report({"verdict": "wrong"})["verdict"], "wrong")
        self.assertEqual(normalize_report({"verdict": "нечто"})["verdict"], "partial")

    def test_normalize_report_cleans_moments(self) -> None:
        report = normalize_report(
            {
                "verdict": "good",
                "moments": [
                    {
                        "ply": "4",
                        "move_san": "Nf3",
                        "title": "  Точный ход ",
                        "comment": "контроль центра",
                        "alt_san": "",
                        "eval_before": 0.2,
                        "eval_after": None,
                    },
                    "мусор",
                    {"ply": "не число", "san": "e4"},
                ],
            }
        )
        self.assertEqual(len(report["moments"]), 2)
        first = report["moments"][0]
        self.assertEqual(first["ply"], 4)
        self.assertEqual(first["move_san"], "Nf3")
        self.assertEqual(first["title"], "Точный ход")
        self.assertIsNone(first["alt_san"])
        self.assertEqual(first["eval_before"], "0.2")
        self.assertIsNone(first["eval_after"])
        self.assertEqual(report["moments"][1]["ply"], 0)
        self.assertEqual(report["moments"][1]["move_san"], "e4")

    def test_build_report_prompt_contains_context_and_schema(self) -> None:
        session = _session()
        apply_user_move(session, "d4e6")
        san = chess.Board(POSITION_BY_KEY["curacao1962:12"].fen).san(
            chess.Move.from_uci("d4e6")
        )
        prompt = build_report_prompt(
            session,
            engine_facts=[
                {
                    "ply": 1,
                    "eval_before": {"cp": 20},
                    "eval_after": {"cp": 15},
                    "best_move_san": "Nxe6",
                }
            ],
            position=POSITION_BY_KEY["curacao1962:12"],
        )
        self.assertIn("человеческий шахматный тренер", prompt)
        self.assertIn(session.fen, prompt)
        self.assertIn("Pal Benko", prompt)
        self.assertIn("Mikhail Tal", prompt)
        self.assertIn("Curacao Candidates 1962", prompt)
        self.assertIn(PLAN, prompt)
        self.assertIn("d4e6", prompt)
        self.assertIn(san, prompt)
        self.assertIn("+0.20", prompt)
        self.assertIn("+0.15", prompt)
        self.assertIn("СТРОГО", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("форточк", prompt)


class MiddlegameDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "middlegame.db")
        self.db.init_db()
        self.addCleanup(self.db.close)

    def _db_session(
        self,
        session_id: str,
        *,
        user: str = "Alice",
        position_key: str = "curacao1962:12",
        created_at: str = "2026-01-01T00:00:00+00:00",
    ) -> MiddlegameSession:
        position = POSITION_BY_KEY[position_key]
        return MiddlegameSession(
            id=session_id,
            user=user,
            position_key=position.key,
            fen=position.fen,
            plan=PLAN,
            status="playing",
            created_at=created_at,
            updated_at=created_at,
        )

    def test_create_and_get_roundtrip(self) -> None:
        session = self._db_session("s1")
        session.moves = ["d4e6"]
        session.report = {"verdict": "good", "summary": "ок"}
        self.db.create_middlegame_session(session)
        loaded = self.db.get_middlegame_session("s1")
        self.assertIsInstance(loaded, MiddlegameSession)
        assert loaded is not None
        self.assertEqual(loaded.id, "s1")
        self.assertEqual(loaded.user, "Alice")
        self.assertEqual(loaded.position_key, "curacao1962:12")
        self.assertEqual(loaded.fen, session.fen)
        self.assertEqual(loaded.side, "white")
        self.assertEqual(loaded.plan, PLAN)
        self.assertEqual(loaded.moves, ["d4e6"])
        self.assertEqual(loaded.status, "playing")
        self.assertIsNone(loaded.result)
        self.assertEqual(loaded.report, {"verdict": "good", "summary": "ок"})
        self.assertIsNone(loaded.error)
        self.assertEqual(loaded.created_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(loaded.updated_at, "2026-01-01T00:00:00+00:00")

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.db.get_middlegame_session("nope"))

    def test_create_is_idempotent_upsert(self) -> None:
        session = self._db_session("s1")
        self.db.create_middlegame_session(session)
        session.plan = "Новый план после правки."
        session.moves = ["d4e6"]
        session.status = "playing"
        session.created_at = "2030-01-01T00:00:00+00:00"
        self.db.create_middlegame_session(session)
        loaded = self.db.get_middlegame_session("s1")
        assert loaded is not None
        self.assertEqual(loaded.plan, "Новый план после правки.")
        self.assertEqual(loaded.moves, ["d4e6"])
        self.assertEqual(loaded.created_at, "2026-01-01T00:00:00+00:00")
        with self.db._connection() as conn:
            count = conn.execute(
                "SELECT count(*) FROM middlegame_sessions"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_update_changes_state_and_preserves_created_at(self) -> None:
        session = self._db_session("s1")
        self.db.create_middlegame_session(session)
        session.moves = ["d4e6", "d8d6"]
        session.status = "analyzed"
        session.result = "1-0"
        session.report = {"verdict": "good"}
        session.error = "нет"
        self.db.update_middlegame_session(session)
        loaded = self.db.get_middlegame_session("s1")
        assert loaded is not None
        self.assertEqual(loaded.moves, ["d4e6", "d8d6"])
        self.assertEqual(loaded.status, "analyzed")
        self.assertEqual(loaded.result, "1-0")
        self.assertEqual(loaded.report, {"verdict": "good"})
        self.assertEqual(loaded.error, "нет")
        self.assertEqual(loaded.created_at, "2026-01-01T00:00:00+00:00")
        self.assertGreater(loaded.updated_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(loaded.updated_at, session.updated_at)

    def test_update_missing_is_noop(self) -> None:
        missing = self._db_session("missing")
        self.db.update_middlegame_session(missing)
        self.assertIsNone(self.db.get_middlegame_session("missing"))

    def test_recent_keys_ordering_user_and_limit(self) -> None:
        self.db.create_middlegame_session(
            self._db_session(
                "s1",
                position_key="curacao1962:1",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        self.db.create_middlegame_session(
            self._db_session(
                "s2",
                position_key="curacao1962:2",
                created_at="2026-01-01T00:00:01+00:00",
            )
        )
        self.db.create_middlegame_session(
            self._db_session(
                "s3",
                position_key="curacao1962:3",
                created_at="2026-01-01T00:00:02+00:00",
            )
        )
        self.db.create_middlegame_session(
            self._db_session(
                "s4",
                user="Bob",
                position_key="curacao1962:4",
                created_at="2026-01-01T00:00:03+00:00",
            )
        )
        self.assertEqual(
            self.db.recent_middlegame_keys("Alice"),
            ["curacao1962:3", "curacao1962:2", "curacao1962:1"],
        )
        self.assertEqual(
            self.db.recent_middlegame_keys("alice", limit=2),
            ["curacao1962:3", "curacao1962:2"],
        )
        self.assertEqual(
            self.db.recent_middlegame_keys("Bob"), ["curacao1962:4"]
        )
        self.assertEqual(self.db.recent_middlegame_keys("Alice", limit=0), [])

    def test_init_db_is_idempotent_for_middlegame(self) -> None:
        self.db.create_middlegame_session(self._db_session("s1"))
        self.db.init_db()
        self.db.init_db()
        self.assertIsNotNone(self.db.get_middlegame_session("s1"))
        with self.db._connection() as conn:
            count = conn.execute(
                "SELECT count(*) FROM middlegame_sessions"
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
