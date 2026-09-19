"""Тесты фаз партии и мотивов (узоров) ошибок (app.patterns) и отчёта coach."""

from __future__ import annotations

import unittest

import chess

from app.analyzer import Eval
from app.games import Game
from app.patterns import (
    MOTIF_FORK,
    MOTIF_HANGING,
    MOTIF_MATE,
    MOTIF_PIN,
    PHASE_LABELS,
    analyze_user_moves,
    material_en_prise,
    motif_stats,
    motifs_for,
    phase,
    phase_stats,
    piece_value_on,
    weakness_stats,
)
from app.report import build_report, format_report

_SICILIAN = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6"]


def _synthetic_game() -> Game:
    return Game(
        id="syn",
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        white={"name": "me"},
        black={"name": "nick"},
        opening="Sicilian Defense",
        eco="B20",
        moves=list(_SICILIAN),
        clocks=[],
        user_color="white",
        opponent="nick",
        result_for_user="win",
    )


def _entry(san: str, cls: str, drop: float = 0.0, best: str | None = None) -> dict:
    return {
        "san": san,
        "classification": cls,
        "drop": drop,
        "win_before": 60.0,
        "win_after": 60.0 - drop,
        "best_move_san": best,
        "best_eval": None,
    }


def _synthetic_analysis() -> dict:
    moves = []
    for ply, san in enumerate(_SICILIAN):
        if ply % 2 == 0:
            if ply == 2:
                moves.append(_entry(san, "mistake", 12.0, best="Nc3"))
            elif ply == 6:
                moves.append(_entry(san, "blunder", 25.0, best="Nxd4"))
            else:
                moves.append(_entry(san, "good"))
        else:
            moves.append(_entry(san, "good"))
    return {
        "game_id": "syn",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "nick",
        "acpl": 15.0,
        "accuracy": 82.0,
        "blunders": [6],
        "mistakes": [2],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def _clean_analysis() -> dict:
    moves = [_entry(san, "good") for san in _SICILIAN]
    return {
        "game_id": "syn",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "nick",
        "acpl": 2.0,
        "accuracy": 98.0,
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


class PhaseTests(unittest.TestCase):
    def test_early_opening(self) -> None:
        self.assertEqual(phase(6, chess.Board()), "opening")

    def test_bare_kings_endgame(self) -> None:
        board = chess.Board("8/8/8/8/8/8/8/k1K5 w - - 0 1")
        self.assertEqual(phase(60, board), "endgame")

    def test_queen_few_pieces_endgame(self) -> None:
        board = chess.Board("6k1/8/8/8/7Q/8/1K6/8 w - - 0 1")
        self.assertEqual(phase(60, board), "endgame")

    def test_no_queens_few_pieces_endgame(self) -> None:
        board = chess.Board("6k1/8/8/8/R7/R7/8/6K1 w - - 0 1")
        self.assertEqual(phase(40, board), "endgame")

    def test_queens_many_pieces_midgame(self) -> None:
        board = chess.Board("r3k2r/pp3ppp/2n5/2q2p2/2B1P3/5N2/PPPP1PPP/R1BQK2R w KQkq - 0 1")
        self.assertEqual(phase(30, board), "midgame")


class PieceValueTests(unittest.TestCase):
    def test_values(self) -> None:
        board = chess.Board()
        self.assertEqual(piece_value_on(board, chess.E2), 1)
        self.assertEqual(piece_value_on(board, chess.G1), 3)
        self.assertEqual(piece_value_on(board, chess.D1), 9)
        self.assertEqual(piece_value_on(board, chess.E5), 0)


class MaterialTests(unittest.TestCase):
    def test_undefended_rook_attacked_true(self) -> None:
        board = chess.Board("7k/8/8/8/1p6/R7/8/6K1 b - - 0 1")
        self.assertTrue(material_en_prise(board))

    def test_defended_rook_false(self) -> None:
        board = chess.Board("7k/8/8/8/1p6/R7/1P6/6K1 b - - 0 1")
        self.assertFalse(material_en_prise(board))


class MotifsTests(unittest.TestCase):
    def test_mate_via_best_eval(self) -> None:
        motifs = motifs_for(chess.Board(), best_eval=Eval(cp=None, mate=1))
        self.assertIn(MOTIF_MATE, motifs)

    def test_mate_after_best_move(self) -> None:
        board = chess.Board("3r2k1/5ppp/8/8/8/8/8/3R2K1 w - - 0 1")
        best = board.parse_san("Rxd8")
        motifs = motifs_for(board, best=best)
        self.assertIn(MOTIF_MATE, motifs)

    def test_fork(self) -> None:
        board = chess.Board("6k1/1r6/4p3/8/8/3N4/1P6/K7 w - - 0 1")
        best = board.parse_san("Nc5")
        motifs = motifs_for(board, best=best)
        self.assertIn(MOTIF_FORK, motifs)

    def test_pin(self) -> None:
        board = chess.Board("6k1/8/4q3/8/4B3/8/8/K7 w - - 0 1")
        best = board.parse_san("Bd5")
        motifs = motifs_for(board, best=best)
        self.assertIn(MOTIF_PIN, motifs)

    def test_hanging_after_played(self) -> None:
        board = chess.Board("7k/8/8/8/1p6/8/8/R5K1 w - - 0 1")
        played = board.parse_san("Ra3")
        motifs = motifs_for(board, played=played)
        self.assertIn(MOTIF_HANGING, motifs)

    def test_quiet_move_no_motifs(self) -> None:
        board = chess.Board()
        best = board.parse_san("Nf3")
        played = board.parse_san("e4")
        self.assertEqual(motifs_for(board, best=best, played=played), [])


class AnalyzeUserMovesTests(unittest.TestCase):
    def test_selects_only_bad_user_moves(self) -> None:
        records = analyze_user_moves(_synthetic_game(), _synthetic_analysis())
        self.assertEqual([r["ply"] for r in records], [2, 6])
        self.assertEqual([r["classification"] for r in records], ["mistake", "blunder"])
        self.assertEqual([r["drop"] for r in records], [12.0, 25.0])
        self.assertTrue(all(r["phase"] in PHASE_LABELS for r in records))
        self.assertEqual(records[0]["phase"], "opening")
        self.assertTrue(all(r["motifs"] == [] for r in records))

    def test_empty_when_no_bad_moves(self) -> None:
        self.assertEqual(analyze_user_moves(_synthetic_game(), _clean_analysis()), [])


class StatsTests(unittest.TestCase):
    def test_phase_stats(self) -> None:
        records = analyze_user_moves(_synthetic_game(), _synthetic_analysis())
        self.assertEqual(
            phase_stats(records),
            {"opening": {"count": 2, "blunders": 1, "mistakes": 1, "avg_drop": 18.5}},
        )

    def test_motif_stats_prochee(self) -> None:
        records = analyze_user_moves(_synthetic_game(), _synthetic_analysis())
        self.assertEqual(
            motif_stats(records),
            {"прочее": {"count": 2, "avg_drop": 18.5}},
        )

    def test_weakness_stats(self) -> None:
        weak = weakness_stats([(_synthetic_game(), _synthetic_analysis())])
        self.assertEqual(weak["total_bad"], 2)
        self.assertEqual(
            weak["phases"],
            {"opening": {"count": 2, "blunders": 1, "mistakes": 1, "avg_drop": 18.5}},
        )
        self.assertEqual(weak["motifs"], {"прочее": {"count": 2, "avg_drop": 18.5}})

    def test_empty_pairs(self) -> None:
        weak = weakness_stats([])
        self.assertEqual(weak["total_bad"], 0)
        self.assertEqual(weak["phases"], {})
        self.assertEqual(weak["motifs"], {})


class ReportTests(unittest.TestCase):
    def test_weakness_sections_in_report(self) -> None:
        report = build_report([(_synthetic_game(), _synthetic_analysis())], "Tleukhanov")
        self.assertIn("phases", report)
        self.assertIn("motifs", report)
        self.assertEqual(report["total_bad"], 2)

        text = format_report(report)
        self.assertIn("Слабости по фазам", text)
        self.assertIn("Узоры ошибок", text)
        self.assertIn("Дебют", text)

    def test_no_bad_moves_hides_weakness_sections(self) -> None:
        report = build_report([(_synthetic_game(), _clean_analysis())], "Tleukhanov")
        text = format_report(report)
        self.assertIn("проблемных ходов нет", text)
        self.assertNotIn("Узоры ошибок", text)


if __name__ == "__main__":
    unittest.main()