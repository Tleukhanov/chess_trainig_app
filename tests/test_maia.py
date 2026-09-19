"""Тесты человекоподобных политик хода (app.maia).

Математика softmax проверяется на фейковом движке; интеграция с реальным
Stockfish — один лёгкий прогон на начальной позиции. Бинарь Maia не
запускается: проверяется только кейс EngineNotConfiguredError.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import chess

from app.maia import EngineNotConfiguredError, MaiaBinaryPolicy, MaiaLitePolicy

_MAIN_OPENINGS = frozenset({"e4", "d4", "Nf3", "c4"})


class FakeEngine:
    """Движок, возвращающий фиксированные линии анализа (без Stockfish)."""

    def __init__(self, lines: list[dict]) -> None:
        self._lines = lines

    def analyze_position(self, board: chess.Board) -> list[dict]:
        return list(self._lines)

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeEngine:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _fixed_lines() -> list[dict]:
    return [
        {"pv": ["e4"], "eval": {"cp": 40, "mate": None}, "win": 90.0, "multipv": 1},
        {"pv": ["d4"], "eval": {"cp": 20, "mate": None}, "win": 70.0, "multipv": 2},
        {"pv": ["Nf3"], "eval": {"cp": 0, "mate": None}, "win": 50.0, "multipv": 3},
    ]


class MaiaLiteSoftmaxTests(unittest.TestCase):
    def test_probs_spread_over_lines(self) -> None:
        lines = _fixed_lines()
        with MaiaLitePolicy(engine_factory=lambda: FakeEngine(lines)) as policy:
            probs = policy.probs(chess.Board(), k=3)

        self.assertEqual(len(probs), 3)
        self.assertAlmostEqual(sum(p for _, p in probs), 1.0, places=6)
        self.assertEqual(probs[0][0], "e4")
        self.assertGreater(probs[0][1], probs[1][1])
        self.assertGreater(probs[1][1], probs[2][1])
        self.assertGreater(probs[2][1], 0.0)
        # «человеческое» размазывание: top-ход не доминирует абсолютно.
        self.assertLess(probs[0][1], 0.85)
        self.assertGreater(probs[0][1], 0.6)

    def test_softmax_for_black_side_uses_complement_win(self) -> None:
        lines = _fixed_lines()
        board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")
        with MaiaLitePolicy(engine_factory=lambda: FakeEngine(lines)) as policy:
            probs = policy.probs(board, k=3)

        self.assertEqual(len(probs), 3)
        self.assertAlmostEqual(sum(p for _, p in probs), 1.0, places=6)
        # для чёрных порядок обращается: топ — линия с минимальным win белых.
        self.assertEqual(probs[0][0], "Nf3")
        self.assertEqual(probs[2][0], "e4")
        self.assertGreater(probs[0][1], probs[1][1])
        self.assertGreater(probs[1][1], probs[2][1])

    def test_skip_lines_without_pv(self) -> None:
        lines = [{"pv": [], "eval": {"cp": 10, "mate": None}, "win": 60.0, "multipv": 1}]
        with MaiaLitePolicy(engine_factory=lambda: FakeEngine(lines)) as policy:
            probs = policy.probs(chess.Board(), k=3)
        self.assertEqual(probs, [])

    def test_probs_without_context_raises(self) -> None:
        policy = MaiaLitePolicy()
        with self.assertRaisesRegex(RuntimeError, "движок"):
            policy.probs(chess.Board())


class MaiaBinaryPolicyTests(unittest.TestCase):
    def test_not_configured_empty_env(self) -> None:
        with patch.dict(os.environ, {"MAIA_PATH": ""}):
            with self.assertRaises(EngineNotConfiguredError) as ctx:
                MaiaBinaryPolicy()
            self.assertIn("MAIA_PATH", str(ctx.exception))

    def test_not_configured_missing_file(self) -> None:
        with patch.dict(os.environ, {"MAIA_PATH": ""}):
            with self.assertRaises(EngineNotConfiguredError) as ctx:
                MaiaBinaryPolicy(path=r"C:\no\such\maia.exe")
            self.assertIn("MAIA_PATH", str(ctx.exception))


class MaiaLiteStockfishIntegrationTests(unittest.TestCase):
    def test_probs_on_start_position(self) -> None:
        with MaiaLitePolicy(depth=6, time_ms=400, k_max=16) as policy:
            board = chess.Board()
            probs = policy.probs(board, k=8)

        self.assertGreaterEqual(len(probs), 1)
        self.assertAlmostEqual(sum(p for _, p in probs), 1.0, places=6)
        top_san = probs[0][0]
        board = chess.Board()
        self.assertIsNotNone(board.parse_san(top_san))
        self.assertTrue(any(san in _MAIN_OPENINGS for san, _ in probs))


if __name__ == "__main__":
    unittest.main()