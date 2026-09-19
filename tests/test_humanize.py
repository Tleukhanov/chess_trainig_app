"""Тесты оценки «человечности» ошибок (app.humanize).

Прогоны идут на синтетических партиях с FakePolicy — без Stockfish.
Проверяется вердикт по beta, сравнение SAN с суффиксами #/+, сборка
report и устойчивость к пустым входным данным.
"""

from __future__ import annotations

import math
import unittest

from app.games import Game
from app.humanize import (
    HumanMoveInfo,
    _san_key,
    grade_user_bad_moves,
    humanize_report,
    verdict_from_beta,
)

_SICILIAN = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6"]
_SCHOLAR = ["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"]

_EN = 1e-9


class FakePolicy:
    """Политика с фиксированным распределением (одинаковым для любой позиции)."""

    def __init__(self, probs: list[tuple[str, float]]) -> None:
        self._probs = list(probs)

    def probs(self, board=None, *, k: int = 8) -> list[tuple[str, float]]:
        return list(self._probs)


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


def _game(moves: list[str], game_id: str = "d1") -> Game:
    return Game(
        id=game_id,
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        white={"name": "me"},
        black={"name": "nick"},
        opening="Sicilian Defense",
        eco="B20",
        moves=list(moves),
        clocks=[],
        user_color="white",
        opponent="nick",
        result_for_user="win",
    )


