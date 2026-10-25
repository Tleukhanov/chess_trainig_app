"""Тесты точности и статистики по фазам/категориям (app.accuracy)."""

from __future__ import annotations

import math
import unittest

from app.accuracy import (
    CATEGORY_KEYS,
    PHASE_KEYS,
    accuracy,
    category_stats,
    estimate_rating,
    phase_stats,
)

_SHORT_GAME = ["e4", "e5", "Nf3", "Nc6"]

# 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O Be7 6.Re1 b5 7.Bb3 d6 8.c3 O-O
_LONG_GAME = [
    "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6",
    "O-O", "Be7", "Re1", "b5", "Bb3", "d6", "c3", "O-O",
]

# 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Bxc6 dxc6 5.Nxe5 Qd4 6.Nf3 Qxe4+ 7.Qe2 Qxe2+ 8.Kxe2 Nf6 9.d4
_ENDGAME_GAME = [
    "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6",
    "Nxe5", "Qd4", "Nf3", "Qxe4+", "Qe2", "Qxe2+", "Kxe2", "Nf6", "d4",
]


def _entry(
    san: str,
    classification: str = "good",
    drop: float | None = 0.0,
    win_before: float | None = 60.0,
    win_after: float | None = None,
) -> dict:
    """Запись хода в формате ``GameAnalysis.summary()["moves"]``."""
    if win_after is None and drop is not None:
        win_after = win_before - drop if win_before is not None else None
    return {
        "san": san,
        "classification": classification,
        "drop": drop,
        "win_before": win_before,
        "win_after": win_after,
    }


def _analysis(moves: list[str], entries: dict[int, dict]) -> dict:
    """Анализ, где записи заданы по номеру полухода, остальные — «good»."""
    records = []
    for ply, san in enumerate(moves):
        records.append(entries.get(ply, _entry(san)))
    return {"moves": records}


class AccuracyFormulaTests(unittest.TestCase):
    def test_lichess_formula(self) -> None:
        # 103.1668 * exp(-0.04354 * x) - 3.1669
        self.assertEqual(accuracy(0), 100.0)  # 99.9999 после округления
        self.assertEqual(accuracy(6), 76.28)
        self.assertEqual(accuracy(10), 63.58)
        expected = 103.1668 * math.exp(-0.04354 * 25.0) - 3.1669
        self.assertAlmostEqual(accuracy(25), expected, delta=0.01)

    def test_accuracy_clamps_and_handles_missing(self) -> None:
        self.assertEqual(accuracy(-10), 100.0)
        self.assertEqual(accuracy(500), 0.0)
        self.assertEqual(accuracy(None), 0.0)
        self.assertEqual(accuracy(float("nan")), 0.0)
        self.assertEqual(accuracy(float("inf")), 0.0)


class PhaseStatsTests(unittest.TestCase):
    def test_short_game_counts_only_white_moves(self) -> None:
        analysis = _analysis(
            _SHORT_GAME,
            {2: _entry("Nf3", "mistake", drop=12.0)},
        )
        stats = phase_stats(_SHORT_GAME, analysis, "white")

        self.assertEqual(tuple(stats), PHASE_KEYS)
        opening = stats["opening"]
        self.assertEqual(opening["moves"], 2)  # только e4 и Nf3
        self.assertEqual(opening["errors"], 1)
        self.assertEqual(opening["avg_drop"], 6.0)  # (0 + 12) / 2
        self.assertEqual(opening["accuracy"], 76.28)
        self.assertEqual(stats["middlegame"]["moves"], 0)
        self.assertEqual(stats["endgame"]["moves"], 0)

    def test_black_moves_counted_on_odd_plies(self) -> None:
        analysis = _analysis(
            _SHORT_GAME,
            {3: _entry("Nc6", "blunder", drop=20.0)},
        )
        stats = phase_stats(_SHORT_GAME, analysis, "black")

        opening = stats["opening"]
        self.assertEqual(opening["moves"], 2)  # e5 и Nc6
        self.assertEqual(opening["errors"], 1)
        self.assertEqual(opening["avg_drop"], 10.0)  # (0 + 20) / 2
        self.assertEqual(opening["accuracy"], 63.58)
        self.assertEqual(stats["middlegame"]["moves"], 0)

    def test_drop_falls_back_to_win_before_after(self) -> None:
        entry = _entry("Nf3", "blunder", drop=None, win_before=80.0, win_after=68.0)
        analysis = _analysis(_SHORT_GAME, {2: entry})
        stats = phase_stats(_SHORT_GAME, analysis, "white")

        self.assertEqual(stats["opening"]["moves"], 2)
        self.assertEqual(stats["opening"]["errors"], 1)
        self.assertEqual(stats["opening"]["avg_drop"], 6.0)  # (0 + 12) / 2

    def test_empty_phase_is_none(self) -> None:
        # Фазы без ходов — None, а не 0%: 0 выглядел бы как «сыграл ужасно».
        stats = phase_stats(_SHORT_GAME, _analysis(_SHORT_GAME, {}), "white")
        for key in ("middlegame", "endgame"):
            self.assertEqual(
                stats[key],
                {"moves": 0, "errors": 0, "avg_drop": None, "accuracy": None},
            )

        empty = phase_stats([], {"moves": []}, "white")
        for key in ("opening", "middlegame", "endgame"):
            self.assertEqual(
                empty[key],
                {"moves": 0, "errors": 0, "avg_drop": None, "accuracy": None},
            )

    def test_middlegame_and_endgame_detected(self) -> None:
        long_analysis = _analysis(
            _LONG_GAME,
            {12: _entry("Bb3", "inaccuracy", drop=4.0), 14: _entry("c3", "mistake", drop=10.0)},
        )
        stats = phase_stats(_LONG_GAME, long_analysis, "white")

        self.assertEqual(stats["opening"]["moves"], 6)  # e4, Nf3, Bb5, Ba4, O-O, Re1
        middlegame = stats["middlegame"]
        self.assertEqual(middlegame["moves"], 2)  # Bb3 и c3
        self.assertEqual(middlegame["errors"], 1)
        self.assertEqual(middlegame["avg_drop"], 7.0)
        self.assertEqual(middlegame["accuracy"], 72.9)

        end_analysis = _analysis(_ENDGAME_GAME, {16: _entry("d4")})
        end_stats = phase_stats(_ENDGAME_GAME, end_analysis, "white")
        endgame = end_stats["endgame"]
        self.assertEqual(endgame["moves"], 1)  # d4 уже в эндшпиле
        self.assertEqual(endgame["errors"], 0)
        self.assertEqual(endgame["avg_drop"], 0.0)
        self.assertEqual(endgame["accuracy"], 100.0)


