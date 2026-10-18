"""Тесты тренера дебютов: стилевой ре-ранкер и сессия партии (app.openings).

Всё офлайн: политика соперника — фейк с методом ``probs``, ни модель, ни сеть
не нужны.
"""

from __future__ import annotations

import random
import unittest

import chess

from app.openings import (
    DEFAULT_LAM,
    MAX_LAM,
    MAX_STYLE_DELTA,
    MIN_RATIO,
    STYLES,
    STYLE_LABELS_RU,
    OpeningGame,
    board_from_moves,
    game_over,
    is_finished,
    new_game,
    opponent_color,
    opponent_move,
    pick_move,
    rerank,
    result,
    style_score,
    user_color,
    user_move,
)

# Позиция после 1.e4 e5 2.Bc4 Nc6: есть шах-жертва Bxf7+ и тихий Nf3/Qh5.
_CHECK_SAC_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 3 3"
# Позиция с доступным равным разменом Nxe5 и жертвой Bxf7+.
_TRADE_SAC_FEN = "4k3/3p1ppp/8/4n3/2B5/5N2/PPPP1PPP/4K3 w - - 0 1"
# Нимпович после 1.e4 Nc6 2.d4 (ход чёрных): Maia переоценивает откат Nb8.
_NIMZO_FEN = "r1bqkbnr/pppppppp/2n5/8/3PP3/8/PPP2PPP/RNBQKBNR b KQkq - 0 2"

# Детский мат (белые), 7 полуходов: мат задолго до лимита 40 полуходов.
_SCHOLARS_MATE = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
# Мат в два хода (чёрные): мат до лимита 40 полуходов.
_FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]
# Самый быстрый пат (19 полуходов): ничья до лимита 40 полуходов.
_QUICK_STALEMATE = [
    "e3", "a5", "Qh5", "Ra6", "Qxa5", "h5", "Qxc7", "Rah6", "h4", "f6",
    "Qxd7+", "Kf7", "Qxb7", "Qd3", "Qxb8", "Qh7", "Qxc8", "Kg6", "Qe6",
]


class FakePolicy:
    """Фейковая политика Maia: топ legal-ходы с быстро убывающими весами."""

    def __init__(self, k_default: int = 8) -> None:
        self.k_default = k_default
        self.calls = 0

    def probs(self, board: chess.Board, *, k: int = 8) -> list[tuple[str, float]]:
        self.calls += 1
        legal = list(board.legal_moves)
        n = max(1, min(k, len(legal)))
        raw = [(board.san(move), 1.0 / (i + 1)) for i, move in enumerate(legal[:n])]
        total = sum(prob for _, prob in raw)
        return [(san, prob / total) for san, prob in raw]


def _first_legal_san(board: chess.Board) -> str:
    return board.san(next(iter(board.legal_moves)))


class StyleDirectionTests(unittest.TestCase):
    def test_aggressive_prefers_check_sacrifice_over_quiet(self) -> None:
        board = chess.Board(_CHECK_SAC_FEN)
        quiet = board.parse_san("Nf3")
        strong = board.parse_san("Bxf7+")
        self.assertGreater(
            style_score(board, strong, "aggressive"),
            style_score(board, quiet, "aggressive"),
        )
        # ре-ранк тоже поднимает шах-жертву наверх
        candidates = [("Nf3", 0.4), ("Bxf7+", 0.3), ("Qh5", 0.3)]
        ranked = rerank(board, candidates, "aggressive", lam=DEFAULT_LAM)
        self.assertEqual(ranked[0][0], "Bxf7+")

    def test_solid_prefers_trade_over_sacrifice(self) -> None:
        board = chess.Board(_TRADE_SAC_FEN)
        trade = board.parse_san("Nxe5")
        sacrifice = board.parse_san("Bxf7+")
        self.assertGreater(
            style_score(board, trade, "solid"),
            style_score(board, sacrifice, "solid"),
        )
        candidates = [("Bxf7+", 0.5), ("Nxe5", 0.3), ("d4", 0.2)]
        ranked = rerank(board, candidates, "solid", lam=DEFAULT_LAM)
        self.assertEqual(ranked[0][0], "Nxe5")

    def test_universal_returns_zero(self) -> None:
        board = chess.Board(_CHECK_SAC_FEN)
        for san in ("Bxf7+", "Nf3", "Qh5"):
            self.assertEqual(style_score(board, board.parse_san(san), "universal"), 0.0)

    def test_unknown_style_raises(self) -> None:
        board = chess.Board()
        with self.assertRaises(ValueError):
            style_score(board, board.parse_san("e4"), "nope")

    def test_styles_and_labels_cover_five(self) -> None:
        self.assertEqual(len(STYLES), 5)
        self.assertEqual(set(STYLES), set(STYLE_LABELS_RU))


class RerankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = chess.Board()

    def test_normalized_and_sorted(self) -> None:
        candidates = [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)]
        ranked = rerank(self.board, candidates, "positional")
        self.assertAlmostEqual(sum(p for _, p in ranked), 1.0, places=9)
        probs = [p for _, p in ranked]
        self.assertEqual(probs, sorted(probs, reverse=True))

    def test_universal_preserves_maia_order_and_probs(self) -> None:
        candidates = [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)]
        ranked = rerank(self.board, candidates, "universal", lam=DEFAULT_LAM)
        self.assertEqual([s for s, _ in ranked], ["e4", "d4", "Nf3"])
        for (san_in, p_in), (san_out, p_out) in zip(candidates, ranked):
            self.assertEqual(san_in, san_out)
            self.assertAlmostEqual(p_in, p_out, places=9)

    def test_lam_zero_preserves_ordering(self) -> None:
        candidates = [("Nf3", 0.6), ("e4", 0.25), ("d4", 0.15)]
        ranked = rerank(self.board, candidates, "aggressive", lam=0.0)
        self.assertEqual([s for s, _ in ranked], ["Nf3", "e4", "d4"])

    def test_lam_is_clamped(self) -> None:
        candidates = [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)]
        negative = rerank(self.board, candidates, "aggressive", lam=-5.0)
        zero = rerank(self.board, candidates, "aggressive", lam=0.0)
        self.assertEqual(negative, zero)
        big = rerank(self.board, candidates, "aggressive", lam=1e9)
        self.assertAlmostEqual(sum(p for _, p in big), 1.0, places=9)

    def test_empty_candidates(self) -> None:
        self.assertEqual(rerank(self.board, [], "aggressive"), [])


class PickMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = chess.Board()
        self.candidates = [("e4", 0.5), ("d4", 0.3), ("Nf3", 0.2)]

    def test_deterministic_with_seed(self) -> None:
        first = pick_move(
            self.board, self.candidates, "aggressive", rng=random.Random(1234)
        )
        second = pick_move(
            self.board, self.candidates, "aggressive", rng=random.Random(1234)
        )
        self.assertEqual(first, second)

    def test_returns_legal_candidate_and_reason(self) -> None:
        sans = {s for s, _ in self.candidates}
        for seed in range(20):
            san, reason = pick_move(
                self.board, self.candidates, "universal", rng=random.Random(seed)
            )
            self.assertIn(san, sans)
            self.assertIsInstance(reason, str)
            self.assertTrue(reason)
            self.board.parse_san(san)  # не бросает

    def test_empty_candidates_raises(self) -> None:
        with self.assertRaises(ValueError):
            pick_move(self.board, [], "universal")

    def test_reason_mentions_king_attack(self) -> None:
        board = chess.Board(_CHECK_SAC_FEN)
        candidates = [("Bxf7+", 1.0)]
        san, reason = pick_move(
            board, candidates, "aggressive", rng=random.Random(0)
        )
        self.assertEqual(san, "Bxf7+")
        self.assertIn("корол", reason)


