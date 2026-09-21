"""Тесты дебютного репертуара (M4): деревья ходов, слабые линии, PGN/JSON."""

from __future__ import annotations

import io
import json
import unittest

import chess
import chess.pgn

from app.games import Game
from app.repertoire import (
    LineSummary,
    Repertoire,
    RepertoireNode,
    format_lines,
    opening_stats,
    repertoire_to_json,
    repertoire_to_pgn,
)

_DEFAULT_DROP = {"blunder": 20.0, "mistake": 12.0, "inaccuracy": 6.0}


def mkgame(
    id: str,
    moves: list[str],
    user_color: str,
    result: str,
    opening: str | None = None,
    eco: str | None = None,
) -> Game:
    """Минимальная партия с нужными полями для репертуара."""
    return Game(
        id=id,
        white={"name": "A"},
        black={"name": "B"},
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner=None,
        opening=opening,
        eco=eco,
        moves=list(moves),
        clocks=[],
        user_color=user_color,
        opponent="B",
        user_rating=2100,
        user_rating_diff=0,
        result_for_user=result,
    )


def _move_record(san: str, cls: str, drop: float) -> dict:
    """Запись хода в формате summary() анализа (полный набор ключей)."""
    win_before = 60.0
    win_after = win_before - drop
    return {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": win_before,
        "win_after": win_after,
        "drop": drop,
        "classification": cls,
        "best_move_san": None,
        "best_eval": None,
        "best_win": None,
        "cp_loss": drop,
        "clock_used": None,
        "time_pressure": None,
    }


