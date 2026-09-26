"""Тесты CLI подкоманды prepare (M7): разбор аргументов, изоляция данных, вывод.

Проверяем главное: партии соперника попадают в кеш под его ником и не
подмешиваются в твои выборки, а лист строится только по твоему разбору.
"""

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

from app.db import Database
from app.games import Game
from tests.test_opponent import mkanalysis, mkgame

_ROOT = Path(__file__).resolve().parent.parent


def _load_cli():
    """Импортирует __main__.py как обычный модуль, чтобы патчить его глобалы."""
    spec = importlib.util.spec_from_file_location("trainer_cli", _ROOT / "__main__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["trainer_cli"] = module
    spec.loader.exec_module(module)
    return module


def _save(db: Database, user: str, pairs: list[tuple[Game, dict]]) -> None:
    """Кладёт партии и анализы в кеш от имени ``user``."""
    db.save_games([game for game, _ in pairs], user)
    for game, analysis in pairs:
        db.save_analysis(game.id, analysis, depth=8)


def my_pairs() -> list[tuple[Game, dict]]:
    """Мои партии чёрными в Сицилианской: чёрные отвечают c5 на e4."""
    pairs = []
    for index in range(3):
        game = mkgame(f"my{index}", "black", "win", opening="Sicilian Defense")
        pairs.append((game, mkanalysis(game)))
    return pairs


def his_pairs(count: int = 4) -> list[tuple[Game, dict]]:
    """Его партии белыми в Сицилианской — то самое пересечение."""
    pairs = []
    for index in range(count):
        game = mkgame(f"his{index}", "white", "win", opening="Sicilian Defense")
        pairs.append((game, mkanalysis(game, errors={0: "mistake"})))
    return pairs


class FakeEngine:
    """Заглушка Stockfish: отдаёт готовый summary, чтобы тесты шли без движка."""

    calls: list[str] = []

    def __init__(self, depth: int = 0, multipv: int = 1, **kwargs) -> None:
        self.depth = depth
        self.multipv = multipv

    def __enter__(self) -> "FakeEngine":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def analyze_game(self, game: Game):
        FakeEngine.calls.append(game.id)
        return _FakeAnalysis(game)


class _FakeAnalysis:
    def __init__(self, game: Game) -> None:
        self._game = game

    def summary(self) -> dict:
        return mkanalysis(self._game, errors={1: "mistake"})


class PrepareCliParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def test_defaults(self) -> None:
        parser = self.cli._make_parser()
        args = parser.parse_args(["prepare", "--user", "me", "--opponent", "rival"])
        self.assertEqual(args.command, "prepare")
        self.assertEqual(args.user, "me")
        self.assertEqual(args.opponent, "rival")
        self.assertEqual(args.max, 30)
        self.assertEqual(args.perf, "rapid")
        self.assertEqual(args.color, "both")
        self.assertEqual(args.top, 8)
        self.assertFalse(args.cached_only)
        self.assertFalse(args.refresh)
        self.assertFalse(args.no_save)
        self.assertIsNone(args.json)

    def test_flags_parse(self) -> None:
        parser = self.cli._make_parser()
        args = parser.parse_args(
            [
                "prepare", "--user", "me", "--opponent", "rival", "--max", "12",
                "--color", "white", "--top", "3", "--depth", "18", "--json", "p.json",
                "--cached-only", "--no-save",
            ]
        )
        self.assertEqual(args.max, 12)
        self.assertEqual(args.color, "white")
        self.assertEqual(args.top, 3)
        self.assertEqual(args.depth, 18)
        self.assertEqual(args.json, "p.json")
        self.assertTrue(args.cached_only)
        self.assertTrue(args.no_save)

    def test_invalid_color_exits(self) -> None:
        parser = self.cli._make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["prepare", "--opponent", "rival", "--color", "purple"])


