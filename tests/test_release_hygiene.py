"""Тесты гигиенических правок релиза 0.9.22 (audit M9 minor fixes).

Синтетические партии/анализы; реальные партии игроков и сетевые вызовы
не используются.
"""

from __future__ import annotations

import unittest

from app.games import Game, _determine_user_color
from app.opening_book import build_opening_book
from app.plan import build_plan
from app.repertoire import opening_stats
from app.report import _move_label, build_report, format_report

_SANS = ["e4", "e5", "Nf3", "Nc6"] * 3


def _game(game_id: str, color: str = "white", opening: str | None = "Synthetic Opening",
          eco: str = "A00") -> Game:
    return Game(
        id=game_id,
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white" if color == "white" else "black",
        white={"name": "me" if color == "white" else "opp"},
        black={"name": "opp" if color == "white" else "me"},
        opening=opening,
        eco=eco,
        moves=list(_SANS[:6]),
        clocks=[],
        user_color=color,
        opponent="opp",
        result_for_user="win",
    )


def _move(san: str, drop: float = 0.0, classification: str = "good",
          time_pressure: bool | None = None) -> dict:
    return {
        "san": san,
        "classification": classification,
        "drop": drop,
        "win_before": 60.0,
        "win_after": 60.0 - drop,
        "best_move_san": "Nf3",
        "best_line": [],
        "played_line": [],
        "time_pressure": time_pressure,
    }


def _analysis(moves, *, with_avg: bool = True) -> dict:
    analysis = {
        "game_id": "g1",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "opp",
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }
    if with_avg:
        analysis["avg_win_loss"] = 5.0
        analysis["avg_win_before"] = 55.0
    return analysis


class MoveLabelTests(unittest.TestCase):
    def test_white_move_number_and_marker(self) -> None:
        self.assertEqual(_move_label(0, "e4"), "1. e4")
        self.assertEqual(_move_label(18, "Qd2"), "10. Qd2")

    def test_black_move_number_and_marker(self) -> None:
        self.assertEqual(_move_label(1, "e5"), "1... e5")
        self.assertEqual(_move_label(19, "bxa3"), "10... bxa3")

    def test_missing_san_keeps_label(self) -> None:
        self.assertEqual(_move_label(20), "11.")


class ReportIntegrityTests(unittest.TestCase):
    def test_missing_avg_metrics_are_none_not_zero(self) -> None:
        moves = [_move(s) for s in _SANS[:6]]
        report = build_report([(_game("g1"), _analysis(moves, with_avg=False))], "me")
        self.assertIsNone(report["avg"]["avg_win_loss"])
        self.assertIsNone(report["avg"]["avg_win_before"])
        self.assertIsNone(report["openings"][0]["avg_win_loss"])

    def test_worst_games_missing_metric_sorts_last(self) -> None:
        moves = [_move(s) for s in _SANS[:6]]
        good = _analysis(moves)  # avg_win_loss = 5.0
        good["avg_win_loss"] = 9.0
        missing = _analysis(moves, with_avg=False)
        report = build_report([(_game("a"), missing), (_game("b"), good)], "me")
        worst = report["worst_games"]
        self.assertEqual(worst[0]["game_id"], "b")
        self.assertIsNone(worst[-1]["avg_win_loss"])

    def test_missed_wins_carry_move_label_and_ply(self) -> None:
        moves = [_move(s, drop=1.0, classification="good") for s in _SANS[:6]]
        analysis = _analysis(moves)
        analysis["missed_wins"] = [3]
        moves[3]["win_before"] = 96.0
        moves[3]["win_after"] = 88.0
        moves[3]["san"] = "Nc6"
        report = build_report([(_game("g1"), analysis)], "me")
        entry = report["missed_wins"][0]
        self.assertEqual(entry["ply"], 3)
        self.assertEqual(entry["move"], "2... Nc6")
        text = format_report(report)
        self.assertIn("2... Nc6", text)

    def test_time_pressure_without_moves_is_no_data(self) -> None:
        moves = [_move(s) for s in _SANS[:6]]
        report = build_report([(_game("g1"), _analysis(moves))], "me")
        self.assertEqual(report["time_pressure"]["moves_count"], 0)
        self.assertIsNone(report["time_pressure"]["tp_blunder_rate_pct"])
        text = format_report(report)
        self.assertIn("Цейтнот: нет данных", text)
        self.assertNotIn("(0%)", text)

    def test_time_pressure_with_moves_shows_rate(self) -> None:
        moves = [_move(s, time_pressure=True) for s in _SANS[:6]]
        moves[2]["classification"] = "blunder"  # ply 2 -> белый ход пользователя
        moves[2]["drop"] = 20.0
        analysis = _analysis(moves)
        analysis["time_pressure_blunders"] = [2]
        report = build_report([(_game("g1"), analysis)], "me")
        self.assertEqual(report["time_pressure"]["moves_count"], 3)
        self.assertAlmostEqual(report["time_pressure"]["tp_blunder_rate_pct"], 100.0 / 3, places=1)
        text = format_report(report)
        self.assertIn("Цейтнот: 3 хода под давлением", text)

    def test_phase_table_header_is_bad_moves(self) -> None:
        moves = [_move(s, drop=25.0, classification="blunder") for s in _SANS[:6]]
        report = build_report([(_game("g1"), _analysis(moves))], "me")
        text = format_report(report)
        self.assertIn("плохих ходов", text)

    def test_format_renders_dash_for_missing_metrics(self) -> None:
        moves = [_move(s) for s in _SANS[:6]]
        report = build_report([(_game("g1"), _analysis(moves, with_avg=False))], "me")
        text = format_report(report)
        self.assertIn("средняя потеря win% = —", text)