def _sicilian_analysis() -> dict:
    moves = []
    for ply, san in enumerate(_SICILIAN):
        if ply == 2:
            moves.append(_entry(san, "mistake", 12.0, best="Nc3"))
        elif ply == 6:
            moves.append(_entry(san, "blunder", 25.0, best="Nxd4"))
        else:
            moves.append(_entry(san, "good"))
    return {
        "game_id": "d1",
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


def _scholar_analysis() -> dict:
    moves = []
    for ply, san in enumerate(_SCHOLAR):
        if ply == 6:
            moves.append(_entry(san, "blunder", 18.0, best="Qxf7"))
        else:
            moves.append(_entry(san, "good"))
    return {
        "game_id": "s1",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "nick",
        "acpl": 18.0,
        "accuracy": 80.0,
        "blunders": [6],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


class VerdictFromBetaTests(unittest.TestCase):
    def test_none_is_no_data(self) -> None:
        self.assertEqual(verdict_from_beta(None), "no-data")

    def test_natural_threshold(self) -> None:
        self.assertEqual(verdict_from_beta(0.0), "natural")
        self.assertEqual(verdict_from_beta(-1.0), "natural")

    def test_borderline_band(self) -> None:
        self.assertEqual(verdict_from_beta(-1.5), "borderline")

    def test_unnatural_threshold(self) -> None:
        self.assertEqual(verdict_from_beta(-2.5), "unnatural")
        self.assertEqual(verdict_from_beta(-2.5001), "unnatural")

    def test_far_unnatural(self) -> None:
        self.assertEqual(verdict_from_beta(-100.0), "unnatural")


class SanKeyTests(unittest.TestCase):
    def test_strips_check_and_mate_suffixes(self) -> None:
        self.assertEqual(_san_key("Qxf7#"), _san_key("Qxf7"))
        self.assertEqual(_san_key("Nxd4+"), "Nxd4")
        self.assertEqual(_san_key("e4"), "e4")


class GradeUserBadMovesTests(unittest.TestCase):
    def test_returns_only_user_bad_moves(self) -> None:
        game = _game(_SICILIAN)
        analysis = _sicilian_analysis()
        policy = FakePolicy([("Nf3", 0.5), ("d3", 0.3), ("Nc3", 0.2)])

        infos = grade_user_bad_moves(game, analysis, policy)

        self.assertEqual([i.ply for i in infos], [2, 6])
        self.assertEqual([i.san for i in infos], ["Nf3", "Nxd4"])
        self.assertEqual([i.classification for i in infos], ["mistake", "blunder"])
        self.assertEqual([i.drop for i in infos], [12.0, 25.0])
        self.assertTrue(all(isinstance(i, HumanMoveInfo) for i in infos))

    def test_beta_formula_for_found_and_missing_moves(self) -> None:
        game = _game(_SICILIAN)
        analysis = _sicilian_analysis()
        policy = FakePolicy([("d3", 0.6), ("Nf3", 0.35), ("Nc3", 0.05)])

        infos = grade_user_bad_moves(game, analysis, policy)

        in_top = infos[0]
        self.assertEqual(in_top.san, "Nf3")
        self.assertEqual(in_top.prob_user, 0.35)
        self.assertEqual(in_top.prob_best, 0.6)
        expected = math.log((0.35 + _EN) / (0.6 + _EN))
        self.assertAlmostEqual(in_top.beta, expected, places=10)
        self.assertEqual(in_top.verdict, "natural")

        missing = infos[1]
        self.assertEqual(missing.san, "Nxd4")
        self.assertEqual(missing.prob_user, 0.0)
        self.assertEqual(missing.prob_best, 0.6)
        self.assertLess(missing.beta, 0.0)
        self.assertEqual(missing.verdict, "unnatural")

    def test_no_data_when_policy_fails(self) -> None:
        game = _game(_SICILIAN)
        analysis = _sicilian_analysis()

        def boom(board=None, *, k: int = 8) -> list[tuple[str, float]]:
            raise RuntimeError("движок упал")

        infos = grade_user_bad_moves(game, analysis, boom)
        self.assertEqual(len(infos), 2)
        self.assertTrue(all(i.verdict == "no-data" for i in infos))
        self.assertTrue(all(i.beta is None for i in infos))

    def test_mate_suffix_comparison(self) -> None:
        game = _game(_SCHOLAR, game_id="s1")
        analysis = _scholar_analysis()
        policy = FakePolicy([("Qxf7", 0.8), ("e5", 0.15), ("d6", 0.05)])

        infos = grade_user_bad_moves(game, analysis, policy)

        self.assertEqual([i.ply for i in infos], [6])
        info = infos[0]
        self.assertEqual(info.san, "Qxf7#")
        self.assertEqual(info.prob_user, 0.8)
        self.assertEqual(info.prob_best, 0.8)
        self.assertEqual(info.beta, 0.0)
        self.assertEqual(info.verdict, "natural")


class HumanizeReportTests(unittest.TestCase):
    def test_report_aggregates_two_games(self) -> None:
        pairs = [
            (_game(_SCHOLAR, game_id="s1"), _scholar_analysis()),
            (_game(_SICILIAN, game_id="d1"), _sicilian_analysis()),
        ]
        policy = FakePolicy([("Nf3", 0.6), ("d3", 0.3), ("Nc3", 0.1)])

        report = humanize_report(pairs, policy)

        self.assertEqual(report["total_bad"], 3)
        self.assertEqual(report["verdicts"]["natural"], 1)
        self.assertEqual(report["verdicts"]["unnatural"], 2)
        self.assertEqual(report["verdicts"]["borderline"], 0)
        self.assertEqual(report["verdicts"]["no-data"], 0)
        self.assertEqual(report["borderline"], 0)

        bad_beta = math.log((0.0 + _EN) / (0.6 + _EN))
        self.assertEqual(report["avg_beta"], round((bad_beta * 2) / 3, 2))

        self.assertEqual(len(report["unnatural"]), 2)
        self.assertEqual(report["unnatural"][0]["san"], "Qxf7#")
        self.assertEqual(report["unnatural"][0]["beta"], round(bad_beta, 2))
        self.assertEqual(report["unnatural"][1]["san"], "Nxd4")

        self.assertEqual(len(report["natural"]), 1)
        self.assertEqual(report["natural"][0]["san"], "Nf3")
        self.assertEqual(report["natural"][0]["beta"], 0.0)

        self.assertEqual(len(report["items"]), 3)
        self.assertEqual(report["items"][0]["prob_user"], 0.0)
        self.assertEqual(report["items"][1]["prob_user"], 0.6)
        self.assertEqual(report["items"][1]["prob_best"], 0.6)
        item = report["items"][2]
        self.assertEqual(item["prob_best"], 0.6)
        self.assertEqual(item["prob_user"], 0.0)
        self.assertEqual(item["beta"], round(bad_beta, 2))

    def test_empty_pairs(self) -> None:
        report = humanize_report([], FakePolicy([]))
        self.assertEqual(report["total_bad"], 0)
        self.assertEqual(report["items"], [])
        self.assertEqual(report["verdicts"]["no-data"], 0)
        self.assertEqual(report["verdicts"]["natural"], 0)
        self.assertEqual(report["unnatural"], [])
        self.assertEqual(report["natural"], [])
        self.assertIsNone(report["avg_beta"])


if __name__ == "__main__":
    unittest.main()