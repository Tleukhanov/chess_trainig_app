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


class BlackToMoveTests(unittest.TestCase):
    """Класификатор симметричен: вторая сторона тоже получает метки."""

    def test_black_central_push_is_positional(self) -> None:
        board = chess.Board()
        board.push_san("e4")
        self.assertEqual(board.turn, chess.BLACK)
        lines = [
            _line(["e5", "Nc3"], 35.0, multipv=1),
            _line(["c5"], 45.0, multipv=2),
        ]
        rec = classify_move_themes(board, "e5", lines, drop=0.0)
        self.assertIn("positional", rec["theme_opportunities"])
        self.assertIn("center_control", rec["evidence"]["positional"])
        self.assertIn("positional", rec["theme_used"])

    def test_black_mate_in_one_is_tactics_and_attack(self) -> None:
        # Зеркало BACK_RANK для чёрных: win%_WHITE первой линии — 0 (мат),
        # значит для чёрных это win% 100 и классический mate.
        fen = "r6k/8/8/8/8/8/5PPP/6K1 b - - 0 1"
        board = chess.Board(fen)
        board.push_san("Ra1#")
        self.assertTrue(board.is_checkmate())
        lines = [
            _line(["Ra1#"], 0.0, mate=-1, multipv=1),
            _line(["Rf8"], 30.0, multipv=2),
        ]
        rec = _classify(fen, "Ra1#", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["tactics"], "high")
        self.assertIn("tactics", rec["theme_used"])
        self.assertIn("mate_in_1", rec["evidence"]["tactics"])

    def test_black_quiet_move_without_forcing_lines_is_not_tactical(self) -> None:
        board = chess.Board()
        board.push_san("e4")
        # Скромный ход без шахов/взятий — не тактика и для чёрных.
        lines = [
            _line(["d6"], 48.0, multipv=1),
            _line(["c6"], 47.5, multipv=2),
        ]
        rec = classify_move_themes(board, "d6", lines, drop=0.5)
        self.assertNotIn("tactics", rec["theme_opportunities"])


class MalformedLinesTests(unittest.TestCase):
    """Мусор в multipv-линиях не должен ронять классификатор.

    Порядок линий — ответственность звонящего: классификатор берёт
    первую линию за лучшую без пересортировки по ``multipv``. Здесь
    фиксируем это поведение, а не исправляем сортировку (см. задачи
    ядра классификатора).
    """

    def test_non_dict_lines_are_ignored(self) -> None:
        rec = _classify(QUIET_FEN, "d4", [
            None,
            "not a line",
            42,
            _line(["d4"], 55.0, multipv=1),
            _line(["c3"], 48.0, multipv=2),
        ], drop=0.0)
        self.assertIn("positional", rec["theme_opportunities"])
        self.assertIn("positional", rec["theme_used"])

    def test_malformed_win_and_pv_do_not_crash(self) -> None:
        rec = _classify(QUIET_FEN, "d4", [
            {"pv": "notalist", "win": "bad", "eval": {"cp": None, "mate": None}},
            {"pv": [123, None], "win": None, "eval": {}},
            _line(["d4"], 55.0, multipv=1),
        ], drop=None)
        self.assertIn("positional", rec["theme_opportunities"])
        self.assertIsNone(rec["drop"])

    def test_empty_and_none_lines_still_use_local_signals(self) -> None:
        for lines in ([], None):
            with self.subTest(lines=lines):
                rec = _classify(QUIET_FEN, "d4", lines, drop=None)
                self.assertIn("positional", rec["theme_opportunities"])
                self.assertIn("positional", rec["theme_used"])

    def test_unsorted_lines_are_taken_in_given_order_without_crash(self) -> None:
        # multipv спутан: вторая линия фактически сильнее первой для белых,
        # поэтому разрыв win% отрицателен и тему уходит в low (но без падения).
        rec = _classify(QUIET_FEN, "d4", [
            _line(["c3"], 48.0, multipv=2),
            _line(["d4"], 55.0, multipv=1),
        ], drop=0.0)
        self.assertIn("positional", rec["evidence"])
        self.assertEqual(rec["confidence"]["positional"], "low")
        # Отсортированный вариант даёт medium: разница именно в порядке.
        rec_sorted = _classify(QUIET_FEN, "d4", [
            _line(["d4"], 55.0, multipv=1),
            _line(["c3"], 48.0, multipv=2),
        ], drop=0.0)
        self.assertIn("positional", rec_sorted["theme_opportunities"])


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
            "opportunity_evidence",
            "used_evidence",
            "opportunity_confidence",
            "used_confidence",
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