_PHASE_FIXTURE = {
    "opening": {"moves": 4, "errors": 1, "avg_drop": 10.0, "accuracy": 63.58},
    "middlegame": {"moves": 6, "errors": 2, "avg_drop": 20.0, "accuracy": 40.02},
    "endgame": {"moves": 2, "errors": 0, "avg_drop": 2.0, "accuracy": 91.4},
}


class CategoryStatsTests(unittest.TestCase):
    def test_phase_values_without_motifs(self) -> None:
        stats = category_stats(_PHASE_FIXTURE)

        self.assertEqual(tuple(stats), CATEGORY_KEYS)
        self.assertEqual(stats["opening"], 63.58)
        self.assertEqual(stats["middlegame"], 40.02)
        self.assertEqual(stats["endgame"], 91.4)
        self.assertIsNone(stats["tactics"])  # без мотивов тактики нет
        self.assertEqual(stats["strategy"], 40.02)
        self.assertEqual(stats["positional"], 51.8)  # (63.58 + 40.02) / 2
        self.assertTrue(all(value is None or 0.0 <= value <= 100.0 for value in stats.values()))

    def test_tactics_share_reduces_strategy(self) -> None:
        motifs = {
            "вилка": {"count": 3, "avg_drop": 20.0},
            "связка": 1,
            "прочее": {"count": 4, "avg_drop": 5.0},
        }
        stats = category_stats(_PHASE_FIXTURE, motifs)

        self.assertEqual(stats["tactics"], 50.0)  # 4 тактических из 8
        self.assertEqual(stats["strategy"], 20.01)  # 40.02 * (1 - 0.5)
        self.assertEqual(stats["positional"], 51.8)

    def test_unknown_phase_and_empty_input(self) -> None:
        stats = category_stats({})
        self.assertEqual(set(stats), set(CATEGORY_KEYS))
        self.assertTrue(all(value is None for value in stats.values()))

        only_mid = category_stats(
            {"middlegame": {"moves": 5, "errors": 1, "avg_drop": 20.0, "accuracy": 40.02}}
        )
        self.assertEqual(only_mid["positional"], 40.02)  # среднее только по фазам с ходами


class EstimateRatingTests(unittest.TestCase):
    def test_shift_from_user_rating(self) -> None:
        self.assertEqual(estimate_rating(70.0, 1500), 1500)
        self.assertEqual(estimate_rating(80.0, 1500), 1650)
        self.assertEqual(estimate_rating(60.0, 1500), 1350)

    def test_clamp_and_missing_rating(self) -> None:
        self.assertEqual(estimate_rating(0.0, 900), 800)
        self.assertEqual(estimate_rating(100.0, 2900), 3000)
        self.assertIsNone(estimate_rating(85.0, None))


if __name__ == "__main__":
    unittest.main()
