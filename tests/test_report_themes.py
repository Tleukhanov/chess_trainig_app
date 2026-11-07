"""Тесты агрегата «темы ходов» в отчёте (app.report._theme_stats).

Синтетические партии/анализы; реальные партии игроков не используются.
"""

from __future__ import annotations

import unittest

import math

from app.games import Game
from app.report import _clean_drop, _theme_stats, build_report, format_report

_SANS = ["e4", "e5", "Nf3", "Nc6"] * 3


def _game(game_id: str, user_color: str = "white") -> Game:
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
        user_color=user_color,
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


def _analysis(moves, user_color: str = "white"):
    return {
        "game_id": "g1",
        "user_color": user_color,
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

    def test_user_color_taken_from_game_not_analysis(self) -> None:
        # Game.user_color=black, а analysis врёт (white): ходы пользователя —
        # нечётные полуходы (1, 3), и только они должны считаться.
        moves = [
            _move("e4", None),
            _move("e5", _themed(["tactics"], ["tactics"], {"tactics": "high"})),
            _move("Nf3", None),
            _move("Nc6", _themed(["tactics"], [], {"tactics": "medium"}, drop=25.0),
                    drop=25.0, classification="blunder"),
        ]
        stats = _theme_stats([(_game("g1", user_color="black"),
                               _analysis(moves, user_color="white"))])
        self.assertEqual(stats["tactics"]["opportunities"], 2)
        self.assertEqual(stats["tactics"]["executed"], 1)
        self.assertEqual(stats["tactics"]["missed"], 1)

    def test_malformed_theme_fields_do_not_crash(self) -> None:
        bad_themes = {
            "theme_opportunities": "tactics",  # строка, а не список
            "theme_used": 5,  # число, а не список
            "confidence": ["low"],  # список, а не словарь
            "drop": float("nan"),
            "evidence": {},
            "note": None,
        }
        moves = [_move("e4", bad_themes, drop=float("inf"))]
        moves += [_move(san, None) for san in _SANS[1:2]]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        self.assertEqual(stats["moves_annotated"], 1)
        self.assertEqual(stats["tactics"]["opportunities"], 0)
        self.assertEqual(stats["tactics"]["unknown"], 0)
        # build_report не падает на таком мусоре.
        report = build_report([(_game("g1"), _analysis(moves))], "me")
        self.assertIn("themes", report)
        self.assertIsNone(report["themes"]["tactics"]["avg_win_loss"])

    def test_clean_drop_rejects_bool_nan_inf(self) -> None:
        self.assertIsNone(_clean_drop(True))
        self.assertIsNone(_clean_drop(False))
        self.assertIsNone(_clean_drop(float("nan")))
        self.assertIsNone(_clean_drop(float("inf")))
        self.assertIsNone(_clean_drop(float("-inf")))
        self.assertIsNone(_clean_drop("5"))
        self.assertIsNone(_clean_drop(None))
        self.assertEqual(_clean_drop(5), 5.0)
        self.assertEqual(_clean_drop(2.5), 2.5)

    def test_bad_drops_never_produce_nan_avg_or_examples(self) -> None:
        moves = [
            _move("e4", _themed(["tactics"], [], {"tactics": "high"},
                                drop=float("nan")), drop=float("inf")),
            _move("e5", None),
            _move("Nf3", _themed(["tactics"], [], {"tactics": "high"},
                                 drop=True), drop=True),
        ]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        avg = stats["tactics"]["avg_win_loss"]
        self.assertIsNone(avg)
        self.assertEqual(stats["tactics"]["examples"], [])
        self.assertFalse(isinstance(avg, bool))
        if avg is not None:
            self.assertTrue(math.isfinite(avg))

    def test_dedupe_games_by_id_and_examples(self) -> None:
        moves = [
            _move("e4", _themed(["tactics"], [], {"tactics": "medium"}, drop=25.0),
                  drop=25.0, classification="blunder"),
            _move("e5", None),
        ]
        pair = (_game("g1"), _analysis(moves))
        single = _theme_stats([pair])
        doubled = _theme_stats([pair, pair])
        self.assertEqual(doubled["tactics"]["opportunities"],
                         single["tactics"]["opportunities"])
        self.assertEqual(doubled["tactics"]["missed"], single["tactics"]["missed"])
        # Пример (g1, ply=0) не дублируется.
        self.assertEqual(len(doubled["tactics"]["examples"]), 1)
        self.assertEqual(doubled["tactics"]["examples"][0]["ply"], 0)

    def test_errors_exclude_inaccuracy(self) -> None:
        moves = [
            _move("e4", _themed(["tactics"], [], {"tactics": "medium"}, drop=8.0),
                  drop=8.0, classification="inaccuracy"),
            _move("e5", None),
            _move("Nf3", _themed(["tactics"], [], {"tactics": "medium"}, drop=15.0),
                  drop=15.0, classification="mistake"),
        ]
        stats = _theme_stats([(_game("g1"), _analysis(moves))])
        # inaccuracy не считается ошибкой, mistake — считается.
        self.assertEqual(stats["tactics"]["errors"], 1)

    def test_needs_refresh_old_partial_and_fresh(self) -> None:
        old_moves = [_move(san, None) for san in _SANS[:4]]
        old_stats = _theme_stats([(_game("g1"), _analysis(old_moves))])
        self.assertTrue(old_stats["needs_refresh"])
        self.assertEqual(old_stats["games_without_themes"], 1)
        self.assertEqual(old_stats["moves_annotated"], 0)

        fresh_moves = [
            _move("e4", _themed(["tactics"], ["tactics"], {"tactics": "high"})),
            _move("e5", None),
        ]
        fresh_stats = _theme_stats([(_game("g1"), _analysis(fresh_moves))])
        self.assertFalse(fresh_stats["needs_refresh"])
        self.assertEqual(fresh_stats["games_without_themes"], 0)

        partial_stats = _theme_stats([
            (_game("g1"), _analysis(fresh_moves)),
            (_game("g2"), _analysis(old_moves)),
        ])
        self.assertTrue(partial_stats["needs_refresh"])
        self.assertEqual(partial_stats["games_without_themes"], 1)
        self.assertGreater(partial_stats["moves_annotated"], 0)

        # Сигнал виден и на верхнем уровне /api/report.
        report = build_report([(_game("g1"), _analysis(old_moves))], "me")
        self.assertTrue(report["needs_refresh"])
        self.assertTrue(report["themes"]["needs_refresh"])
        fresh_report = build_report([(_game("g1"), _analysis(fresh_moves))], "me")
        self.assertFalse(fresh_report["needs_refresh"])

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


if __name__ == "__main__":
    unittest.main()