class RookMovementTests(unittest.TestCase):
    def test_static_rook_on_open_file_does_not_count_without_rook_move(self) -> None:
        fen = "6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"
        lines = [_line(["Kb2"], 55.0, multipv=1), _line(["Kh2"], 48.0, multipv=2)]
        rec = _classify(fen, "Kb2", lines, drop=0.0)
        self.assertNotIn("rook_open_file", rec["evidence"].get("positional", []))
        self.assertNotIn("rook_seventh", rec["evidence"].get("positional", []))
        self.assertNotIn("positional", rec["theme_opportunities"])

    def test_rook_landing_on_open_file_counts(self) -> None:
        fen = "6k1/8/8/8/8/8/7p/4R2K w - - 0 1"
        lines = [_line(["Ra1"], 55.0, multipv=1), _line(["Re2"], 48.0, multipv=2)]
        rec = _classify(fen, "Ra1", lines, drop=0.0)
        self.assertIn("rook_open_file", rec["evidence"]["positional"])
        self.assertIn("positional", rec["theme_opportunities"])

    def test_rook_landing_on_seventh_counts(self) -> None:
        fen = "6k1/8/8/8/8/8/8/R6K w - - 0 1"
        lines = [_line(["Ra7"], 55.0, multipv=1), _line(["Kb2"], 48.0, multipv=2)]
        rec = _classify(fen, "Ra7", lines, drop=0.0)
        self.assertIn("rook_seventh", rec["evidence"]["positional"])


class PawnStormTests(unittest.TestCase):
    def test_static_pawn_storm_does_not_fire(self) -> None:
        fen = "6k1/5ppp/8/8/5PP1/8/8/6K1 w - - 0 1"
        lines = [_line(["Kg2"], 55.0, multipv=1), _line(["Kh1"], 48.0, multipv=2)]
        rec = _classify(fen, "Kg2", lines, drop=0.0)
        self.assertNotIn("pawn_storm", rec["evidence"].get("attack", []))

    def test_pawn_advancing_into_storm_zone_counts(self) -> None:
        fen = "6k1/5ppp/8/8/8/5P2/8/6K1 w - - 0 1"
        lines = [_line(["f4"], 55.0, multipv=1), _line(["Kh1"], 48.0, multipv=2)]
        rec = _classify(fen, "f4", lines, drop=0.0)
        self.assertIn("pawn_storm", rec["evidence"]["attack"])


class TacticsStrengthTests(unittest.TestCase):
    def test_single_check_is_not_tactics_opportunity(self) -> None:
        fen = "4k3/8/8/8/8/8/8/R3K3 w - - 0 1"
        lines = [_line(["Ra8+"], 55.0, multipv=1), _line(["Rb1"], 48.0, multipv=2)]
        rec = _classify(fen, "Ra8+", lines, drop=0.0)
        self.assertIn("check", rec["evidence"]["tactics"])
        self.assertNotIn("tactics", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["tactics"], "low")

    def test_single_pawn_capture_is_not_tactics_opportunity(self) -> None:
        fen = "4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1"
        lines = [_line(["exd5"], 55.0, multipv=1), _line(["e5"], 48.0, multipv=2)]
        rec = _classify(fen, "exd5", lines, drop=0.0)
        self.assertNotIn("tactics", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["tactics"], "low")

    def test_capture_major_is_tactics_opportunity(self) -> None:
        fen = "6k1/8/8/8/8/8/n7/R6K w - - 0 1"
        lines = [_line(["Rxa2"], 55.0, multipv=1), _line(["Kb2"], 48.0, multipv=2)]
        rec = _classify(fen, "Rxa2", lines, drop=0.0)
        self.assertIn("capture_major", rec["evidence"]["tactics"])
        self.assertIn("tactics", rec["theme_opportunities"])


