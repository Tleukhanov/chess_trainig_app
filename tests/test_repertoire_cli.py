"""Тесты CLI подкоманды repertoire (M4): парсер, отбор партий, вывод и запись."""

from __future__ import annotations

import io
import json
import runpy
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app.db import Database
from app.games import Game

_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_DROP = {"blunder": 20.0, "mistake": 12.0, "inaccuracy": 6.0}


def _load_cli() -> dict:
    """Загружает __main__.py без исполнения диспетчера (как python -m trainer)."""
    return runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_cli_test")


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
    return {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": win_before,
        "win_after": win_before - drop,
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
    """Анализ: spec — список (san) или (san, classification) или (san, cls, drop)."""
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


def fixture_pairs() -> list[tuple[Game, dict]]:
    """Две синтетические белые партии с разными дебютами."""
    sicilian = mkgame(
        "a", ["e4", "e5", "Nf3", "Nc6"], "white", "win",
        opening="Sicilian Defense", eco="B20",
    )
    a1 = mkanalysis(
        "white", 4, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake"), ("Nc6", "best")]
    )
    dutch = mkgame(
        "b", ["e4", "e5", "Bc4", "Nc6"], "white", "win",
        opening="Dutch Defense", eco="A80",
    )
    a2 = mkanalysis(
        "white", 4, [("e4", "good"), ("e5", "best"), ("Bc4", "good"), ("Nc6", "best")]
    )
    return [(sicilian, a1), (dutch, a2)]


def _seed_db(path: Path) -> None:
    """Создаёт временную БД с партиями 'u' и анализами к ним."""
    db = Database(path)
    db.init_db()
    games = [game for game, _ in fixture_pairs()]
    db.save_games(games, "u")
    for game, analysis in fixture_pairs():
        db.save_analysis(game.id, analysis, depth=8)
    db.close()


class CliParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def test_repertoire_args_parse(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(["repertoire", "--user", "X"])
        self.assertEqual(args.command, "repertoire")
        self.assertEqual(args.user, "X")
        self.assertIsNone(args.game)
        self.assertEqual(args.color, "both")
        self.assertEqual(args.max_depth, 16)
        self.assertEqual(args.min_count, 1)
        self.assertIsNone(args.opening)
        self.assertIsNone(args.pgn)
        self.assertIsNone(args.json)
        self.assertFalse(args.no_write)

    def test_invalid_color_raises_system_exit(self) -> None:
        parser = self.cli["_make_parser"]()
        with self.assertRaises(SystemExit):
            parser.parse_args(["repertoire", "--color", "purple"])

    def test_opening_filter_and_no_write_parse(self) -> None:
        parser = self.cli["_make_parser"]()
        args = parser.parse_args(
            ["repertoire", "--user", "X", "--opening", "sicilian", "dutch", "--no-write"]
        )
        self.assertEqual(args.opening, ["sicilian", "dutch"])
        self.assertTrue(args.no_write)


class RepertoirePairsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "test.db"
        _seed_db(self.db_path)

    def _args(self, **overrides):
        args = self.cli["_make_parser"]().parse_args(["repertoire", "--user", "u"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_game_id_returns_single_pair(self) -> None:
        with Database(self.db_path) as db:
            pairs = self.cli["_repertoire_pairs"](db, self._args(game="a", user=None))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0].id, "a")

    def test_missing_game_raises(self) -> None:
        with Database(self.db_path) as db:
            with self.assertRaises(RuntimeError) as ctx:
                self.cli["_repertoire_pairs"](db, self._args(game="NaN", user=None))
        self.assertIn("не найдена", str(ctx.exception))

    def test_user_returns_all_pairs(self) -> None:
        with Database(self.db_path) as db:
            pairs = self.cli["_repertoire_pairs"](db, self._args(user="u"))
        self.assertEqual(len(pairs), 2)

    def test_user_without_analyses_raises(self) -> None:
        with Database(self.db_path) as db:
            with self.assertRaises(RuntimeError) as ctx:
                self.cli["_repertoire_pairs"](db, self._args(user="nobody"))
        self.assertIn("Нет проанализированных партий", str(ctx.exception))

    def test_neither_user_nor_game_raises(self) -> None:
        with Database(self.db_path) as db:
            with self.assertRaises(RuntimeError) as ctx:
                self.cli["_repertoire_pairs"](db, self._args(user=None, game=None))
        self.assertIn("Укажи --user или --game", str(ctx.exception))


class RunRepertoireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def _args(self, **overrides):
        args = self.cli["_make_parser"]().parse_args(
            ["repertoire", "--user", "u", "--no-write"]
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_counts_and_stdout(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.cli["_run_repertoire"](fixture_pairs(), self._args())
        text = output.getvalue()
        self.assertGreaterEqual(result["white"], 2)
        self.assertEqual(result["black"], 0)
        self.assertEqual(result["openings"], 2)
        self.assertIn("Репертуар белых", text)
        self.assertIn("Слабые места репертуара", text)
        self.assertIn("Слабых линий: 1", text)

    def test_opening_filter_filters_stats(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.cli["_run_repertoire"](
                fixture_pairs(), self._args(opening=["sicilian"])
            )
        self.assertEqual(result["openings"], 1)
        self.assertNotIn("Dutch Defense", output.getvalue())

    def test_color_white_only_ignores_black(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.cli["_run_repertoire"](
                fixture_pairs(), self._args(color="white")
            )
        self.assertGreaterEqual(result["white"], 2)
        self.assertEqual(result["black"], 0)
        self.assertNotIn("Репертуар чёрных", output.getvalue())

    def test_writes_pgn_and_json(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmpdir, True)
        pgn_path = tmpdir / "repertoire.pgn"
        json_path = tmpdir / "repertoire.json"
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.cli["_run_repertoire"](
                fixture_pairs(),
                self._args(no_write=False, pgn=str(pgn_path), json=str(json_path)),
            )
        self.assertEqual(result["openings"], 2)
        self.assertIn("Репертуар записан", output.getvalue())
        self.assertIn("JSON →", output.getvalue())
        self.assertTrue(pgn_path.exists())
        self.assertGreater(len(pgn_path.read_text(encoding="utf-8")), 0)
        self.assertTrue(json_path.exists())
        self.assertGreater(len(json_path.read_text(encoding="utf-8")), 0)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertIn("white_lines", data)
        self.assertIn("openings", data)
        self.assertEqual(len(data["openings"]), 2)


if __name__ == "__main__":
    unittest.main()