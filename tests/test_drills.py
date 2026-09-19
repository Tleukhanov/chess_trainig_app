"""Тесты дрелей: отбор ошибок, экспорт в PGN/JSON и сводка."""

from __future__ import annotations

import io
import json
import unittest

import chess
import chess.pgn

from app.drills import (
    Drill,
    collect_drills,
    drills_to_json,
    drills_to_pgn,
    summarize,
)
from app.games import Game

_MOVES = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6"]


def _move(
    san: str,
    cls: str,
    drop: float,
    win_before: float | None = None,
    win_after: float | None = None,
    best: str | None = "Qh5",
    **extra,
) -> dict:
    """Запись хода в формате summary() анализа."""
    wb = win_before if win_before is not None else 60.0
    wa = win_after if win_after is not None else wb - drop
    entry = {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": wb,
        "win_after": wa,
        "drop": drop,
        "classification": cls,
        "best_move_san": best,
        "best_eval": None,
        "best_win": wb,
        "cp_loss": drop,
        "clock_used": None,
        "time_pressure": None,
    }
    entry.update(extra)
    return entry


def _game(**overrides) -> Game:
    params = dict(
        id="d1",
        white={"name": "A"},
        black={"name": "B"},
        opening="Sicilian Defense",
        eco="B20",
        moves=list(_MOVES),
        user_color="white",
        result_for_user="win",
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        clocks=[],
        user_rating=2100,
        user_rating_diff=0,
        opponent="B",
    )
    params.update(overrides)
    return Game(**params)


