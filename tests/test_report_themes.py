"""Тесты агрегата «темы ходов» в отчёте (app.report._theme_stats).

Синтетические партии/анализы; реальные партии игроков не используются.
"""

from __future__ import annotations

import unittest

from app.games import Game
from app.report import _theme_stats, build_report, format_report

_SANS = ["e4", "e5", "Nf3", "Nc6"] * 3


def _game(game_id: str) -> Game:
    return Game(
        id=game_id,
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        white={"name": "me"},
        black={"name": "opponent"},
        opening="Synthetic Opening",
        eco="A00",
        moves=list(_SANS[:6]),
        clocks=[],
        user_color="white",
        opponent="opponent",
        result_for_user="win",
    )


def _move(san: str, themes: dict | None, drop: float = 0.0,
          classification: str = "good", best: str | None = "Nf3") -> dict:
    move = {
        "san": san,
        "classification": classification,
        "drop": drop,
        "win_before": 60.0,
        "win_after": 60.0 - drop,
        "best_move_san": best,
        "best_line": [],
        "played_line": [],
    }
    if themes is not None:
        move["themes"] = themes
    return move


def _themed(opportunities, used, confidence, drop=0.0):
    return {
        "theme_opportunities": list(opportunities),
        "theme_used": list(used),
        "confidence": dict(confidence),
        "drop": drop,
        "evidence": {theme: ["synthetic"] for theme in set(opportunities) | set(used)},
        "note": None,
    }


