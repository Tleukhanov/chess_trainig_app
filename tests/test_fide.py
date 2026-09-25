"""Офлайн-тесты логики FIDE и разбора турнира."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.fide as fide
from app.fide import (
    FideRatings,
    TournamentGame,
    build_fide_trend,
    build_tournament_report,
    format_fide_brief,
    format_fide_trend,
    format_tournament,
)


def _ratings(points: list[tuple[int, int]]) -> FideRatings:
    return FideRatings(history={"standard": list(points)})


def _tournament_games() -> list[TournamentGame]:
    return [
        TournamentGame("Alpha", "white", "win", 2100),
        TournamentGame("Bravo", "black", "draw", 2100),
        TournamentGame("Charlie", "white", "loss", 2100),
        TournamentGame("Delta", "black", "draw", 2100),
    ]


class BuildFideTrendTests(unittest.TestCase):
    def test_groups_points_and_computes_deltas(self) -> None:
        ratings = FideRatings(
            history={
                "standard": [
                    (202301, 2000),
                    (202302, 2010),
                    (202303, 2025),
                    (202304, 2030),
                    (202305, 2050),
                    (202306, 2060),
                    (202307, 2075),
                    (202308, 2095),
                ]
            }
        )

        trend = build_fide_trend(ratings, windows=3)

        self.assertEqual(trend["best_current"], 2095)
        controls = {control["key"]: control for control in trend["controls"]}
        self.assertEqual(set(controls), {"standard", "rapid", "blitz"})
        standard = controls["standard"]
        self.assertEqual(standard["current"], 2095)
        self.assertEqual(standard["points"], 8)
        self.assertEqual(standard["direction"], "up")
        self.assertEqual(standard["delta"], 95)
        self.assertEqual([row["periods"] for row in standard["rows"]], [3, 3, 2])
        self.assertEqual(
            [row["first_rating"] for row in standard["rows"]],
            [2000, 2030, 2075],
        )
        self.assertEqual(
            [row["last_rating"] for row in standard["rows"]],
            [2025, 2060, 2095],
        )
        self.assertEqual(
            [row["avg_rating"] for row in standard["rows"]],
            [2012, 2047, 2085],
        )
        self.assertEqual([row["delta"] for row in standard["rows"]], [25, 30, 20])

    def test_short_history_clamps_window_count(self) -> None:
        trend = build_fide_trend(
            _ratings([(202401, 2000), (202402, 2040)]),
            windows=8,
        )

        standard = trend["controls"][0]
        self.assertEqual(standard["points"], 2)
        self.assertEqual(len(standard["rows"]), 2)
        self.assertEqual([row["index"] for row in standard["rows"]], [1, 2])
        self.assertEqual(standard["direction"], "up")
        self.assertEqual(standard["delta"], 40)


class FideBriefTests(unittest.TestCase):
    def test_improving_brief_contains_id_and_up_marker(self) -> None:
        trend = build_fide_trend(
            _ratings([(202401, 2000), (202402, 2020)]),
            windows=2,
        )

        text = format_fide_brief(trend, user_fide="12345678")

        self.assertIsInstance(text, str)
        self.assertTrue(text)
        self.assertIn("12345678", text)
        self.assertIn("▲", text)
        self.assertNotIn("▼", text)

    def test_declining_brief_contains_id_and_down_marker(self) -> None:
        trend = build_fide_trend(
            _ratings([(202401, 2040), (202402, 2000)]),
            windows=2,
        )

        text = format_fide_brief(trend, user_fide="12345678")

        self.assertIsInstance(text, str)
        self.assertTrue(text)
        self.assertIn("12345678", text)
        self.assertIn("▼", text)
        self.assertNotIn("▲", text)


class FideTrendFormatTests(unittest.TestCase):
    def test_improving_trend_contains_id_and_up_marker(self) -> None:
        trend = build_fide_trend(
            _ratings([(202401, 2000), (202402, 2020)]),
            windows=2,
        )

        text = format_fide_trend(trend, user_fide="12345678")

        self.assertIsInstance(text, str)
        self.assertTrue(text)
        self.assertIn("12345678", text)
        self.assertIn("▲", text)
        self.assertNotIn("▼", text)

    def test_declining_trend_contains_id_and_down_marker(self) -> None:
        trend = build_fide_trend(
            _ratings([(202401, 2040), (202402, 2000)]),
            windows=2,
        )

        text = format_fide_trend(trend, user_fide="12345678")

        self.assertIsInstance(text, str)
        self.assertTrue(text)
        self.assertIn("12345678", text)
        self.assertIn("▼", text)
        self.assertNotIn("▲", text)


class TournamentReportTests(unittest.TestCase):
    def test_scores_points_and_average_opponent_rating(self) -> None:
        report = build_tournament_report(_tournament_games())

        self.assertEqual(report["games"], 4)
        self.assertEqual(report["points"], 2.0)
        self.assertEqual(report["avg_opponent"], 2100)
        self.assertEqual(report["performance"], 2100)
        self.assertIsNone(report["delta"])
        self.assertEqual([row["points"] for row in report["rows"]], [1.0, 0.5, 0.0, 0.5])

    def test_initial_rating_calculates_delta(self) -> None:
        report = build_tournament_report(
            _tournament_games(),
            initial_rating=1900,
        )

        self.assertEqual(report["performance"], 2100)
        self.assertEqual(report["delta"], 21)


class TournamentFormatTests(unittest.TestCase):
    def test_includes_title_and_summary(self) -> None:
        text = format_tournament(
            build_tournament_report(_tournament_games()),
            title="City Cup",
        )

        self.assertIsInstance(text, str)
        self.assertIn("City Cup", text)
        self.assertIn("партий: 4", text)
        self.assertIn("очков: 2.0", text)

    def test_empty_games(self) -> None:
        text = format_tournament(
            build_tournament_report([]),
            title="City Cup",
        )

        self.assertIsInstance(text, str)
        self.assertIn("City Cup", text)
        self.assertIn("результатов нет", text)


class TournamentGameTests(unittest.TestCase):
    def test_dataclass_defaults(self) -> None:
        game = TournamentGame("Alice")

        self.assertEqual(
            (game.opponent, game.color, game.result, game.opponent_rating),
            ("Alice", "white", "draw", None),
        )
        self.assertEqual(game.points(), 0.5)
        self.assertEqual(game.color_txt(), "белые")
        self.assertEqual(game.result_txt(), "ничья")


class FideCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.cache_path = self.tmpdir / "fide.json"

    def test_cache_for_different_id_is_not_reused(self) -> None:
        with patch("app.fide._cache_path", return_value=self.cache_path):
            fide.fide_cache_save(
                profile=fide.FidePlayer(id="1503014", name="Wrong"),
                ratings=fide.FideRatings(
                    history={"standard": [(202301, 1900)]}
                ),
                ratings_id="1503014",
            )
            with patch(
                "app.fide._http_json",
                side_effect=[
                    {"id": "4000001", "name": "Right", "standard": 2100},
                    {"standard": ["2024022000"]},
                ],
            ) as http:
                player = fide.fetch_fide_player(" 4000001 ")
                ratings = fide.fetch_fide_ratings("4000001")

        self.assertEqual(http.call_count, 2)
        self.assertEqual(player.id, "4000001")
        self.assertEqual(player.name, "Right")
        self.assertEqual(ratings.history["standard"], [(202402, 2000)])

    def test_empty_cached_ratings_fall_back_to_network(self) -> None:
        with patch("app.fide._cache_path", return_value=self.cache_path):
            fide.fide_cache_save(
                ratings=fide.FideRatings(history={"standard": []}),
                ratings_id="12345678",
            )
            with patch(
                "app.fide._http_json",
                return_value={"standard": ["2024012100"]},
            ) as http:
                ratings = fide.fetch_fide_ratings("12345678")

        http.assert_called_once()
        self.assertEqual(ratings.history["standard"], [(202401, 2100)])
        saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ratings"]["id"], "12345678")
        self.assertEqual(saved["ratings"]["standard"], ["2024012100"])

    def test_legacy_cache_without_ratings_id_is_refetched(self) -> None:
        legacy = {
            "profile": {
                "id": "1503014",
                "name": "Legacy Player",
                "federation": "TST",
                "year": 1990,
                "standard": 2000,
                "rapid": None,
                "blitz": None,
            },
            "ratings": {"standard": ["2023011900"]},
        }
        self.cache_path.write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        with patch("app.fide._cache_path", return_value=self.cache_path):
            with patch(
                "app.fide._http_json",
                return_value={"standard": ["2024022000"]},
            ) as http:
                player = fide.fetch_fide_player("1503014")
                ratings = fide.fetch_fide_ratings("1503014")

        http.assert_called_once()
        self.assertEqual(player.name, "Legacy Player")
        self.assertEqual(ratings.history["standard"], [(202402, 2000)])


if __name__ == "__main__":
    unittest.main()