def mkanalysis(
    user_color: str,
    moves_count: int,
    spec: list[str | tuple],
) -> dict:
    """Анализ: spec — список (san) или (san, classification) или (san, cls, drop).

    Пропущенные записи (короче moves_count) трактуются как «ok».
    """
    moves: list[dict] = []
    for ply in range(moves_count):
        item = spec[ply] if ply < len(spec) else ("d4", "good")
        if isinstance(item, tuple):
            san, cls = item[0], item[1]
            drop = float(item[2]) if len(item) > 2 else _DEFAULT_DROP.get(cls, 0.0)
        else:
            san, cls, drop = item, "good", 0.0
        moves.append(_move_record(san, cls, drop))
    return {
        "game_id": "test",
        "user_color": user_color,
        "result_for_user": "win",
        "opponent": "B",
        "acpl": 0.0,
        "accuracy": 100.0,
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


class RepertoireAddGameTests(unittest.TestCase):
    def test_single_game_white_tree_and_lines(self) -> None:
        game = mkgame("g1", ["e4", "e5", "Nf3", "Nc6"], "white", "win")
        analysis = mkanalysis(
            "white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best")]
        )
        repertoire = Repertoire()
        self.assertEqual(repertoire.add_game(game, analysis), 2)

        e4 = repertoire.white.children["e4"]
        self.assertEqual((e4.count, e4.ok, e4.bad), (1, 1, 0))
        self.assertEqual(e4.games, ["g1"])
        self.assertEqual(e4.ok_rate, 1.0)

        nf3 = e4.children["Nf3"]
        self.assertEqual((nf3.count, nf3.ok, nf3.bad), (1, 0, 1))
        self.assertEqual(nf3.ok_rate, 0.0)

        lines = repertoire.lines("white", min_count=1)
        self.assertEqual(len(lines), 2)
        by_moves = {line.moves: line for line in lines}
        self.assertIn(("e4",), by_moves)
        self.assertIn(("e4", "Nf3"), by_moves)
        self.assertEqual(by_moves[("e4",)].count, 1)
        self.assertEqual(by_moves[("e4",)].ok_rate, 1.0)
        self.assertEqual(by_moves[("e4", "Nf3")].ok_rate, 0.0)

        weak = repertoire.weak_lines("white", max_ok_rate=0.7)
        self.assertEqual([line.moves for line in weak], [("e4", "Nf3")])

    def test_two_games_merge_counts_and_black_empty(self) -> None:
        first = mkgame("g1", ["e4", "e5", "Nf3"], "white", "win")
        a1 = mkanalysis("white", 3, [("e4", "good"), ("e5", "best"), ("Nf3", "good")])
        second = mkgame("g2", ["e4", "e5", "Bc4"], "white", "win")
        a2 = mkanalysis("white", 3, [("e4", "good"), ("e5", "best"), ("Bc4", "good")])
        repertoire = Repertoire()
        self.assertEqual(repertoire.add_game(first, a1, max_depth=4), 2)
        self.assertEqual(repertoire.add_game(second, a2, max_depth=4), 2)

        e4 = repertoire.white.children["e4"]
        self.assertEqual((e4.count, e4.ok, e4.bad), (2, 2, 0))
        self.assertEqual(e4.children["Nf3"].count, 1)
        self.assertEqual(e4.children["Nf3"].games, ["g1"])
        self.assertEqual(e4.children["Bc4"].count, 1)
        self.assertEqual(e4.children["Bc4"].games, ["g2"])

        lines = repertoire.lines("white")
        self.assertEqual([line.moves for line in lines], [("e4",), ("e4", "Nf3"), ("e4", "Bc4")])
        self.assertEqual(lines[0].count, 2)
        self.assertEqual(lines[0].ok, 2)
        self.assertEqual(repertoire.lines("black"), [])

    def test_max_depth_limits_inserted_moves(self) -> None:
        game = mkgame(
            "g3",
            ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6"],
            "white",
            "win",
        )
        analysis = mkanalysis(
            "white",
            8,
            [
                ("e4", "good"), ("e5", "best"), ("Nf3", "good"), ("Nc6", "best"),
                ("Bb5", "good"), ("a6", "best"), ("Bxc6", "good"), ("dxc6", "best"),
            ],
        )
        repertoire = Repertoire()
        self.assertEqual(repertoire.add_game(game, analysis, max_depth=3), 3)

        node = repertoire.white.children["e4"].children["Nf3"].children["Bb5"]
        self.assertEqual(node.count, 1)
        self.assertEqual(node.children, {})  # четвёртого хода пользователя нет

    def test_missing_classification_treated_as_ok(self) -> None:
        game = mkgame("g4", ["e4", "e5", "Nf3", "Nc6"], "white", "win")
        analysis = mkanalysis("white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best")])
        del analysis["moves"][2]["classification"]
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        nf3 = repertoire.white.children["e4"].children["Nf3"]
        self.assertEqual((nf3.ok, nf3.bad), (1, 0))

    def test_shorter_analysis_than_moves_is_fine(self) -> None:
        game = mkgame("g5", ["e4", "e5", "Nf3", "Nc6", "Bb5"], "white", "win")
        analysis = mkanalysis("white", 2, [("e4", "good"), ("e5", "best")])
        repertoire = Repertoire()
        self.assertEqual(repertoire.add_game(game, analysis), 3)
        e4 = repertoire.white.children["e4"].children["Nf3"]
        self.assertEqual((e4.ok, e4.bad), (1, 0))

    def test_ok_rate_none_when_count_zero(self) -> None:
        node = RepertoireNode()
        self.assertEqual(node.count, 0)
        self.assertIsNone(node.ok_rate)

    def test_line_summary_fields(self) -> None:
        summary = LineSummary(
            moves=("e4", "Nf3"), count=3, ok=2, bad=1,
            ok_rate=round(2 / 3, 4), games=["a", "b", "c"],
        )
        self.assertEqual(summary.games, ["a", "b", "c"])
        self.assertEqual(summary.count, 3)
        self.assertEqual(summary.path_ok, 0)
        self.assertEqual(summary.path_bad, 0)
        self.assertIsNone(summary.path_ok_rate)

    def test_path_stats_cumulative_along_line(self) -> None:
        game = mkgame(
            "g6",
            ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6"],
            "white",
            "win",
        )
        analysis = mkanalysis(
            "white",
            6,
            [
                ("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best"),
                ("Bc4", "good"), ("Nf6", "best"),
            ],
        )
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        by_moves = {line.moves: line for line in repertoire.lines("white")}

        e4 = by_moves[("e4",)]
        self.assertEqual((e4.path_ok, e4.path_bad), (1, 0))
        self.assertEqual(e4.path_ok_rate, 1.0)

        nf3 = by_moves[("e4", "Nf3")]
        self.assertEqual((nf3.path_ok, nf3.path_bad), (1, 1))
        self.assertEqual(nf3.path_ok_rate, 0.5)

        bc4 = by_moves[("e4", "Nf3", "Bc4")]
        self.assertEqual((bc4.path_ok, bc4.path_bad), (2, 1))
        self.assertAlmostEqual(bc4.path_ok_rate, 2 / 3)
        # узел Bc4 «хороший», но вся линия с ошибкой посередине — слабая
        weak = repertoire.weak_lines("white", max_ok_rate=0.7)
        self.assertEqual(
            [line.moves for line in weak],
            [("e4", "Nf3"), ("e4", "Nf3", "Bc4")],
        )

    def test_deep_isolated_blunder_not_weak_line(self) -> None:
        game = mkgame(
            "g7",
            ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "d3", "d6", "Bg5", "Be7", "Bd3", "O-O"],
            "white",
            "win",
        )
        spec = [("e4", "good"), ("e5", "best"), ("Nf3", "good"), ("Nc6", "best"),
                ("Bc4", "good"), ("Nf6", "best"), ("d3", "good"), ("d6", "best"),
                ("Bg5", "good"), ("Be7", "best"), ("Bd3", "mistake"), ("O-O", "best")]
        analysis = mkanalysis("white", 12, spec)
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)

        deep = {line.moves: line for line in repertoire.lines("white")}[
            ("e4", "Nf3", "Bc4", "d3", "Bg5", "Bd3")
        ]
        self.assertEqual(deep.ok_rate, 0.0)  # последний ход узла — ошибка
        self.assertAlmostEqual(deep.path_ok_rate, 5 / 6)  # но линия в целом крепкая
        self.assertNotIn(
            deep.moves,
            [line.moves for line in repertoire.weak_lines("white", max_ok_rate=0.7)],
        )


