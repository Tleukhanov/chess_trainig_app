"""Тесты CLI дрелей под соперника (M8): разбор аргументов, кеш двух игроков, вывод."""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app import paths as paths_mod
from app.db import Database
from app.games import Game
from tests.test_opponent import mkanalysis, mkgame

_ROOT = Path(__file__).resolve().parent.parent


def _load_cli():
    """Импортирует __main__.py как обычный модуль, чтобы патчить его глобалы."""
    spec = importlib.util.spec_from_file_location("trainer_drills_cli", _ROOT / "__main__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["trainer_drills_cli"] = module
    spec.loader.exec_module(module)
    return module


def _save(db: Database, user: str, pairs: list[tuple[Game, dict]]) -> None:
    db.save_games([game for game, _ in pairs], user)
    for game, analysis in pairs:
        db.save_analysis(game.id, analysis, depth=8)


def _vs_opponent(game: Game, name: str) -> Game:
    return dataclasses.replace(game, opponent=name)


def my_pairs_vs(opponent: str, count: int = 2, prefix: str = "vs") -> list[tuple[Game, dict]]:
    """Мои партии против него: я чёрными, зевок на своём первом ходу (полуход 1)."""
    pairs = []
    for index in range(count):
        game = _vs_opponent(
            mkgame(f"{prefix}{index}", "black", "loss", opening="Sicilian Defense"), opponent
        )
        pairs.append((game, mkanalysis(game, errors={1: "blunder"})))
    return pairs


def my_own_pairs(count: int = 2) -> list[tuple[Game, dict]]:
    """Мои партии с другими людьми — в набор попадать не должны."""
    pairs = []
    for index in range(count):
        game = _vs_opponent(
            mkgame(f"other{index}", "black", "win", opening="Caro-Kann Defense"),
            f"Someone{index}",
        )
        pairs.append((game, mkanalysis(game, errors={1: "blunder"})))
    return pairs


def his_pairs(count: int = 3) -> list[tuple[Game, dict]]:
    """Его собственные партии (в кеше лежат под его ником)."""
    pairs = []
    for index in range(count):
        game = mkgame(f"his{index}", "white", "win", opening="French Defense")
        pairs.append((game, mkanalysis(game, errors={0: "blunder"})))
    return pairs


class DrillsOpponentParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def test_defaults_unchanged_without_opponent(self) -> None:
        args = self.cli._make_parser().parse_args(["drills", "--user", "me"])
        self.assertEqual(args.command, "drills")
        self.assertIsNone(args.opponent)
        self.assertEqual(args.focus, "both")
        self.assertEqual(args.min_drop, 15.0)
        self.assertEqual(args.out, None)

    def test_opponent_flags_parse(self) -> None:
        args = self.cli._make_parser().parse_args(
            [
                "drills", "--user", "me", "--opponent", "Rival", "--focus", "his",
                "--min-drop", "10", "--limit", "5", "--out", "d.pgn",
            ]
        )
        self.assertEqual(args.opponent, "Rival")
        self.assertEqual(args.focus, "his")
        self.assertEqual(args.min_drop, 10.0)
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.out, "d.pgn")

    def test_invalid_focus_exits(self) -> None:
        with self.assertRaises(SystemExit):
            self.cli._make_parser().parse_args(
                ["drills", "--opponent", "Rival", "--focus", "everyone"]
            )

    def test_dispatch_reaches_opponent_branch(self) -> None:
        # изоляция от продакшен-базы: resolve_identity открывает Database()
        with Database(Path(tempfile.mkdtemp()) / "dispatch.db") as db:
            db.init_db()
            real_db_cls = self.cli.Database
            self.cli.Database = lambda path=None: db
            try:
                with redirect_stdout(io.StringIO()):
                    # --yes: неинтерактивный прогон, без запросов
                    code = self.cli.main(["--yes", "drills", "--opponent", "Rival"])
            finally:
                self.cli.Database = real_db_cls
        # нет --user: понятная ошибка, а не падение
        self.assertEqual(code, 1)


