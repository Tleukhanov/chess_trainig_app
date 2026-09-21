"""Тесты плана тренировки (M4/B4): сборка из кеша и текстовый вывод."""

from __future__ import annotations

import io
import runpy
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app.games import Game
from app.plan import build_plan, format_plan

_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_DROP = {"blunder": 20.0, "mistake": 12.0, "inaccuracy": 6.0}


def mkgame(
    id: str,
    moves: list[str],
    user_color: str,
    result: str,
    opening: str | None = None,
    eco: str | None = None,
) -> Game:
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


def mkanalysis(user_color: str, moves_count: int, spec: list[str | tuple]) -> dict:
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
        "acpl": 45.0,
        "accuracy": 92.0,
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def fixture_pairs() -> list[tuple[Game, dict]]:
    sicilian = mkgame(
        "a", ["e4", "e5", "Nf3", "Nc6"], "white", "win",
        opening="Sicilian Defense", eco="B20",
    )
    a1 = mkanalysis(
        "white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best")]
    )
    dutch = mkgame(
        "b", ["e4", "e5", "Bc4", "Nc6"], "white", "loss",
        opening="Dutch Defense", eco="A80",
    )
    a2 = mkanalysis(
        "white", 4, [("e4", "blunder", 20), ("e5", "best"), ("Bc4", "good"), ("Nc6", "best")]
    )
    return [(sicilian, a1), (dutch, a2)]


_HUMANITY = {
    "total_bad": 3,
    "verdicts": {"natural": 1, "borderline": 0, "unnatural": 2},
    "unnatural": [
        {"game_id": "a", "ply": 4, "san": "Nf3", "beta": -18.0, "drop": 12.0, "classification": "mistake"}
    ],
}


class BuildPlanTests(unittest.TestCase):
    def test_structure_and_counts(self) -> None:
        plan = build_plan(
            fixture_pairs(),
            user="u",
            humanity=_HUMANITY,
            drills_total=8,
            drills_unnatural=5,
        )
        self.assertEqual(plan["user"], "u")
        self.assertEqual(plan["games"], 2)
        self.assertEqual(plan["score_pct"], 50.0)
        self.assertEqual(plan["acpl"], 45.0)
        white = plan["repertoire"]["white"]
        self.assertGreaterEqual(len(white), 1)
        self.assertEqual(white[0]["moves"], ["e4"])
        self.assertEqual(white[0]["count"], 2)
        self.assertAlmostEqual(white[0]["ok_rate"], 0.5)
        self.assertEqual(plan["patterns"]["total_bad"], 2)
        self.assertEqual(plan["humanity"]["unnatural"], 2)
        self.assertEqual(plan["humanity"]["unnatural_share_pct"], 66.7)
        self.assertEqual(plan["drills"], {"total": 8, "unnatural": 5})

    def test_no_humanity(self) -> None:
        plan = build_plan(fixture_pairs())
        self.assertIsNone(plan["humanity"])

    def test_empty_pairs(self) -> None:
        plan = build_plan([])
        self.assertEqual(plan["games"], 0)
        self.assertIsNone(plan["score_pct"])
        self.assertEqual(plan["repertoire"]["white"], [])
        self.assertEqual(plan["patterns"]["total_bad"], 0)


class FormatPlanTests(unittest.TestCase):
    def test_sections_present(self) -> None:
        plan = build_plan(
            fixture_pairs(),
            user="u",
            humanity=_HUMANITY,
            drills_total=8,
            drills_unnatural=5,
        )
        text = format_plan(plan)
        self.assertIn("План тренировки · u", text)
        self.assertIn("Репертуар — закрепляй частые ветви", text)
        self.assertIn("Слабые дебюты", text)
        self.assertIn("Узоры ошибок", text)
        self.assertIn("Фазы", text)
        self.assertIn("Человечность", text)
        self.assertIn("неестественные промахи", text)
        self.assertIn("data/drills_unnatural.pgn: 5", text)
        self.assertIn("1.e4", text)

    def test_empty_plan(self) -> None:
        text = format_plan(build_plan([]))
        self.assertIn("План тренировки", text)


class PlanCliTests(unittest.TestCase):
    def test_parser_and_format_import_wire(self) -> None:
        cli = runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_plan_test")
        parser = cli["_make_parser"]()
        args = parser.parse_args(["plan", "--user", "X", "--max-depth", "10"])
        self.assertEqual(args.command, "plan")
        self.assertEqual(args.max_depth, 10)
        # format_plan печатается _cmd_plan через та же функцию, что тестили выше
        with redirect_stdout(io.StringIO()):
            code = cli["main"](["plan", "--user", "__no_such_user__"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()