class PlanBranchesTests(unittest.TestCase):
    def test_branches_expose_path_ok_rate_and_compat_ok_rate(self) -> None:
        moves = [_move(s) for s in _SANS[:4]]
        pairs = [(_game("g1"), _analysis(moves)), (_game("g2"), _analysis(moves))]
        plan = build_plan(pairs, user="me")
        branches = plan["repertoire"]["white"]
        self.assertTrue(branches)
        for branch in branches:
            self.assertIn("path_ok_rate", branch)
            self.assertEqual(branch["path_ok_rate"], branch["ok_rate"])

    def test_avg_win_loss_skipped_when_missing(self) -> None:
        moves = [_move(s) for s in _SANS[:4]]
        pairs = [(_game("g1"), _analysis(moves, with_avg=False))]
        plan = build_plan(pairs, user="me")
        self.assertIsNone(plan["avg_win_loss"])


class OpeningStatsColorTests(unittest.TestCase):
    def test_single_color_rows_encode_color(self) -> None:
        white_game = _game("w1", color="white")
        black_game = _game("b1", color="black")
        moves = [_move(s) for s in _SANS[:4]]
        rows = opening_stats([(white_game, _analysis(moves)), (black_game, _analysis(moves))])
        by_color = {row["color"] for row in rows}
        self.assertIn("mixed", by_color)
        mixed = next(row for row in rows if row["color"] == "mixed")
        self.assertEqual(mixed["white_games"], 1)
        self.assertEqual(mixed["black_games"], 1)

    def test_white_only_row(self) -> None:
        white_game = _game("w1", color="white")
        moves = [_move(s) for s in _SANS[:4]]
        rows = opening_stats([(white_game, _analysis(moves))])
        self.assertEqual(rows[0]["color"], "white")
        self.assertEqual(rows[0]["white_games"], 1)
        self.assertEqual(rows[0]["black_games"], 0)

    def test_black_only_row(self) -> None:
        black_game = _game("b1", color="black")
        moves = [_move(s) for s in _SANS[:4]]
        rows = opening_stats([(black_game, _analysis(moves))])
        self.assertEqual(rows[0]["color"], "black")
        self.assertEqual(rows[0]["white_games"], 0)
        self.assertEqual(rows[0]["black_games"], 1)


class DetermineUserColorTests(unittest.TestCase):
    def test_matches_white_case_insensitive(self) -> None:
        self.assertEqual(_determine_user_color("Me", "me", "opp"), "white")

    def test_matches_black_case_insensitive(self) -> None:
        self.assertEqual(_determine_user_color("Me", "opp", "ME"), "black")

    def test_no_match_falls_back_to_white_with_warning(self) -> None:
        with self.assertLogs("app.games", level="WARNING"):
            self.assertEqual(_determine_user_color("nobody", "me", "opp"), "white")

    def test_empty_inputs_fall_back_to_white_with_warning(self) -> None:
        with self.assertLogs("app.games", level="WARNING"):
            self.assertEqual(_determine_user_color("", None, None), "white")


class OpeningBookTieBreakTests(unittest.TestCase):
    def test_tied_opponent_first_move_picks_smallest_san(self) -> None:
        def black_game(game_id: str, moves: list[str]) -> Game:
            base = _game(game_id, color="black")
            return Game(
                id=base.id,
                rated=base.rated,
                speed=base.speed,
                created_at=base.created_at,
                status=base.status,
                winner=base.winner,
                white={"name": "opp"},
                black={"name": "me"},
                opening=base.opening,
                eco=base.eco,
                moves=list(moves),
                clocks=[],
                user_color="black",
                opponent="opp",
                result_for_user="win",
            )

        pairs = [
            (black_game("d1", ["d4", "e6", "Nf3", "d5"]), _analysis([])),
            (black_game("d2", ["d4", "e6", "c4", "d5"]), _analysis([])),
            (black_game("e1", ["e4", "e6", "Nf3", "d5"]), _analysis([])),
            (black_game("e2", ["e4", "e6", "d4", "d5"]), _analysis([])),
        ]
        book = build_opening_book(pairs, "black")
        first = next(row for row in book["first_moves"] if row["move"] == "e6")
        # Ничья 2-2 между d4 и e4: правило модуля — (-count, SAN asc), значит d4.
        self.assertEqual(first["main_line"][:2], ["d4", "e6"])


if __name__ == "__main__":
    unittest.main()
