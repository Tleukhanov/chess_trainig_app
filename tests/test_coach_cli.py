"""Тесты CLI coach: точечный переанализ партии (--game)."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_cli() -> dict:
    import runpy

    return runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_coach_cli_test")


class CoachCliParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = _load_cli()

    def test_game_without_user_parses(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(["coach", "--game", "abc123", "def456", "--depth", "18"])
        self.assertEqual(args.command, "coach")
        self.assertEqual(args.game, ["abc123", "def456"])
        self.assertIsNone(args.user)
        self.assertEqual(args.depth, 18)

    def test_no_user_no_game_errors_at_runtime(self) -> None:
        # argparse больше не требует --user; отсутствие обоих ловится в _cmd_coach.
        with redirect_stdout(io.StringIO()):
            code = self.cli["main"](["coach"])
        self.assertEqual(code, 1)

    def test_unknown_game_returns_hint(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = self.cli["main"](["coach", "--game", "__no_such_game_id__"])
        self.assertEqual(code, 1)
        self.assertIn("не найдены", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()