def _basic_analysis() -> dict:
    """Анализ партии белыми: дрель — ход чёрных?? нет: ply4 (d4) зевок белых.

    Все ходы партии (e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6). ply4 — d4 (белые),
    классификация blunder, лучший ход Bc4, линия Bc4 Qb8.
    """
    moves: list[dict] = []
    for ply, san in enumerate(_MOVES):
        if ply == 4:
            moves.append(
                _move(
                    san,
                    "blunder",
                    30.0,
                    win_before=70.0,
                    win_after=40.0,
                    best="Bc4",
                    best_line=["Bc4", "Qb8"],
                )
            )
        elif ply % 2 == 0:
            moves.append(_move(san, "good", 0.0))
        else:
            moves.append(_move(san, "best", 0.0))
    return {
        "game_id": "d1",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "B",
        "acpl": 20.0,
        "accuracy": 80.0,
        "blunders": [4],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


class CollectDrillsTests(unittest.TestCase):
    def test_returns_blunder_drill_with_fen_before_move(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        self.assertEqual(len(drills), 1)
        drill = drills[0]
        self.assertEqual(drill.game_id, "d1")
        self.assertEqual(drill.ply, 4)
        self.assertEqual(drill.san_played, "d4")
        self.assertEqual(drill.best_move_san, "Bc4")
        self.assertEqual(drill.best_line, ["Bc4", "Qb8"])
        self.assertEqual(drill.color, "white")
        self.assertEqual(drill.fullmove, 3)
        self.assertEqual(drill.drop, 30.0)
        self.assertEqual(drill.win_before, 70.0)
        self.assertEqual(drill.opponent, "B")
        self.assertEqual(drill.opening, "Sicilian Defense")
        self.assertIn(" w ", drill.fen)
        self.assertNotIn(" b ", drill.fen)
        board = chess.Board(drill.fen)
        self.assertEqual(board.turn, chess.WHITE)

    def test_black_user_drill_has_black_to_move(self) -> None:
        game = _game(user_color="black", result_for_user="loss", winner="white")
        analysis = _basic_analysis()
        self.assertEqual(collect_drills(game, analysis), [])

    def test_ignores_small_drops_and_opponent_moves(self) -> None:
        analysis = _basic_analysis()
        moves = analysis["moves"]
        for ply, san in enumerate(_MOVES):
            if ply % 2 == 0:
                moves[ply]["classification"] = "inaccuracy"
                moves[ply]["drop"] = 6.0
        moves[4] = _move("d4", "blunder", 30.0, 70.0, 40.0, "Bc4", best_line=["Bc4", "Qb8"])
        drills = collect_drills(_game(), analysis)
        self.assertEqual(len(drills), 1)
        self.assertEqual(drills[0].ply, 4)

    def test_min_win_filters_low_win_before(self) -> None:
        analysis = _basic_analysis()
        analysis["moves"][4] = _move("d4", "blunder", 30.0, 40.0, 10.0, "Bc4")
        drills = collect_drills(_game(), analysis)
        self.assertEqual(drills, [])

    def test_missing_best_move_skipped(self) -> None:
        analysis = _basic_analysis()
        analysis["moves"][4] = _move("d4", "blunder", 30.0, 70.0, 40.0, best=None)
        self.assertEqual(collect_drills(_game(), analysis), [])

    def test_max_per_game_trims(self) -> None:
        game = _game(id="d2")
        moves: list[dict] = []
        for ply, san in enumerate(_MOVES):
            if ply % 2 == 0:
                moves.append(_move(san, "blunder", 25.0, 80.0, 55.0, "Qh5"))
            else:
                moves.append(_move(san, "good", 0.0))
        analysis = {"user_color": "white", "result_for_user": "win", "moves": moves}
        drills = collect_drills(game, analysis)
        self.assertEqual(len(drills), 4)  # только ходы белых
        limited = collect_drills(game, analysis, max_per_game=2)
        self.assertEqual(limited, drills[:2])

    def test_no_limit_when_max_per_game_zero(self) -> None:
        game = _game(id="d2")
        moves: list[dict] = []
        for ply, san in enumerate(_MOVES):
            if ply % 2 == 0:
                moves.append(_move(san, "blunder", 25.0, 80.0, 55.0, "Qh5"))
            else:
                moves.append(_move(san, "good", 0.0))
        analysis = {"user_color": "white", "result_for_user": "win", "moves": moves}
        drills = collect_drills(game, analysis, max_per_game=0)
        self.assertEqual(len(drills), 4)

    def test_old_cache_without_best_line_is_fine(self) -> None:
        analysis = _basic_analysis()
        del analysis["moves"][4]["best_line"]
        drills = collect_drills(_game(), analysis)
        self.assertEqual(len(drills), 1)
        self.assertEqual(drills[0].best_line, [])


class DrillsToPgnTests(unittest.TestCase):
    def test_pgn_has_headers_and_comment(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        text = drills_to_pgn(drills)
        self.assertIn("[Event", text)
        self.assertIn("Chess Trainer — дрель 1", text)
        self.assertIn("[FEN", text)
        self.assertIn("[SetUp", text)
        self.assertIn("[Result", text)
        self.assertIn("Сыграно", text)
        self.assertIn("Лучший ход", text)
        self.assertIn("Bc4", text)
        self.assertIn("Задача: Найди лучший ход", text)

    def test_pgn_parses_back_and_has_moves(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        text = drills_to_pgn(drills)
        game = chess.pgn.read_game(io.StringIO(text))
        self.assertIsNotNone(game)
        assert game is not None
        mainline = list(game.mainline())
        self.assertGreaterEqual(len(mainline), 1)
        first = mainline[0].move
        self.assertIsNotNone(first)
        self.assertEqual(game.board().san(first), "Bc4")

    def test_comment_attached_to_first_move(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        text = drills_to_pgn(drills)
        game = chess.pgn.read_game(io.StringIO(text))
        self.assertIsNotNone(game)
        assert game is not None
        mainline = list(game.mainline())
        self.assertTrue(mainline)
        first = mainline[0]
        comment = first.comment if hasattr(first, "comment") else ""
        self.assertIn("Сыграно", comment)
        self.assertIn("Лучший ход", comment)
        self.assertIn("Найди лучший ход", comment)
        self.assertIn("ход пользователя 3", comment)


class DrillsToJsonTests(unittest.TestCase):
    def test_json_valid_and_matches_count(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        raw = drills_to_json(drills)
        data = json.loads(raw)
        self.assertEqual(len(data), 1)
        item = data[0]
        self.assertEqual(item["game_id"], "d1")
        self.assertEqual(item["san_played"], "d4")
        self.assertEqual(item["best_move_san"], "Bc4")
        self.assertEqual(item["fullmove"], 3)
        self.assertEqual(item["color"], "white")
        self.assertIn(" w ", item["fen"])

    def test_two_drills_two_objects(self) -> None:
        first = Drill(
            game_id="a",
            ply=4,
            fen=chess.Board().fen(),
            color="white",
            san_played="d4",
            classification="blunder",
            drop=30.0,
            win_before=70.0,
            win_after=40.0,
            best_move_san="Bc4",
            fullmove=3,
        )
        second = Drill(
            game_id="b",
            ply=6,
            fen=chess.Board().fen(),
            color="white",
            san_played="O-O",
            classification="mistake",
            drop=20.0,
            win_before=60.0,
            win_after=40.0,
            best_move_san="Bc4",
            fullmove=4,
        )
        data = json.loads(drills_to_json([first, second]))
        self.assertEqual(len(data), 2)


class SummarizeTests(unittest.TestCase):
    def test_counts_and_top(self) -> None:
        drills = collect_drills(_game(), _basic_analysis())
        summary = summarize(drills)
        self.assertEqual(summary["found"], 1)
        self.assertEqual(summary["games"], 1)
        self.assertEqual(summary["avg_drop"], 30.0)
        self.assertEqual(summary["top"][0]["game_id"], "d1")
        self.assertEqual(summary["top"][0]["san"], "d4")
        self.assertEqual(summary["top"][0]["best"], "Bc4")

    def test_empty_drills(self) -> None:
        summary = summarize([])
        self.assertEqual(summary["found"], 0)
        self.assertEqual(summary["games"], 0)
        self.assertIsNone(summary["avg_drop"])
        self.assertEqual(summary["top"], [])


if __name__ == "__main__":
    unittest.main()