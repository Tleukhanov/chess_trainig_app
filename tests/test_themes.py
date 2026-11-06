"""Тесты детерминированной классификации тем хода (app.themes).

Чистые модульные тесты: движок не поднимается, multipv-линии поданы
вручную. Позиции синтетические, без привязки к реальным партиям игроков.
"""

from __future__ import annotations

import unittest

import chess

from app.themes import (
    MIN_THEME_SAMPLE,
    THEMES,
    classify_move_themes,
)


def _line(pv, win_white, mate=None, multipv=1):
    """Фейковая multipv-линия: win — win% для БЕЛЫХ, как в анализаторе."""
    cp = None if mate is not None else 30.0
    return {"pv": list(pv), "eval": {"cp": cp, "mate": mate}, "win": win_white, "multipv": multipv}


def _classify(fen, san, lines, drop=None):
    board = chess.Board(fen)
    return classify_move_themes(board, san, lines, drop=drop)


BACK_RANK = "6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"
FORK_FEN = "3q2kr/8/4P3/4N3/8/8/8/4K3 w - - 0 1"
QUIET_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
SAC_FEN = "r1bqkbnr/ppp1pppp/2np3p/8/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 1"


class TacticsTests(unittest.TestCase):
    def test_mate_in_one_is_tactics_and_attack(self) -> None:
        lines = [
            _line(["Ra8#"], 100.0, mate=1, multipv=1),
            _line(["Kb2"], 95.0, multipv=2),
        ]
        rec = _classify(BACK_RANK, "Ra8#", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["tactics"], "high")
        # мульти-метка: ход и тактический, и атакующий
        self.assertIn("tactics", rec["theme_used"])
        self.assertIn("attack", rec["theme_used"])
        self.assertIn("mate_in_1", rec["evidence"]["tactics"])

    def test_knight_fork_counts_as_tactics(self) -> None:
        lines = [
            _line(["Nf7", "Qd4"], 65.0, multipv=1),
            _line(["Ng4"], 55.0, multipv=2),
        ]
        rec = _classify(FORK_FEN, "Nf7", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertIn("fork", rec["evidence"]["tactics"])
        self.assertIn("tactics", rec["theme_used"])

    def test_near_equal_alternatives_are_not_punished(self) -> None:
        # Разрыв < NEAR_EQUAL_WIN_GAP: вторая линия равносильна первой —
        # это не «упущенная возможность», а выбор между равными ходами.
        lines = [
            _line(["d4"], 52.0, multipv=1),
            _line(["Nc3"], 51.0, multipv=2),
        ]
        rec = _classify(QUIET_FEN, "d4", lines, drop=1.0)
        self.assertNotIn("positional", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["positional"], "low")
        # Но ход реально усиливает центр — признак recorded, просто не принят.
        self.assertIn("center_control", rec["evidence"]["positional"])

    def test_quiet_move_without_forcing_lines_is_not_tactical(self) -> None:
        # Спокойный ход без шахов и взятий — не «тактика», даже если это лучший ход.
        fen = "r1bqkbnr/pppp1ppp/2n5/8/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 1"
        lines = [
            _line(["Nc3"], 50.0, multipv=1),
            _line(["d3"], 49.5, multipv=2),
        ]
        rec = _classify(fen, "Nc3", lines, drop=0.5)
        self.assertNotIn("tactics", rec["theme_opportunities"])


class PositionalTests(unittest.TestCase):
    def test_central_push_is_positional(self) -> None:
        lines = [
            _line(["d4", "exd4"], 55.0, multipv=1),
            _line(["c3"], 48.0, multipv=2),
        ]
        rec = _classify(QUIET_FEN, "d4", lines, drop=0.0)
        self.assertIn("positional", rec["theme_opportunities"])
        self.assertIn("center_control", rec["evidence"]["positional"])
        self.assertIn("positional", rec["theme_used"])

    def test_quiet_castling_has_no_false_tactics(self) -> None:
        fen = "r1bqk2r/pppp1ppp/2n2n2/4p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 1"
        lines = [
            _line(["O-O"], 54.0, multipv=1),
            _line(["d3"], 53.5, multipv=2),
        ]
        rec = _classify(fen, "O-O", lines, drop=0.0)
        self.assertNotIn("tactics", rec["theme_opportunities"])
        self.assertNotIn("attack", rec["theme_opportunities"])
        self.assertIn("king_castle", rec["evidence"]["positional"])


class AttackTests(unittest.TestCase):
    def test_sacrifice_without_engine_compensation_is_not_attack(self) -> None:
        # Ng5?! crashes into h6-pawn; engine даёт минимальный разрыв —
        # компенсации нет, атакой не считаем.
        lines = [
            _line(["Ng5", "hxg5"], 52.0, multipv=1),
            _line(["d4"], 51.0, multipv=2),
        ]
        rec = _classify(SAC_FEN, "Ng5", lines, drop=0.0)
        self.assertNotIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["attack"], "low")
        self.assertIn("sac_unverified", rec["evidence"]["attack"])

    def test_sacrifice_with_engine_backed_line_counts(self) -> None:
        lines = [
            _line(["Ng5", "hxg5", "Qh5"], 58.0, multipv=1),
            _line(["d4"], 48.0, multipv=2),
        ]
        rec = _classify(SAC_FEN, "Ng5", lines, drop=0.0)
        self.assertIn("attack", rec["theme_opportunities"])
        self.assertIn("sac_compensation", rec["evidence"]["attack"])

    def test_sacrifice_with_mate_in_pv_is_high_confidence(self) -> None:
        lines = [
            _line(["Ng5", "hxg5", "Qh5", "f6", "Qxg6#"], 100.0, mate=3, multipv=1),
            _line(["d4"], 40.0, multipv=2),
        ]
        rec = _classify(SAC_FEN, "Ng5", lines, drop=0.0)
        self.assertIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["attack"], "high")
        self.assertIn("mate_in_pv", rec["evidence"]["attack"])


class RecordShapeTests(unittest.TestCase):
    def test_record_has_stable_shape(self) -> None:
        rec = _classify(QUIET_FEN, "d4", [
            _line(["d4"], 55.0, multipv=1),
            _line(["c3"], 48.0, multipv=2),
        ], drop=3.5)
        self.assertEqual(rec["drop"], 3.5)
        self.assertIsNone(rec["note"])
        for key in (
            "theme_opportunities",
            "theme_used",
            "confidence",
            "drop",
            "evidence",
            "note",
        ):
            self.assertIn(key, rec)
        for theme in set(rec["theme_opportunities"]) | set(rec["theme_used"]):
            self.assertIn(theme, THEMES)

    def test_empty_lines_do_not_crash(self) -> None:
        rec = _classify(BACK_RANK, "Ra8#", [], drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])  # mate_in_1 — локальный признак

    def test_invalid_san_returns_empty_used(self) -> None:
        rec = _classify(QUIET_FEN, "Zz9", [], drop=None)
        self.assertEqual(rec["theme_used"], [])
        self.assertIsNone(rec["drop"])

    def test_min_sample_constant(self) -> None:
        self.assertGreaterEqual(MIN_THEME_SAMPLE, 5)


if __name__ == "__main__":
    unittest.main()
