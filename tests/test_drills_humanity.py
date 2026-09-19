"""Тесты фильтрации дрелей по «человечности» (M3) и парсера CLI."""

from __future__ import annotations

import runpy
import unittest
from pathlib import Path

from app.drills import collect_drills
from app.games import Game

_MOVES = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6"]


def _move(
    san: str,
    cls: str,
    drop: float,
    win_before: float,
    best: str,
) -> dict:
    return {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": win_before,
        "win_after": win_before - drop,
        "drop": drop,
        "classification": cls,
        "best_move_san": best,
        "best_eval": None,
        "best_win": win_before,
        "cp_loss": drop,
        "clock_used": None,
        "time_pressure": None,
    }


def _game(**overrides) -> Game:
    params = dict(
        id="d1",
        white={"name": "A"},
        black={"name": "B"},
        opening="Sicilian Defense",
        eco="B20",
        moves=list(_MOVES),
        user_color="white",
        result_for_user="win",
        opponent="B",
    )
    params.update(overrides)
    return Game(**params)


def _analysis() -> dict:
    """Анализ белыми: дрели на ходах белых ply4 (d4) и ply6 (Nxd4)."""
    moves: list[dict] = []
    for ply, san in enumerate(_MOVES):
        if ply == 4:
            moves.append(_move(san, "blunder", 30.0, 70.0, "Bc4"))
        elif ply == 6:
            moves.append(_move(san, "mistake", 15.0, 65.0, "Bb5+"))
        elif ply % 2 == 0:
            moves.append(_move(san, "good", 0.0, 60.0, "Qh5"))
        else:
            moves.append(_move(san, "best", 0.0, 60.0, "Qh5"))
    return {
        "game_id": "d1",
        "user_color": "white",
        "result_for_user": "win",
        "blunders": [4],
        "mistakes": [6],
        "moves": moves,
    }


def _humanity() -> list[dict]:
    return [
        {"game_id": "d1", "ply": 4, "verdict": "unnatural"},
        {"game_id": "d1", "ply": 6, "verdict": "natural"},
    ]


class HumanityFilterTests(unittest.TestCase):
    def test_no_filter_returns_both_drills(self) -> None:
        drills = collect_drills(_game(), _analysis())
        self.assertEqual([d.ply for d in drills], [4, 6])

    def test_filter_unnatural_only(self) -> None:
        drills = collect_drills(
            _game(), _analysis(), humanity=_humanity(), allowed_verdicts={"unnatural"}
        )
        self.assertEqual([d.ply for d in drills], [4])

    def test_filter_natural_only(self) -> None:
        drills = collect_drills(
            _game(), _analysis(), humanity=_humanity(), allowed_verdicts={"natural"}
        )
        self.assertEqual([d.ply for d in drills], [6])

    def test_filter_both_verdicts(self) -> None:
        drills = collect_drills(
            _game(),
            _analysis(),
            humanity=_humanity(),
            allowed_verdicts={"unnatural", "natural"},
        )
        self.assertEqual([d.ply for d in drills], [4, 6])

    def test_allowed_verdicts_without_humanity_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            collect_drills(_game(), _analysis(), allowed_verdicts={"unnatural"})
        message = str(ctx.exception)
        self.assertIn("человечность", message)
        self.assertIn("humanize", message)

    def test_foreign_game_id_filters_all(self) -> None:
        drills = collect_drills(
            _game(),
            _analysis(),
            humanity=[{"game_id": "zz", "ply": 4, "verdict": "unnatural"}],
            allowed_verdicts={"unnatural"},
        )
        self.assertEqual(drills, [])


_ROOT = Path(__file__).resolve().parent.parent


def _load_cli() -> dict:
    """Загружает __main__.py без исполнения диспетчера (как python -m trainer)."""
    return runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_cli_test")


class CliParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def test_drills_verdict_args_parse(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(
            ["drills", "--user", "X", "--verdict", "unnatural"]
        )
        self.assertEqual(args.verdict, ["unnatural"])
        self.assertIsNone(args.humanity)
        self.assertEqual(args.user, "X")

    def test_humanize_args_parse(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(
            ["humanize", "--engine", "maia", "--out", "/tmp/x.json"]
        )
        self.assertEqual(args.engine, "maia")
        self.assertEqual(args.out, "/tmp/x.json")
        self.assertIsNone(args.user)


if __name__ == "__main__":
    unittest.main()