class PlausibilityTests(unittest.TestCase):
    """Соперник не должен выбирать неправдоподобные ходы Maia (реальный баг)."""

    def test_tail_move_is_dropped_by_default_ratio(self) -> None:
        board = chess.Board()
        candidates = [("e4", 0.95), ("d4", 0.03), ("Nf3", 0.02)]
        ranked = rerank(board, candidates, "universal")
        self.assertEqual([s for s, _ in ranked], ["e4"])

    def test_implausible_styled_move_not_picked(self) -> None:
        # Шах-жертва Bxf7+ стилево привлекательна, но Maia её почти не даёт.
        board = chess.Board(_CHECK_SAC_FEN)
        candidates = [("Nf3", 0.97), ("Bxf7+", 0.02), ("Qh5", 0.01)]
        for seed in range(25):
            san, _ = pick_move(
                board, candidates, "aggressive", rng=random.Random(seed)
            )
            self.assertNotEqual(san, "Bxf7+")

    def test_low_probability_retreat_not_selected(self) -> None:
        board = chess.Board(_NIMZO_FEN)
        candidates = [("d5", 0.9), ("Nf6", 0.08), ("Nb8", 0.02)]
        for seed in range(25):
            san, _ = pick_move(
                board, candidates, "positional", rng=random.Random(seed)
            )
            self.assertNotEqual(san, "Nb8")

    def test_high_probability_retreat_still_avoided(self) -> None:
        # Прямой аналог репорта: Maia ставит откат ...Nb8 на первое место (p=0.9),
        # но тренер обязан сыграть развивающий ход.
        board = chess.Board(_NIMZO_FEN)
        candidates = [("Nb8", 0.90), ("d5", 0.05), ("Nf6", 0.03), ("e6", 0.02)]
        chosen = {
            pick_move(board, candidates, "positional", rng=random.Random(seed))[0]
            for seed in range(60)
        }
        self.assertNotIn("Nb8", chosen)
        self.assertTrue(chosen & {"d5", "Nf6", "e6"})
        self.assertTrue(chosen <= {"d5", "Nf6", "e6"})

    def test_development_guard_keeps_only_candidate(self) -> None:
        # Откат нельзя выбросить, если это единственный кандидат (напр. фолбэк).
        board = chess.Board(_NIMZO_FEN)
        ranked = rerank(board, [("Nb8", 1.0)], "universal")
        self.assertEqual([s for s, _ in ranked], ["Nb8"])

    def test_style_still_shifts_among_plausible_moves(self) -> None:
        board = chess.Board(_NIMZO_FEN)
        candidates = [("d5", 0.5), ("Nf6", 0.3), ("e6", 0.2)]
        self.assertEqual(rerank(board, candidates, "positional", lam=0.0)[0][0], "d5")
        # Позиционный стиль предпочитает развитие Nf6, но выбор остаётся в Maia-наборе.
        ranked = rerank(board, candidates, "positional")
        self.assertEqual(ranked[0][0], "Nf6")
        self.assertEqual({s for s, _ in ranked}, {"d5", "Nf6", "e6"})

    def test_style_delta_clamped(self) -> None:
        board = chess.Board(_NIMZO_FEN)
        candidates = [("d5", 0.5), ("Nf6", 0.3), ("e6", 0.2)]
        tight = rerank(
            board, candidates, "positional", lam=MAX_LAM, max_style_delta=0.0
        )
        self.assertEqual([s for s, _ in tight], ["d5", "Nf6", "e6"])
        loose = rerank(
            board, candidates, "positional", lam=MAX_LAM, max_style_delta=99.0
        )
        self.assertAlmostEqual(sum(p for _, p in loose), 1.0, places=9)

    def test_constants_are_sane(self) -> None:
        self.assertGreater(MIN_RATIO, 0.0)
        self.assertLess(MIN_RATIO, 1.0)
        self.assertGreater(MAX_STYLE_DELTA, 0.0)