class DrillsOpponentRunTests(unittest.TestCase):
    """Прогон на временной БД: сеть и движок не должны понадобиться."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "drills.db"

        db = Database(self.db_path)
        db.init_db()
        _save(db, "me", my_pairs_vs("Rival") + my_own_pairs())
        _save(db, "Rival", his_pairs())
        # отдельный игрок: партии с соперником, которого в кеше нет
        _save(db, "solo", my_pairs_vs("Ghost", prefix="solo"))
        db.close()

        real_db_cls = self.cli.Database
        self.cli.Database = lambda path=None: real_db_cls(self.db_path)
        self.addCleanup(setattr, self.cli, "Database", real_db_cls)

    def _run(self, *argv) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli.main(list(argv))
        return code, output.getvalue()

    def test_writes_pgn_and_sheet(self) -> None:
        out = self.tmpdir / "vs.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertIn("Дрели против: Rival", text)
        self.assertIn("Твоих провалов против него: 2 игры, 2 дрели", text)
        self.assertIn("Его ошибок — твои наказания: 3 игры, 3 дрели", text)
        self.assertIn("Дрелей записано: 5", text)
        self.assertTrue(out.exists())
        pgn = out.read_text(encoding="utf-8")
        self.assertEqual(pgn.count("[Event "), 5)

    def test_json_suffix_writes_json(self) -> None:
        out = self.tmpdir / "vs.json"
        code, _ = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--out", str(out)
        )
        self.assertEqual(code, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 5)
        self.assertEqual({item["opponent"] for item in data[:2]}, {"Rival"})

    def test_default_out_name(self) -> None:
        data_dir = self.tmpdir / "data"
        real_settings = self.cli.settings
        self.cli.settings = dataclasses.replace(real_settings, data_dir=data_dir)
        self.addCleanup(setattr, self.cli, "settings", real_settings)
        # артефакты пишутся через app.paths.settings, а не __main__.settings
        real_paths_settings = paths_mod.settings
        paths_mod.settings = dataclasses.replace(
            real_paths_settings, data_dir=data_dir
        )
        self.addCleanup(setattr, paths_mod, "settings", real_paths_settings)
        code, text = self._run("drills", "--user", "me", "--opponent", "Rival")
        self.assertEqual(code, 0)
        self.assertIn("drills_vs_Rival.pgn", text)
        self.assertTrue((data_dir / "me" / "drills_vs_Rival.pgn").exists())

    def test_focus_mine_hides_his_drills(self) -> None:
        out = self.tmpdir / "mine.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--focus", "mine",
            "--out", str(out),
        )
        self.assertEqual(code, 0)
        self.assertIn("Дрелей записано: 2", text)
        self.assertEqual(out.read_text(encoding="utf-8").count("[Event "), 2)

    def test_focus_his_ignores_my_games(self) -> None:
        out = self.tmpdir / "his.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--focus", "his",
            "--out", str(out),
        )
        self.assertEqual(code, 0)
        self.assertIn("Дрелей записано: 3", text)
        self.assertIn("Твоих провалов против него: 0 игр, 0 дрелей", text)

    def test_games_against_other_players_excluded(self) -> None:
        out = self.tmpdir / "vs.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--focus", "mine",
            "--out", str(out),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("Caro-Kann", text)

    def test_limit_caps_total(self) -> None:
        out = self.tmpdir / "lim.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--limit", "2",
            "--out", str(out),
        )
        self.assertEqual(code, 0)
        self.assertIn("Дрелей записано: 2", text)

    def test_min_drop_filters_everything(self) -> None:
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--min-drop", "99"
        )
        self.assertEqual(code, 0)
        self.assertIn("Дрелей не нашлось", text)
        self.assertIn("--min-drop", text)

    def test_missing_opponent_games_suggests_prepare(self) -> None:
        code, text = self._run("drills", "--user", "me", "--opponent", "Stranger")
        self.assertEqual(code, 1)
        self.assertIn("trainer prepare", text)

    def test_focus_mine_without_common_games_hints_coach(self) -> None:
        code, text = self._run("drills", "--user", "me", "--opponent", "Ghost",
                               "--focus", "mine")
        self.assertEqual(code, 0)
        self.assertIn("trainer coach", text)
        self.assertNotIn("trainer prepare", text)

    def test_focus_mine_does_not_need_opponent_cache(self) -> None:
        out = self.tmpdir / "ghost.pgn"
        code, text = self._run(
            "--yes", "drills", "--user", "solo", "--opponent", "Ghost", "--focus", "mine",
            "--out", str(out),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("trainer prepare", text)
        self.assertIn("Твоих провалов против него: 2 игры, 2 дрели", text)
        self.assertIn("Его ошибок — твои наказания: 0 игр, 0 дрелей", text)
        self.assertEqual(out.read_text(encoding="utf-8").count("[Event "), 2)

    def test_missing_my_games_points_to_coach(self) -> None:
        code, text = self._run(
            "--yes", "drills", "--user", "ghost", "--opponent", "Rival"
        )
        self.assertEqual(code, 1)
        self.assertIn("trainer coach", text)

    def test_verdict_without_humanity_file_errors(self) -> None:
        real_path = self.tmpdir / "nope.json"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--verdict", "unnatural",
            "--humanity", str(real_path),
        )
        self.assertEqual(code, 1)
        self.assertIn("humanize", text)

    def test_verdict_filter_applies_and_notes_other_set(self) -> None:
        humanity = self.tmpdir / "humanity.json"
        humanity.write_text(
            json.dumps({"items": [{"game_id": "his0", "ply": 0, "verdict": "unnatural"}]}),
            encoding="utf-8",
        )
        out = self.tmpdir / "verdict.pgn"
        code, text = self._run(
            "drills", "--user", "me", "--opponent", "Rival", "--verdict", "unnatural",
            "--humanity", str(humanity), "--out", str(out),
        )
        self.assertEqual(code, 0)
        # фильтр оставил одну его задачу, твои две прошли без фильтра
        self.assertIn("Дрелей записано: 3", text)
        self.assertIn("1 дреля", text)
        self.assertIn("твои партии", text)
        self.assertEqual(out.read_text(encoding="utf-8").count("[Event "), 3)

    def test_focus_without_opponent_is_rejected(self) -> None:
        code, text = self._run("drills", "--user", "me", "--focus", "his")
        self.assertEqual(code, 1)
        self.assertIn("--focus", text)
        self.assertIn("--opponent", text)

    def test_game_with_opponent_is_rejected(self) -> None:
        code, text = self._run("drills", "--user", "me", "--opponent", "Rival",
                               "--game", "vs0")
        self.assertEqual(code, 1)
        self.assertIn("--game", text)

    def test_plain_drills_path_still_works(self) -> None:
        out = self.tmpdir / "plain.pgn"
        code, text = self._run("drills", "--user", "me", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("Дрелей:", text)
        # без --opponent берутся все мои партии: 4 (2 против Rival + 2 против других)
        self.assertEqual(out.read_text(encoding="utf-8").count("[Event "), 4)


if __name__ == "__main__":
    unittest.main()