class PrepareCliRunTests(unittest.TestCase):
    """Прогон _cmd_prepare на временной БД с заглушками вместо сети и движка."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def setUp(self) -> None:
        FakeEngine.calls = []
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "prepare.db"

        db = Database(self.db_path)
        db.init_db()
        _save(db, "me", my_pairs())
        _save(db, "rival", his_pairs())
        db.close()

        # Подменяем путь БД, движок и сеть в глобалах загруженного __main__.py.
        real_db_cls = self.cli.Database

        def temp_db(path=None) -> Database:
            return real_db_cls(self.db_path)

        self.cli.Database = temp_db
        self.addCleanup(setattr, self.cli, "Database", real_db_cls)
        real_engine = self.cli.Engine
        self.cli.Engine = FakeEngine
        self.addCleanup(setattr, self.cli, "Engine", real_engine)

        self.fetch_calls: list[tuple] = []
        real_fetch = self.cli.fetch_user_games
        self.cli.fetch_user_games = self._fake_fetch
        self.addCleanup(setattr, self.cli, "fetch_user_games", real_fetch)

    def _fake_fetch(self, username, since_ts=None, max_games=50, perf="rapid"):
        self.fetch_calls.append((username, max_games, perf, since_ts))
        return [game for game, _ in his_pairs(2)]

    def _run(self, *argv) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli._cmd_prepare(self.cli._make_parser().parse_args(argv))
        return code, output.getvalue()

    # --- разбор аргументов на уровне команды ---

    def test_missing_opponent_explains(self) -> None:
        code, text = self._run("prepare", "--user", "me")
        self.assertEqual(code, 1)
        self.assertIn("--opponent", text)

    def test_no_own_analyses_points_to_coach(self) -> None:
        code, text = self._run("prepare", "--opponent", "rival", "--cached-only")
        self.assertEqual(code, 1)
        self.assertIn("python -m trainer coach", text)

    def test_dispatch_reaches_prepare(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli.main(
                ["prepare", "--user", "me", "--opponent", "rival", "--cached-only"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Подготовка к сопернику: rival", output.getvalue())

    # --- основной сценарий из кеша ---

    def test_cached_only_prints_profile_and_confrontation(self) -> None:
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only"
        )
        self.assertEqual(code, 0)
        self.assertIn("Режим --cached-only", text)
        self.assertIn("уже проанализированы", text)
        self.assertIn("Подготовка к сопернику: rival", text)
        self.assertIn("ты — me", text)
        self.assertIn("Sicilian Defense", text)
        self.assertIn("Точки встречи", text)
        self.assertIn("твой ответ", text)
        # анализ в кеше есть, движок запускаться не должен
        self.assertEqual(FakeEngine.calls, [])

    def test_color_filter_limits_profile(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmpdir, True)
        json_path = tmpdir / "prep.json"
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only",
            "--color", "black", "--json", str(json_path),
        )
        self.assertEqual(code, 0)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["profile"]["games"], 0)
        self.assertNotIn("Точки встречи", text)

    def test_json_contains_profile_and_confrontations(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmpdir, True)
        json_path = tmpdir / "prep.json"
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only",
            "--json", str(json_path),
        )
        self.assertEqual(code, 0)
        self.assertIn("JSON", text)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["profile"]["opponent"], "rival")
        self.assertEqual(data["profile"]["games"], 4)
        self.assertNotIn("progress", data["profile"])
        self.assertTrue(data["confrontations"])
        first = data["confrontations"][0]
        self.assertEqual(first["opening"], "Sicilian Defense")
        self.assertEqual(first["his_color"], "white")
        self.assertEqual(first["your_color"], "black")

    # --- сеть: чужие партии сохраняются под ником соперника ---

    def test_fetch_uses_opponent_nickname(self) -> None:
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--max", "7",
            "--perf", "blitz",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.fetch_calls, [("rival", 7, "blitz", None)])
        self.assertIn("новых в кеше", text)

    def test_no_save_does_not_write_games(self) -> None:
        db = Database(self.db_path)
        db.init_db()
        db.save_games([mkgame("fresh0", "white", "win")], "rival")
        db.close()

        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "fresh", "--no-save"
        )
        self.assertEqual(code, 0)
        self.assertIn("--no-save", text)
        with Database(self.db_path) as db:
            self.assertEqual(db.get_games("fresh"), [])

    # --- изоляция данных: главное свойство M7 ---

    def test_opponent_games_stay_in_own_namespace(self) -> None:
        code, _ = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only"
        )
        self.assertEqual(code, 0)
        with Database(self.db_path) as db:
            mine = db.get_analyzed_games("me", limit=100)
            theirs = db.get_analyzed_games("rival", limit=100)
        self.assertEqual([game.id for game, _ in mine], ["my0", "my1", "my2"])
        self.assertEqual([game.id for game, _ in theirs], ["his0", "his1", "his2", "his3"])

    def test_unanalysed_opponent_games_go_through_engine(self) -> None:
        with Database(self.db_path) as db:
            db.save_games(
                [mkgame("new0", "white", "win"), mkgame("new1", "white", "win")],
                "rival",
            )
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only"
        )
        self.assertEqual(code, 0)
        self.assertIn("К анализу 2 партий", text)
        self.assertIn("Оценка времени", text)
        self.assertEqual(FakeEngine.calls, ["new0", "new1"])
        with Database(self.db_path) as db:
            self.assertEqual(len(db.get_analyzed_games("rival", limit=100)), 6)
            # твоя выборка не изменилась
            self.assertEqual(len(db.get_analyzed_games("me", limit=100)), 3)

    def test_refresh_reanalyses_everything(self) -> None:
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only",
            "--refresh",
        )
        self.assertEqual(code, 0)
        self.assertIn("--refresh", text)
        self.assertEqual(
            FakeEngine.calls, ["his0", "his1", "his2", "his3"]
        )

    def test_aborted_games_skipped(self) -> None:
        aborted = dataclasses.replace(
            mkgame("hisAborted", "white", "win"), status="aborted"
        )
        analysis = mkanalysis(aborted)
        with Database(self.db_path) as db:
            db.save_games([aborted], "rival")
            db.save_analysis(aborted.id, analysis, depth=8)
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "rival", "--cached-only"
        )
        self.assertEqual(code, 0)
        self.assertIn("Пропущено", text)

    def test_opponent_without_games_returns_one(self) -> None:
        code, text = self._run(
            "prepare", "--user", "me", "--opponent", "nobody", "--cached-only"
        )
        self.assertEqual(code, 1)
        self.assertIn("готовиться не к чему", text)


if __name__ == "__main__":
    unittest.main()