class SessionTests(unittest.TestCase):
    def test_new_game_validates(self) -> None:
        with self.assertRaises(ValueError):
            new_game(1500, 1500, "nope", "white")
        with self.assertRaises(ValueError):
            new_game(1500, 1500, "solid", "green")
        game = new_game(1500, 1600, "solid", "black", plies=30)
        self.assertEqual(game.plies, 30)
        self.assertEqual(game.moves, [])

    def test_user_move_accepts_and_rejects(self) -> None:
        game = new_game(1500, 1500, "universal", "white")
        user_move(game, "e4")
        self.assertEqual(game.moves, ["e4"])
        # теперь ход соперника (чёрных) — пользователь не может ходить
        with self.assertRaises(ValueError):
            user_move(game, "e5")
        # нелегальный ход, когда снова ход пользователя
        game2 = new_game(1500, 1500, "universal", "white")
        with self.assertRaises(ValueError):
            user_move(game2, "e5")
        with self.assertRaises(ValueError):
            user_move(game2, "Zz9")

    def test_opponent_move_wrong_turn_raises(self) -> None:
        game = new_game(1500, 1500, "universal", "white")
        with self.assertRaises(ValueError):
            opponent_move(game, FakePolicy())

    def test_result_none_until_finished(self) -> None:
        game = new_game(1500, 1500, "universal", "white", plies=2)
        self.assertIsNone(result(game))
        self.assertFalse(is_finished(game))
        user_move(game, "e4")
        board = board_from_moves(game.moves)
        opponent_move(game, FakePolicy(), rng=random.Random(1))
        self.assertTrue(is_finished(game))
        self.assertEqual(result(game), "*")
        self.assertEqual(len(game.moves), 2)

    def test_colors(self) -> None:
        white = new_game(1500, 1500, "universal", "white")
        black = new_game(1500, 1500, "universal", "black")
        self.assertEqual(user_color(white), chess.WHITE)
        self.assertEqual(opponent_color(white), chess.BLACK)
        self.assertEqual(user_color(black), chess.BLACK)
        self.assertEqual(opponent_color(black), chess.WHITE)

    def test_scripted_playthrough_40_plies(self) -> None:
        policy = FakePolicy()
        rng = random.Random(7)
        for color in ("white", "black"):
            game = new_game(1500, 1500, "aggressive", color, plies=40)
            while not is_finished(game):
                board = board_from_moves(game.moves)
                if board.turn == user_color(game):
                    user_move(game, _first_legal_san(board))
                else:
                    san, _ = opponent_move(game, policy, rng=rng)
                    board.parse_san(san)
            self.assertEqual(len(game.moves), 40)
            self.assertIsInstance(game, OpeningGame)
            final = board_from_moves(game.moves)
            self.assertTrue(final.is_valid())
            self.assertEqual(result(game), "*")

    def test_board_from_moves_replays_san(self) -> None:
        moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
        board = board_from_moves(moves)
        self.assertTrue(board.is_valid())
        self.assertEqual(board.turn, chess.BLACK)
        self.assertEqual(board.fullmove_number, 3)


class TerminalPositionTests(unittest.TestCase):
    """Мат/пат до лимита полуходов завершают партию (реальный баг: доска вставала)."""

    def _game(self, moves: list[str]) -> OpeningGame:
        game = new_game(1500, 1500, "universal", "white", plies=40)
        game.moves = list(moves)
        return game

    def test_checkmate_white_detected_before_ply_limit(self) -> None:
        game = self._game(_SCHOLARS_MATE)
        self.assertLess(len(game.moves), game.plies)
        self.assertTrue(game_over(game))
        self.assertTrue(is_finished(game))
        self.assertEqual(result(game), "1-0")

    def test_checkmate_black_detected_before_ply_limit(self) -> None:
        game = self._game(_FOOLS_MATE)
        self.assertLess(len(game.moves), game.plies)
        self.assertTrue(game_over(game))
        self.assertTrue(is_finished(game))
        self.assertEqual(result(game), "0-1")

    def test_stalemate_detected_before_ply_limit(self) -> None:
        game = self._game(_QUICK_STALEMATE)
        self.assertLess(len(game.moves), game.plies)
        self.assertTrue(game_over(game))
        self.assertTrue(is_finished(game))
        self.assertEqual(result(game), "1/2-1/2")

    def test_ply_limit_truncation_still_returns_star(self) -> None:
        game = new_game(1500, 1500, "universal", "white", plies=3)
        game.moves = ["e4", "e5", "Nf3"]
        self.assertFalse(game_over(game))  # позиция не терминальна
        self.assertTrue(is_finished(game))  # но лимит полуходов достигнут
        self.assertEqual(result(game), "*")

    def test_non_terminal_position_not_finished(self) -> None:
        game = self._game(["e4", "e5", "Nf3"])
        self.assertFalse(game_over(game))
        self.assertFalse(is_finished(game))
        self.assertIsNone(result(game))


if __name__ == "__main__":
    unittest.main()
