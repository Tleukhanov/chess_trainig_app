"""Тесты линий движка (best_line/played_line) в анализе партии.

Интеграционные, но лёгкие: одна короткая партия, глубина 6, один вариант.
"""

from __future__ import annotations

import unittest

from app.analyzer import Engine, _san_key
from app.config import settings
from app.games import Game


def _make_game() -> Game:
    return Game(
        id="unit",
        rated=False,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        white={"name": "A"},
        black={"name": "B"},
        opening=None,
        eco=None,
        moves=["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
        clocks=[],
        user_color="white",
        opponent="B",
        user_rating=2100,
        user_rating_diff=0,
        result_for_user="win",
    )


class AnalyzerLinesTests(unittest.TestCase):
    def test_analyze_game_returns_lines(self) -> None:
        game = _make_game()
        with Engine(
            path=settings.stockfish_path,
            depth=6,
            multipv=1,
            time_ms=2000,
        ) as engine:
            analysis = engine.analyze_game(game)

        self.assertEqual(len(analysis.moves), len(game.moves))
        for move in analysis.moves:
            self.assertIsInstance(move.best_line, list)
            self.assertIsInstance(move.played_line, list)
            self.assertTrue(all(isinstance(x, str) for x in move.best_line))
            self.assertTrue(all(isinstance(x, str) for x in move.played_line))

    def test_played_line_matches_next_moves(self) -> None:
        game = _make_game()
        with Engine(
            path=settings.stockfish_path,
            depth=6,
            multipv=1,
            time_ms=2000,
        ) as engine:
            analysis = engine.analyze_game(game)

        for ply, move in enumerate(analysis.moves):
            self.assertEqual(move.played_line, game.moves[ply + 1 : ply + 1 + 5])

    def test_best_line_does_not_start_with_best_move(self) -> None:
        game = _make_game()
        with Engine(
            path=settings.stockfish_path,
            depth=6,
            multipv=1,
            time_ms=2000,
        ) as engine:
            analysis = engine.analyze_game(game)

        for move in analysis.moves:
            if not move.best_line:
                continue
            self.assertNotEqual(
                _san_key(move.best_line[0]),
                _san_key(move.best_move_san),
            )

    def test_to_dict_contains_lines(self) -> None:
        game = _make_game()
        with Engine(
            path=settings.stockfish_path,
            depth=6,
            multipv=1,
            time_ms=2000,
        ) as engine:
            analysis = engine.analyze_game(game)

        for move in analysis.moves:
            data = move.to_dict()
            self.assertIn("best_line", data)
            self.assertIn("played_line", data)
            self.assertIsInstance(data["best_line"], list)
            self.assertIsInstance(data["played_line"], list)


if __name__ == "__main__":
    unittest.main()