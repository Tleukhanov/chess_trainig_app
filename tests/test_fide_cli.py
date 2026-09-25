"""Офлайн-тесты CLI-команд fide и tournament."""

from __future__ import annotations

import io
import json
import runpy
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.fide import FidePlayer, FideRatings

_ROOT = Path(__file__).resolve().parent.parent


def _load_cli() -> dict:
    return runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_fide_cli_test")


class TournamentCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_happy_path_prints_report(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](
                [
                    "tournament",
                    "--games",
                    "Alpha:white:win:2100",
                    "--games",
                    "Bravo:black:draw:2050",
                    "--games",
                    "Charlie:white:loss:2000",
                ]
            )

        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("партий: 3", text)
        self.assertIn("очков: 1.5", text)
        self.assertIn("Alpha", text)
        self.assertIn("Charlie", text)

    def test_json_output(self) -> None:
        json_path = self.tmpdir / "tournament.json"
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](
                [
                    "tournament",
                    "--games",
                    "Alpha:white:win:2100",
                    "--games",
                    "Bravo:black:draw:2050",
                    "--json",
                    str(json_path),
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(json_path.is_file())
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["games"], 2)
        self.assertEqual(data["points"], 1.5)
        self.assertEqual(data["avg_opponent"], 2075)
        self.assertEqual(len(data["rows"]), 2)
        self.assertIn(str(json_path), output.getvalue())

    def test_invalid_spec_fails_without_traceback(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](["tournament", "--games", "Alpha:white"])

        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Ошибка", text)
        self.assertIn("неверный формат", text)
        self.assertNotIn("Traceback", text)


class FideCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_happy_path_and_json_output_without_http(self) -> None:
        player = FidePlayer(
            id="12345678",
            name="Test Player",
            federation="TST",
            birth_year=1990,
            standard=2140,
        )
        ratings = FideRatings(
            history={
                "standard": [
                    (202401, 2000),
                    (202402, 2020),
                    (202403, 2040),
                    (202404, 2140),
                ]
            }
        )
        json_path = self.tmpdir / "fide.json"
        output = io.StringIO()
        with (
            patch("app.fide.fetch_fide_player", return_value=player) as fetch_player,
            patch("app.fide.fetch_fide_ratings", return_value=ratings) as fetch_ratings,
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network access disabled"),
            ) as urlopen,
        ):
            with redirect_stdout(output):
                code = self.cli["main"](
                    [
                        "fide",
                        "--id",
                        "12345678",
                        "--windows",
                        "2",
                        "--json",
                        str(json_path),
                    ]
                )

        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(text.strip())
        self.assertIn("Test Player", text)
        self.assertIn("12345678", text)
        fetch_player.assert_called_once_with("12345678")
        fetch_ratings.assert_called_once_with("12345678")
        urlopen.assert_not_called()
        self.assertTrue(json_path.is_file())
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], "12345678")
        self.assertEqual(data["player"]["name"], "Test Player")
        self.assertEqual(data["trend"]["best_current"], 2140)

    def test_missing_id_fails_without_http_or_traceback(self) -> None:
        empty_settings = replace(self.cli["settings"], fide_id="")
        output = io.StringIO()
        with (
            patch.dict(self.cli, {"settings": empty_settings}),
            patch(
                "app.fide.fetch_fide_player",
                side_effect=AssertionError("profile fetch must not run"),
            ) as fetch_player,
            patch(
                "app.fide.fetch_fide_ratings",
                side_effect=AssertionError("ratings fetch must not run"),
            ) as fetch_ratings,
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network access disabled"),
            ) as urlopen,
        ):
            with redirect_stdout(output):
                code = self.cli["main"](["fide"])

        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("FIDE ID", text)
        self.assertIn("--id", text)
        self.assertNotIn("Traceback", text)
        fetch_player.assert_not_called()
        fetch_ratings.assert_not_called()
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