class NearEqualAndDropTests(unittest.TestCase):
    def test_fork_does_not_bypass_near_equal_when_drop_is_small(self) -> None:
        lines = [_line(["Nf7", "Qd4"], 53.0, multipv=1), _line(["Ng4"], 52.0, multipv=2)]
        rec = _classify(FORK_FEN, "Nf7", lines, drop=1.0)
        self.assertIn("fork", rec["opportunity_evidence"]["tactics"])
        self.assertNotIn("tactics", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["tactics"], "low")

    def test_blunder_drop_prevents_near_equal_demotion_for_opportunity(self) -> None:
        # Разрыв между двумя лучшими линиями мал, но сыгранный ход —
        # провал уровня блундера: возможность не должна уходить в unknown.
        lines = [_line(["Nf7", "Qd4"], 53.0, multipv=1), _line(["Ng4"], 52.0, multipv=2)]
        rec = _classify(FORK_FEN, "Ng4", lines, drop=25.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertNotIn("tactics", rec["theme_used"])

    def test_gap_none_single_line_is_low_unless_mate_in_1(self) -> None:
        lines = [_line(["d4"], 55.0, multipv=1)]
        rec = _classify(QUIET_FEN, "d4", lines, drop=0.0)
        self.assertNotIn("positional", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["positional"], "low")
        mate_lines = [_line(["Ra8#"], 100.0, mate=1, multipv=1)]
        mate_rec = _classify(BACK_RANK, "Ra8#", mate_lines, drop=0.0)
        self.assertIn("tactics", mate_rec["theme_opportunities"])
        self.assertEqual(mate_rec["confidence"]["tactics"], "high")

    def test_no_lines_no_opportunity_except_mate_in_1(self) -> None:
        rec = _classify(QUIET_FEN, "d4", [], drop=0.0)
        self.assertEqual(rec["theme_opportunities"], [])
        self.assertEqual(rec["opportunity_evidence"], {})
        mate_rec = _classify(BACK_RANK, "Ra8#", [], drop=0.0)
        self.assertIn("tactics", mate_rec["theme_opportunities"])


class AttackGateTests(unittest.TestCase):
    def test_pv_checks_require_win_mover_at_least_50(self) -> None:
        fen = "4k3/8/8/8/8/8/8/R3K3 w - - 0 1"
        losing = [_line(["Ra8+", "Ke7", "Ra7+"], 40.0, multipv=1),
                  _line(["Rb1"], 30.0, multipv=2)]
        winning = [_line(["Ra8+", "Ke7", "Ra7+"], 60.0, multipv=1),
                   _line(["Rb1"], 50.0, multipv=2)]
        self.assertNotIn("pv_checks", _classify(fen, "Ra8+", losing)["evidence"].get("attack", []))
        self.assertIn("pv_checks", _classify(fen, "Ra8+", winning)["evidence"]["attack"])

    def test_single_pv_check_is_not_pv_checks(self) -> None:
        fen = "4k3/8/8/8/8/8/8/R3K3 w - - 0 1"
        lines = [_line(["Ra8+", "Ke7", "Ra1"], 60.0, multipv=1),
                 _line(["Rb1"], 55.0, multipv=2)]
        rec = _classify(fen, "Ra8+", lines, drop=0.0)
        self.assertNotIn("pv_checks", rec["evidence"].get("attack", []))

    def test_sac_compensation_requires_win_mover_at_least_50(self) -> None:
        losing = [_line(["Ng5", "hxg5"], 40.0, multipv=1), _line(["d4"], 35.0, multipv=2)]
        rec = _classify(SAC_FEN, "Ng5", losing, drop=0.0)
        self.assertIn("sac_unverified", rec["evidence"]["attack"])
        self.assertNotIn("sac_compensation", rec["evidence"]["attack"])
        self.assertEqual(rec["confidence"]["attack"], "low")

    def test_single_pv_check_does_not_compensate_sac(self) -> None:
        fen = "8/8/1p6/p3k3/8/8/8/R3K3 w - - 0 1"
        lines = [_line(["Rxa5+", "Ke4", "Ra1"], 55.0, multipv=1),
                 _line(["Rb1"], 54.0, multipv=2)]
        rec = _classify(fen, "Rxa5+", lines, drop=0.0)
        self.assertIn("sac_unverified", rec["evidence"]["attack"])
        self.assertNotIn("sac_compensation", rec["evidence"]["attack"])


class KingZoneAndOutpostTests(unittest.TestCase):
    def test_lone_king_zone_medium_with_clear_gap(self) -> None:
        fen = "6k1/5ppp/8/3N4/8/8/8/6K1 w - - 0 1"
        lines = [_line(["Ne7+"], 60.0, multipv=1), _line(["Kh2"], 50.0, multipv=2)]
        rec = _classify(fen, "Ne7+", lines, drop=0.0)
        self.assertEqual(rec["evidence"]["attack"], ["king_zone"])
        self.assertIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["attack"], "medium")

    def test_lone_king_zone_low_without_clear_gap(self) -> None:
        fen = "6k1/5ppp/8/3N4/8/8/8/6K1 w - - 0 1"
        lines = [_line(["Ne7+"], 55.0, multipv=1), _line(["Kh2"], 50.0, multipv=2)]
        rec = _classify(fen, "Ne7+", lines, drop=0.0)
        self.assertNotIn("attack", rec["theme_opportunities"])
        self.assertEqual(rec["confidence"]["attack"], "low")

    def test_cheap_outpost_development_is_not_high(self) -> None:
        lines = [_line(["Nc3"], 60.0, multipv=1), _line(["d3"], 50.0, multipv=2)]
        rec = _classify(QUIET_FEN, "Nc3", lines, drop=0.0)
        self.assertNotIn("outpost", rec["evidence"].get("positional", []))
        self.assertNotEqual(rec["confidence"].get("positional"), "high")

    def test_advanced_outpost_counts(self) -> None:
        fen = "4k3/8/8/8/8/5N2/8/4K3 w - - 0 1"
        lines = [_line(["Ne5"], 60.0, multipv=1), _line(["Nd2"], 50.0, multipv=2)]
        rec = _classify(fen, "Ne5", lines, drop=0.0)
        self.assertIn("outpost", rec["evidence"]["positional"])