class OpeningStatsTests(unittest.TestCase):
    def test_aggregation_and_sorting(self) -> None:
        sic_a = mkgame("s1", ["e4", "c5", "Nf3", "d6"], "white", "win",
                       opening="Sicilian Defense", eco="B20")
        sic_b = mkgame("s2", ["e4", "c5", "Nf3", "d6"], "white", "loss",
                       opening="Sicilian Defense", eco="B20")
        dutch = mkgame("s3", ["d4", "f5", "Bf4", "e6"], "white", "win",
                       opening="Dutch Defense", eco="A80")

        a1 = mkanalysis("white", 4, [("e4", "good"), ("c5", "best"), ("Nf3", "good"), ("d6", "best")])
        a2 = mkanalysis("white", 4, [("e4", "mistake", 15), ("c5", "best"), ("Nf3", "blunder", 20), ("d6", "best")])
        a3 = mkanalysis("white", 4, [("d4", "good"), ("f5", "best"), ("Bf4", "good"), ("e6", "best")])

        rows = opening_stats([(sic_a, a1), (sic_b, a2), (dutch, a3)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["opening"], "Sicilian Defense")  # ошибки убыв. -> первым
        self.assertEqual(rows[1]["opening"], "Dutch Defense")

        sicilian = rows[0]
        self.assertEqual(sicilian["eco"], "B20")
        self.assertEqual(sicilian["games"], 2)
        self.assertEqual(sicilian["wins"], 1)
        self.assertEqual(sicilian["draws"], 0)
        self.assertEqual(sicilian["points_pct"], 50.0)
        self.assertEqual(sicilian["bad_moves"], 2)
        self.assertEqual(sicilian["errors_per_game"], 1.0)
        self.assertEqual(sicilian["avg_drop"], 17.5)

        dutch_row = rows[1]
        self.assertEqual(dutch_row["points_pct"], 100.0)
        self.assertEqual(dutch_row["errors_per_game"], 0.0)
        self.assertEqual(dutch_row["avg_drop"], 0.0)

    def test_missing_opening_name(self) -> None:
        game = mkgame("n1", ["e4", "e5"], "white", "draw", opening=None, eco="C20")
        analysis = mkanalysis("white", 2, [("e4", "good"), ("e5", "best")])
        rows = opening_stats([(game, analysis)])
        self.assertEqual(rows[0]["opening"], "Без названия")
        self.assertEqual(rows[0]["eco"], "C20")


class RepertoireToPgnTests(unittest.TestCase):
    def _make_pairs(self) -> list[tuple[Game, dict]]:
        white_win = mkgame("p1", ["e4", "e5", "Nf3", "Nc6"], "white", "win",
                           opening="Sicilian Defense", eco="B20")
        a1 = mkanalysis("white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "good"), ("Nc6", "best")])
        white_loss = mkgame("p2", ["e4", "c5", "Nf3", "d6"], "white", "loss",
                            opening="Sicilian Defense", eco="B20")
        a2 = mkanalysis("white", 4, [("e4", "blunder", 20), ("c5", "best"), ("Nf3", "good"), ("d6", "best")])
        black_draw = mkgame("p3", ["d4", "d5", "c4", "e6"], "black", "draw",
                            opening="Queen's Gambit Declined", eco="D30")
        a3 = mkanalysis("black", 4, [("d4", "best"), ("d5", "good"), ("c4", "best"), ("e6", "good")])
        return [(white_win, a1), (white_loss, a2), (black_draw, a3)]

    def test_color_filter_headers_results_comments(self) -> None:
        text = repertoire_to_pgn(self._make_pairs(), "white")
        self.assertIn('[Event "Репертуар белых: Sicilian Defense"]', text)
        self.assertIn('[Result "1-0"]', text)
        self.assertIn('[Result "0-1"]', text)
        self.assertNotIn("Queen's Gambit Declined", text)
        self.assertIn("ok: e4", text)
        self.assertIn("ЗЕВ", text)
        self.assertIn("ошибка", text)
        self.assertIn("ok: Nf3", text)

    def test_parses_back_to_multiple_games(self) -> None:
        text = repertoire_to_pgn(self._make_pairs(), "white")
        stream = io.StringIO(text)
        games: list[chess.pgn.Game] = []
        while True:
            parsed = chess.pgn.read_game(stream)
            if parsed is None:
                break
            games.append(parsed)
        self.assertGreaterEqual(len(games), 2)

    def test_black_filter(self) -> None:
        text = repertoire_to_pgn(self._make_pairs(), "black")
        self.assertIn('[Event "Репертуар чёрных: Queen\'s Gambit Declined"]', text)
        self.assertIn('[Result "1/2-1/2"]', text)
        self.assertNotIn("Sicilian Defense", text)

    def test_opening_filter_by_substring(self) -> None:
        text = repertoire_to_pgn(self._make_pairs(), "white", opening_filter=["sicilian"])
        self.assertIn("Sicilian Defense", text)
        self.assertNotIn("Queen's Gambit Declined", text)


class RepertoireToJsonTests(unittest.TestCase):
    def test_json_keys_and_lines(self) -> None:
        game = mkgame("j1", ["e4", "e5", "Nf3", "Nc6"], "white", "win",
                      opening="Sicilian Defense", eco="B20")
        analysis = mkanalysis("white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "good"), ("Nc6", "best")])
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        data = json.loads(repertoire_to_json(repertoire, [(game, analysis)]))
        self.assertIn("white_lines", data)
        self.assertIn("black_lines", data)
        self.assertIn("openings", data)
        self.assertEqual(len(data["white_lines"]), 2)
        self.assertEqual(data["black_lines"], [])
        top = data["white_lines"][0]
        self.assertEqual(top["moves"], ["e4"])
        self.assertEqual(top["count"], 1)
        self.assertEqual(top["ok"], 1)
        self.assertEqual(top["ok_rate"], 1.0)
        self.assertEqual(data["openings"][0]["opening"], "Sicilian Defense")


class FormatLinesTests(unittest.TestCase):
    def _weak_repertoire(self) -> Repertoire:
        game = mkgame("f1", ["e4", "e5", "Nf3", "Nc6"], "white", "win")
        analysis = mkanalysis(
            "white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best")]
        )
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        return repertoire

    def test_header_and_strong_line(self) -> None:
        repertoire = self._weak_repertoire()
        text = format_lines("white", repertoire.lines("white"))
        self.assertIn("Репертуар белых:", text)
        self.assertIn("1.e4", text)
        self.assertIn("прочность 100%", text)

    def test_weak_mark_only_on_weak_lines(self) -> None:
        repertoire = self._weak_repertoire()
        text = format_lines("white", repertoire.lines("white"))
        self.assertEqual(text.count("⚠ слабое место"), 1)
        self.assertIn("1.e4 2.Nf3", text)

    def test_strong_only_repertoire_has_no_warning(self) -> None:
        game = mkgame("f2", ["e4", "e5", "Nf3"], "white", "win")
        analysis = mkanalysis("white", 3, [("e4", "good"), ("e5", "best"), ("Nf3", "good")])
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        text = format_lines("white", repertoire.lines("white"))
        self.assertNotIn("⚠", text)

    def test_black_heading(self) -> None:
        game = mkgame("f3", ["d4", "d5", "c4", "e6"], "black", "win")
        analysis = mkanalysis("black", 4, [("d4", "best"), ("d5", "good"), ("c4", "best"), ("e6", "good")])
        repertoire = Repertoire()
        repertoire.add_game(game, analysis)
        text = format_lines("black", repertoire.lines("black"))
        self.assertIn("Репертуар чёрных:", text)
        self.assertIn("1.d5", text)
        self.assertIn("2.e6", text)


if __name__ == "__main__":
    unittest.main()