def _analysis(moves):
    return {
        "game_id": "g1",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "opponent",
        "avg_win_loss": 5.0,
        "avg_win_before": 55.0,
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


class ThemeStatsTests(unittest.TestCase):
    def test_counts_opportunities_executed_errors(self) -> None:
        moves = []
        for ply, san in enumerate(_SANS[:6]):
            if ply % 2 == 0:  # ходы пользователя (белые)
                if ply == 0:
                    moves.append(_move(san, _themed(
                        ["tactics"], ["tactics"], {"tactics": "high"}, drop=0.0)))
                elif ply == 2:
                    moves.append(_move(san, _themed(
                        ["tactics"], [], {"tactics": "medium"}, drop=25.0),
                        drop=25.0, classification="blunder"))
                else:
                    moves.append(_move(san, _themed(
                        ["positional"], ["positional"], {"positional": "medium"})))
            else:
                moves.append(_move(san, None))
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        tactics = stats["tactics"]
        self.assertEqual(tactics["opportunities"], 2)
        self.assertEqual(tactics["executed"], 1)
        self.assertEqual(tactics["missed"], 1)
        self.assertEqual(tactics["errors"], 1)
        self.assertEqual(tactics["avg_win_loss"], 12.5)
        self.assertTrue(tactics["insufficient"])  # 2 < MIN_THEME_SAMPLE
        self.assertEqual(len(tactics["examples"]), 1)
        self.assertEqual(tactics["examples"][0]["ply"], 2)

    def test_low_confidence_goes_to_unknown_not_opportunities(self) -> None:
        moves = [
            _move(san, _themed([], [], {"attack": "low"})) if ply == 0 else _move(san, None)
            for ply, san in enumerate(_SANS[:2])
        ]
        # ply 0 — пользователь, ply 1 — соперник; добавим ещё низкоуверенный ход
        moves.append(_move("Nf3", _themed([], [], {"attack": "low"})))
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        self.assertEqual(stats["attack"]["opportunities"], 0)
        self.assertEqual(stats["attack"]["unknown"], 2)
        self.assertTrue(stats["attack"]["insufficient"])

    def test_old_cached_analysis_without_themes_is_skipped(self) -> None:
        moves = [_move(san, None) for san in _SANS[:6]]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        self.assertEqual(stats["moves_annotated"], 0)
        for theme in ("tactics", "positional", "attack"):
            self.assertEqual(stats[theme]["opportunities"], 0)
            self.assertTrue(stats[theme]["insufficient"])

    def test_opponent_moves_are_not_counted(self) -> None:
        moves = [
            _move(san, _themed(["tactics"], ["tactics"], {"tactics": "high"}))
            if ply % 2 == 1 else _move(san, None)
            for ply, san in enumerate(_SANS[:4])
        ]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        self.assertEqual(stats["tactics"]["opportunities"], 0)

    def test_build_report_exposes_themes(self) -> None:
        moves = [
            _move(san, _themed(["positional"], ["positional"], {"positional": "medium"}))
            if ply % 2 == 0 else _move(san, None)
            for ply, san in enumerate(_SANS[:6])
        ]
        report = build_report([(_game("g1"), _analysis(moves))], "me")
        self.assertIn("themes", report)
        self.assertEqual(report["themes"]["moves_annotated"], 3)
        text = format_report(report)
        self.assertIn("Темы ходов", text)
        # недостаточно данных — честно об этом сказано, процентов нет
        self.assertIn("недостаточно данных", text)


def _user_moves_with_opportunities(count: int, *, drop: float = 0.0, used: bool = True):
    """12 полуходов: чётные — пользователь (белые) с возможностью tactics."""
    moves = []
    user_seen = 0
    for ply in range(12):
        san = _SANS[ply % len(_SANS)]
        if ply % 2 == 0 and user_seen < count:
            moves.append(
                _move(
                    san,
                    _themed(
                        ["tactics"],
                        ["tactics"] if used else [],
                        {"tactics": "high" if used else "medium"},
                        drop=drop,
                    ),
                    drop=drop,
                )
            )
            user_seen += 1
        elif ply % 2 == 0:
            moves.append(_move(san, None))
        else:
            moves.append(_move(san, None))
    return moves


class SufficientSampleTests(unittest.TestCase):
    def test_insufficient_below_min_sample_and_sufficient_at_min(self) -> None:
        for count, expected_insufficient in ((4, True), (5, False), (6, False)):
            with self.subTest(count=count):
                moves = _user_moves_with_opportunities(count)
                stats = _theme_stats([(_game("g1"), _analysis(moves))])
                self.assertEqual(stats["tactics"]["opportunities"], count)
                self.assertEqual(stats["tactics"]["insufficient"], expected_insufficient)
                self.assertEqual(stats["min_sample"], 5)

    def test_sufficient_report_shows_counts_not_insufficient_text(self) -> None:
        moves = _user_moves_with_opportunities(6)
        report = build_report([(_game("g1"), _analysis(moves))], "me")
        self.assertFalse(report["themes"]["tactics"]["insufficient"])
        text = format_report(report)
        self.assertIn("возможностей 6", text)


class ExamplesCapAndSortTests(unittest.TestCase):
    def test_examples_capped_at_three_and_sorted_by_drop_desc(self) -> None:
        drops = [30.0, 10.0, 25.0, 5.0, 20.0, 15.0]
        moves = []
        user_index = 0
        for ply in range(12):
            san = _SANS[ply % len(_SANS)]
            if ply % 2 == 0 and user_index < len(drops):
                drop = drops[user_index]
                moves.append(
                    _move(
                        san,
                        _themed(["tactics"], [], {"tactics": "medium"}, drop=drop),
                        drop=drop,
                        classification="mistake",
                    )
                )
                user_index += 1
            else:
                moves.append(_move(san, None))
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        tactics = stats["tactics"]
        self.assertEqual(tactics["opportunities"], 6)
        self.assertEqual(tactics["missed"], 6)
        self.assertEqual(len(tactics["examples"]), 3)
        self.assertEqual(
            [e["drop"] for e in tactics["examples"]], [30.0, 25.0, 20.0]
        )
        self.assertEqual(
            [e["ply"] for e in tactics["examples"]], [0, 4, 8]
        )
        self.assertEqual(tactics["avg_win_loss"], 17.5)

    def test_examples_only_for_missed_moves_with_drop_at_least_five(self) -> None:
        moves = [
            _move("e4", _themed(["tactics"], [], {"tactics": "medium"}, drop=4.0), drop=4.0),
            _move("e5", None),
            _move("Nf3", _themed(["tactics"], [], {"tactics": "medium"}, drop=5.0), drop=5.0),
            _move("Nc6", None),
        ]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        self.assertEqual(len(stats["tactics"]["examples"]), 1)
        self.assertEqual(stats["tactics"]["examples"][0]["drop"], 5.0)
        # Успешный ход не попадает в примеры даже при большом drop записи хода.
        moves_ok = [
            _move("e4", _themed(["tactics"], ["tactics"], {"tactics": "high"}, drop=30.0), drop=30.0),
        ]
        stats_ok = _theme_stats([(_game("g1"), _analysis(moves_ok))])
        self.assertEqual(stats_ok["tactics"]["examples"], [])


class FallbackDropAndRobustnessTests(unittest.TestCase):
    def test_drop_fallback_to_move_drop_when_themes_drop_is_not_numeric(self) -> None:
        themed = _themed(["tactics"], [], {"tactics": "medium"}, drop=0.0)
        themed["drop"] = "bad"
        move = _move("e4", themed, drop=20.0, classification="mistake")
        stats = _theme_stats([(_game("g1"), _analysis([move]))])
        self.assertEqual(stats["tactics"]["avg_win_loss"], 20.0)
        self.assertEqual(stats["tactics"]["examples"][0]["drop"], 20.0)

    def test_avg_win_loss_is_none_when_no_drop_anywhere(self) -> None:
        themed = _themed(["tactics"], ["tactics"], {"tactics": "high"}, drop=0.0)
        themed["drop"] = None
        move = _move("e4", themed, drop=0.0)
        move["drop"] = None
        stats = _theme_stats([(_game("g1"), _analysis([move]))])
        self.assertEqual(stats["tactics"]["opportunities"], 1)
        self.assertIsNone(stats["tactics"]["avg_win_loss"])

    def test_non_dict_move_entries_are_skipped(self) -> None:
        moves = ["not-a-dict", None, 42, _move("e4", _themed(
            ["tactics"], ["tactics"], {"tactics": "high"}))]
        # ply 3 (odd) — не пользователь при белых; ставим пользовательский цвет
        # через анализ, чтобы ply 3 считался, а мусорные записи — нет.
        analysis = _analysis(moves)
        analysis["user_color"] = "black"
        stats = _theme_stats([(_game("g1"), analysis)])
        self.assertEqual(stats["moves_annotated"], 1)
        self.assertEqual(stats["tactics"]["opportunities"], 1)

    def test_non_dict_themes_value_is_skipped(self) -> None:
        for bad_themes in ("oops", 123, []):
            with self.subTest(bad_themes=bad_themes):
                move = _move("e4", None)
                move["themes"] = bad_themes
                stats = _theme_stats([(_game("g1"), _analysis([move]))])
                self.assertEqual(stats["moves_annotated"], 0)
                self.assertEqual(stats["tactics"]["opportunities"], 0)

    def test_black_user_counts_odd_plies(self) -> None:
        moves = [
            _move(san, _themed(["tactics"], ["tactics"], {"tactics": "high"}))
            if ply % 2 == 1 else _move(san, None)
            for ply, san in enumerate(_SANS[:6])
        ]
        analysis = _analysis(moves)
        analysis["user_color"] = "black"
        stats = _theme_stats([(_game("g1"), analysis)])
        self.assertEqual(stats["tactics"]["opportunities"], 3)
        self.assertEqual(stats["moves_annotated"], 3)


if __name__ == "__main__":
    unittest.main()
