"""Тесты динамики прогресса (M5): окна партий, метрики, тренд и CLI."""

from __future__ import annotations

import io
import runpy
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app.games import Game
from app.progress import (
    BUILTIN_METRICS,
    build_progress,
    format_progress,
    progress_brief,
)

_ROOT = Path(__file__).resolve().parent.parent

_DAY_MS = 24 * 3600 * 1000


def mkgame(
    id: str,
    created_at: int,
    moves: list[str],
    user_color: str,
    result: str,
    rating: int | None = 2100,
) -> Game:
    return Game(
        id=id,
        rated=True,
        speed="rapid",
        created_at=created_at,
        status="win",
        winner=None,
        opening="Test Opening",
        eco="B20",
        moves=list(moves),
        clocks=[],
        user_color=user_color,
        opponent="B",
        user_rating=rating,
        user_rating_diff=0,
        result_for_user=result,
    )


def _move_record(san: str, cls: str, drop: float) -> dict:
    return {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": 60.0,
        "win_after": 60.0 - drop,
        "drop": drop,
        "classification": cls,
        "best_move_san": None,
        "best_eval": None,
        "best_win": None,
        "cp_loss": drop,
        "clock_used": None,
        "time_pressure": None,
    }


def mkanalysis(acpl: float, accuracy: float, blunders: int = 1, mistakes: int = 1) -> dict:
    moves: list[dict] = []
    for ply in range(6):
        san = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"][ply]
        if ply == 0:
            moves.append(_move_record(san, "good", 0.0))
        elif ply == 2:
            moves.append(_move_record(san, "blunder" if blunders else "good", 20.0 if blunders else 0.0))
        elif ply == 4:
            moves.append(_move_record(san, "mistake" if mistakes else "good", 12.0 if mistakes else 0.0))
        else:
            moves.append(_move_record(san, "best", 0.0))
    return {
        "game_id": "test",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "B",
        "acpl": acpl,
        "accuracy": accuracy,
        "blunders": [2] if blunders else [],
        "mistakes": [4] if mistakes else [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def _pairs_improving() -> list[tuple[Game, dict]]:
    """6 партий: первые 3 — слабые, последние 3 — сильные."""
    early = [
        (mkgame("e1", 3000, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "loss", 2100),
         mkanalysis(45.0, 80.0, blunders=2, mistakes=1)),
        (mkgame("e2", 3300, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "draw", 2105),
         mkanalysis(40.0, 82.0, blunders=1, mistakes=2)),
        (mkgame("e3", 3600, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "win", 2110),
         mkanalysis(35.0, 84.0, blunders=1, mistakes=1)),
    ]
    late = [
        (mkgame("l1", 9000, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "win", 2120),
         mkanalysis(15.0, 94.0, blunders=0, mistakes=0)),
        (mkgame("l2", 9400, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "win", 2130),
         mkanalysis(12.0, 96.0, blunders=0, mistakes=1)),
        (mkgame("l3", 9800, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "white", "draw", 2140),
         mkanalysis(10.0, 97.0, blunders=0, mistakes=0)),
    ]
    return early + late


def _humanity() -> dict:
    items: list[dict] = []
    game_id = None
    for game, _ in _pairs_improving():
        if game.created_at < 5000:
            verdicts = {"e1": ["unnatural", "unnatural"], "e2": ["unnatural"], "e3": ["unnatural", "natural"]}
        else:
            verdicts = {"l1": ["natural"], "l2": ["borderline"], "l3": ["natural"]}
        for verdict in verdicts.get(game.id, []):
            items.append({"game_id": game.id, "ply": 0, "verdict": verdict})
    return {"total_bad": len(items), "items": items}


class BuildProgressTests(unittest.TestCase):
    def test_two_windows_metrics(self) -> None:
        progress = build_progress(_pairs_improving(), user="u", windows=2, humanity=_humanity())
        self.assertEqual(progress["user"], "u")
        self.assertEqual(progress["games"], 6)
        self.assertEqual(progress["windows"], 2)
        rows = progress["windows_rows"]
        self.assertEqual([r["games"] for r in rows], [3, 3])
        # ранние партии слабее: ACPL выше, точность ниже
        self.assertGreater(rows[0]["acpl"], rows[1]["acpl"])
        self.assertLess(rows[0]["accuracy"], rows[1]["accuracy"])
        self.assertGreater(rows[0]["blunders_per_game"], rows[1]["blunders_per_game"])
        self.assertGreater(rows[0]["unnatural_share_pct"], rows[1]["unnatural_share_pct"])
        self.assertLess(rows[0]["avg_rating"], rows[1]["avg_rating"])
        self.assertEqual(rows[0]["dates"], "01.01.70")  # все созданы в один день (эпоха)

    def test_windows_sorted_by_date(self) -> None:
        pairs = list(reversed(_pairs_improving()))
        progress = build_progress(pairs, windows=2)
        rows = [row for row in progress["windows_rows"]]
        self.assertEqual([r["games"] for r in rows], [3, 3])
        # прогресс всегда сортирует по дате: первые окна — старые (слабые) партии
        self.assertGreater(rows[0]["acpl"], rows[1]["acpl"])

    def test_trend_improving(self) -> None:
        progress = build_progress(_pairs_improving(), user="u", windows=2)
        trend = progress["trend"]
        self.assertEqual(trend["overall"], "improving")
        self.assertIn("acpl", trend["improving"])
        self.assertIn("accuracy", trend["improving"])
        self.assertIn("avg_rating", trend["improving"])
        self.assertEqual(trend["worsening"], [])

    def test_empty_pairs(self) -> None:
        progress = build_progress([], user="u", windows=5)
        self.assertEqual(progress["games"], 0)
        self.assertEqual(progress["windows"], 0)
        self.assertEqual(progress["windows_rows"], [])
        self.assertEqual(progress["trend"]["overall"], "flat")

    def test_windows_clamp_to_games(self) -> None:
        pairs = _pairs_improving()[:1]
        progress = build_progress(pairs, windows=5)
        self.assertEqual(progress["windows"], 1)
        self.assertEqual(progress["windows_rows"][0]["games"], 1)

    def test_builtin_metrics_are_finite(self) -> None:
        self.assertIn("acpl", BUILTIN_METRICS)
        self.assertIn("unnatural_share_pct", BUILTIN_METRICS)


class FormatProgressTests(unittest.TestCase):
    def test_contains_header_and_trend(self) -> None:
        text = format_progress(build_progress(_pairs_improving(), user="u", windows=2))
        self.assertIn("Динамика прогресса", text)
        self.assertIn("партий: 6", text)
        self.assertIn("Динамика (первое → последнее окно)", text)
        self.assertIn("Итог", text)
        self.assertIn("ACPL", text)

    def test_empty_text(self) -> None:
        text = format_progress(build_progress([]))
        self.assertIn("Динамика прогресса", text)


class ProgressBriefTests(unittest.TestCase):
    def test_brief_summary(self) -> None:
        progress = build_progress(_pairs_improving(), user="u", windows=2)
        brief = progress_brief(progress)
        self.assertIn("acpl", brief)
        self.assertIn("общий тренд", brief)
        self.assertIn("improving", brief)

    def test_brief_empty_when_single_window(self) -> None:
        pairs = _pairs_improving()[:1]
        self.assertEqual(progress_brief(build_progress(pairs, windows=5)), "")


class ProgressCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_progress_test")

    def test_parser_defaults(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(["progress", "--user", "X", "--windows", "3"])
        self.assertEqual(args.command, "progress")
        self.assertEqual(args.windows, 3)
        self.assertIsNone(args.json)
        self.assertEqual(args.user, "X")

    def test_dispatch_unknown_user_returns_1(self) -> None:
        with redirect_stdout(io.StringIO()):
            code = self.cli["main"](["progress", "--user", "__no_such_user__"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()