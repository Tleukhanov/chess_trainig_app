"""Тесты гибридного соперника (:mod:`app.hybrid`).

Maia и Stockfish подменяются фейками: проверяется только логика фильтра
(потеря cp, min_candidates, жёсткий потолок 2*threshold), нормализация весов,
легальность ходов и деградация в чистую Maia без движка.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import chess

from app.config import settings
from app.hybrid import HybridPolicy

BASE_CP = 30.0
BASE_BEST = "e4"


class FakeMaia:
    """Фейковая Maia: отдаёт заданный список (SAN, вероятность)."""

    def __init__(self, candidates: list[tuple[str, float]]) -> None:
        self.candidates = list(candidates)
        self.requested_k: list[int] = []

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        self.requested_k.append(k)
        return self.candidates[:k]


class FakeEngine:
    """Фейковый Stockfish: оценка берётся из ``eval_fn(board) -> (cp, best_san)``."""

    def __init__(self, eval_fn, *, fail: bool = False) -> None:
        self.eval_fn = eval_fn
        self.fail = fail
        self.calls: list[str] = []

    def __enter__(self) -> FakeEngine:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def close(self) -> None:
        pass

    def analyze_position(self, board: chess.Board) -> list[dict]:
        self.calls.append(board.fen())
        if self.fail:
            raise RuntimeError("stockfish unavailable")
        cp, best = self.eval_fn(board)
        return [
            {
                "pv": [best] if best else [],
                "eval": {"cp": cp, "mate": None},
                "win": 50.0,
                "multipv": 1,
            }
        ]


def _engine_for_move_evals(
    move_evals: dict[str, float],
    *,
    base_cp: float = BASE_CP,
    base_best: str = BASE_BEST,
    fail: bool = False,
) -> FakeEngine:
    """Движок, который знает оценку стартовой позиции и позиций после ходов (UCI)."""

    def eval_fn(board: chess.Board) -> tuple[float | None, str | None]:
        if not board.move_stack:
            return base_cp, base_best
        return move_evals.get(board.peek().uci()), None

    return FakeEngine(eval_fn, fail=fail)


class HybridPolicyTests(unittest.TestCase):
    def _run(
        self,
        candidates: list[tuple[str, float]],
        move_evals: dict[str, float],
        *,
        policy_kwargs: dict | None = None,
        fail: bool = False,
        board: chess.Board | None = None,
        engine: FakeEngine | None = None,
    ) -> tuple[list[tuple[str, float]], FakeMaia, FakeEngine]:
        maia = FakeMaia(candidates)
        engine = engine or _engine_for_move_evals(move_evals, fail=fail)
        with patch("app.hybrid._maia_policy", return_value=maia), patch(
            "app.analyzer.Engine", lambda *args, **kwargs: engine
        ):
            policy = HybridPolicy(2500, 2200, **(policy_kwargs or {}))
            result = policy.probs(board or chess.Board(), k=8)
        return result, maia, engine

    def test_filter_drops_blunder(self) -> None:
        """Ход с потерей больше threshold отбрасывается, остальные остаются."""
        result, _, _ = self._run(
            [("e4", 0.5), ("a3", 0.4), ("Nf3", 0.1)],
            {"e2e4": 25.0, "a2a3": -200.0, "g1f3": 20.0},
            policy_kwargs={"threshold_cp": 80, "min_candidates": 2},
        )

        sans = [san for san, _ in result]
        self.assertNotIn("a3", sans)
        self.assertEqual(set(sans), {"e4", "Nf3"})
        # Сортировка по вероятности Maia сохранена.
        self.assertEqual(sans, ["e4", "Nf3"])

    def test_min_candidates_is_filled_below_hard_cap(self) -> None:
        """Если после фильтра мало ходов — добиваем топом Maia в пределах 2*threshold."""
        result, _, _ = self._run(
            [("e4", 0.6), ("d4", 0.25), ("Nf3", 0.15)],
            {"e2e4": 30.0, "d2d4": 0.0, "g1f3": -50.0},
            policy_kwargs={"threshold_cp": 20, "min_candidates": 2},
        )

        sans = [san for san, _ in result]
        # d4: потеря 30 > 20, но 30 <= 2*20 — добит до min_candidates.
        self.assertEqual(sans, ["e4", "d4"])
        # Nf3: потеря 80 > 2*20 — жёсткий потолок не пробит.
        self.assertNotIn("Nf3", sans)

    def test_hard_cap_blocks_fill(self) -> None:
        """Добивка не выходит за 2*threshold, даже если min_candidates не набран."""
        result, _, _ = self._run(
            [("e4", 0.6), ("d4", 0.25), ("Nf3", 0.15)],
            {"e2e4": 30.0, "d2d4": 0.0, "g1f3": -50.0},
            policy_kwargs={"threshold_cp": 10, "min_candidates": 2},
        )

        self.assertEqual([san for san, _ in result], ["e4"])

    def test_all_blunders_returns_engine_best(self) -> None:
        """Все кандидаты — грубые зевки: играется лучший ход движка."""
        result, _, _ = self._run(
            [("a3", 0.6), ("h3", 0.4)],
            {"a2a3": -200.0, "h2h3": -300.0},
            policy_kwargs={"threshold_cp": 80, "min_candidates": 2},
        )

        self.assertEqual(result, [("e4", 1.0)])

    def test_stockfish_unavailable_falls_back_to_maia(self) -> None:
        """Stockfish недоступен: возвращается чистая Maia и пишется warning."""
        candidates = [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)]
        with self.assertLogs("app.hybrid", level="WARNING"):
            result, _, _ = self._run(candidates, {}, fail=True)

        self.assertEqual([san for san, _ in result], ["e4", "d4", "Nf3"])
        self.assertAlmostEqual(sum(prob for _, prob in result), 1.0, places=9)

    def test_illegal_san_skipped_and_output_legal(self) -> None:
        """Нелегальный SAN от Maia пропускается, остальные ходы легальны."""
        board = chess.Board()
        result, _, _ = self._run(
            [("e4", 0.5), ("not-a-move", 0.3), ("d4", 0.2)],
            {"e2e4": 25.0, "d2d4": 20.0},
            policy_kwargs={"threshold_cp": 80, "min_candidates": 2},
        )

        self.assertEqual([san for san, _ in result], ["e4", "d4"])
        for san, _ in result:
            self.assertIn(board.parse_san(san), board.legal_moves)
        self.assertAlmostEqual(sum(prob for _, prob in result), 1.0, places=9)

    def test_black_perspective_loss_sign(self) -> None:
        """Потеря для чёрных считается в их пользу, а не «за белых»."""
        board = chess.Board()
        board.push_san("e4")

        evals: dict[str, tuple[float | None, str | None]] = {
            board.fen(): (-30.0, "e5"),  # чёрным лучше на 30 cp
        }
        after_e5 = board.copy()
        after_e5.push_san("e5")
        evals[after_e5.fen()] = (-20.0, None)  # потеря для чёрных: 10 cp
        after_c5 = board.copy()
        after_c5.push_san("c5")
        evals[after_c5.fen()] = (-35.0, None)  # чёрные улучшили на 5 cp

        engine = FakeEngine(lambda b: evals[b.fen()])
        result, _, _ = self._run(
            [("e5", 0.6), ("c5", 0.4)],
            {},
            board=board,
            engine=engine,
            policy_kwargs={"threshold_cp": 3, "min_candidates": 2},
        )

        # e5 (10 cp) выше жёсткого потолка 6 cp, c5 — усиление позиции.
        self.assertEqual([san for san, _ in result], ["c5"])

    def test_engine_evaluations_cached_per_call(self) -> None:
        """В рамках одного probs каждая позиция оценивается ровно один раз."""
        result, _, engine = self._run(
            [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)],
            {"e2e4": 25.0, "d2d4": 20.0, "g1f3": 10.0},
            policy_kwargs={"threshold_cp": 80, "min_candidates": 1},
        )

        self.assertEqual(len(result), 3)
        # Стартовая позиция + три позиции после ходов, без повторного анализа.
        self.assertEqual(len(engine.calls), 4)
        self.assertEqual(len(set(engine.calls)), 4)
        self.assertEqual(engine.calls.count(chess.Board().fen()), 1)

    def test_defaults_and_bounds(self) -> None:
        """Дефолты берутся из настроек, отрицательные значения зажимаются."""
        policy = HybridPolicy(2500, 2200)
        self.assertEqual(policy.threshold_cp, settings.hybrid_threshold_cp)
        self.assertEqual(policy.depth, settings.hybrid_depth)
        self.assertEqual(policy.time_ms, settings.hybrid_time_ms)
        self.assertEqual(policy.min_candidates, settings.hybrid_min_candidates)

        bounded = HybridPolicy(
            2500,
            2200,
            threshold_cp=0,
            depth=-3,
            time_ms=-5,
            min_candidates=0,
        )
        self.assertEqual(bounded.threshold_cp, 1)
        self.assertEqual(bounded.depth, 1)
        self.assertEqual(bounded.time_ms, 0)
        self.assertEqual(bounded.min_candidates, 1)

        broken = HybridPolicy(2500, 2200, threshold_cp="oops")  # type: ignore[arg-type]
        self.assertEqual(broken.threshold_cp, settings.hybrid_threshold_cp)


if __name__ == "__main__":
    unittest.main()