class EvidenceSeparationTests(unittest.TestCase):
    def test_opportunity_and_used_evidence_are_separate(self) -> None:
        lines = [_line(["Nf7", "Qd4"], 65.0, multipv=1), _line(["Ng4"], 55.0, multipv=2)]
        rec = _classify(FORK_FEN, "Ng4", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertNotIn("tactics", rec["theme_used"])
        self.assertEqual(rec["opportunity_evidence"]["tactics"], ["fork"])
        self.assertNotIn("fork", rec["evidence"].get("tactics", []))
        self.assertEqual(rec["evidence"], rec["used_evidence"])
        self.assertEqual(rec["opportunity_confidence"]["tactics"], "high")


class InputHardeningTests(unittest.TestCase):
    def test_san_annotations_are_normalized(self) -> None:
        lines = [_line(["Nf7?!"], 65.0, multipv=1), _line(["Ng4"], 55.0, multipv=2)]
        rec = _classify(FORK_FEN, "Nf7!?", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertIn("tactics", rec["theme_used"])
        self.assertIn("fork", rec["evidence"]["tactics"])

    def test_lines_are_sorted_by_multipv(self) -> None:
        lines = [_line(["c3"], 48.0, multipv=2), _line(["d4"], 55.0, multipv=1)]
        rec = _classify(QUIET_FEN, "d4", lines, drop=0.0)
        self.assertIn("positional", rec["theme_opportunities"])
        self.assertIn("positional", rec["theme_used"])

    def test_black_to_move_uses_black_perspective(self) -> None:
        fen = "4k3/8/8/8/4n3/4p3/8/3Q2KR b - - 0 1"
        lines = [_line(["Nf2"], 35.0, multipv=1), _line(["Ng5"], 45.0, multipv=2)]
        rec = _classify(fen, "Nf2", lines, drop=0.0)
        self.assertIn("tactics", rec["theme_opportunities"])
        self.assertIn("tactics", rec["theme_used"])
        self.assertEqual(rec["confidence"]["tactics"], "high")

    def test_black_win_gate_uses_black_perspective(self) -> None:
        fen = "r3k3/8/8/8/8/8/8/4K3 b - - 0 1"
        losing = [_line(["Ra1+", "Ke2", "Ra2+"], 60.0, multipv=1),
                  _line(["Rb8"], 70.0, multipv=2)]
        winning = [_line(["Ra1+", "Ke2", "Ra2+"], 40.0, multipv=1),
                   _line(["Rb8"], 50.0, multipv=2)]
        self.assertNotIn("pv_checks", _classify(fen, "Ra1+", losing)["evidence"].get("attack", []))
        self.assertIn("pv_checks", _classify(fen, "Ra1+", winning)["evidence"]["attack"])

    def test_en_passant_fork_counts_as_tactics(self) -> None:
        fen = "1r1q2k1/2p1p3/8/3pP3/8/8/8/4K3 w - d6 0 1"
        lines = [_line(["exd6"], 60.0, multipv=1), _line(["e6"], 50.0, multipv=2)]
        rec = _classify(fen, "exd6", lines, drop=0.0)
        self.assertIn("fork", rec["evidence"]["tactics"])
        self.assertIn("tactics", rec["theme_opportunities"])


if __name__ == "__main__":
    unittest.main()
