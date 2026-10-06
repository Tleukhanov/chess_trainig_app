"""Тесты подкоманды user и завязки идентичности через main().

Проценки не нужны: ``_cmd_user`` работает по вводу/выводу, а resolve_identity
в ``main()`` в тестах гоняется с ``--yes`` (неинтерактивно). Реальную базу
не трогаем: глобалы загруженного ``__main__.py`` патчатся на временную БД.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.games import Game

_ROOT = Path(__file__).resolve().parent.parent


def _load_cli() -> dict:
    import runpy

    return runpy.run_path(str(_ROOT / "__main__.py"), run_name="trainer_identity_cli_test")


def mkgame(id: str) -> Game:
    return Game(
        id=id,
        white={"name": "A"},
        black={"name": "B"},
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner=None,
        opening="Sicilian Defense",
        eco="B20",
        moves=["e4"],
        clocks=[],
        user_color="black",
        opponent="B",
        user_rating=2100,
        user_rating_diff=0,
        result_for_user="loss",
    )


class UserCliTests(unittest.TestCase):
    """user list/whoami/add/switch через main() на временной БД."""

    def setUp(self) -> None:
        self.cli = _load_cli()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"

        real_db_cls = self.cli["Database"]

        def temp_db(path=None):
            return real_db_cls(self.db_path)

        globals_ = self.cli["main"].__globals__
        patcher = patch.dict(globals_, {"Database": temp_db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *argv) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](list(argv))
        return code, output.getvalue()

    def _seed_user(self, nick: str, prefix: str) -> None:
        """Одна проанализированная партия, чтобы команда вернула код 0."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame(f"{prefix}1")], nick)
            db.save_analysis(f"{prefix}1", {"summary": []}, depth=8)

    # --- list / whoami ---

    def test_list_empty(self) -> None:
        code, text = self._run("user", "list")
        self.assertEqual(code, 0)
        self.assertIn("Профилей пока нет", text)

    def test_whoami_none(self) -> None:
        code, text = self._run("user", "whoami")
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), "никто не закреплён")

    def test_list_marks_current_and_counts(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("ada")
            db.save_games([mkgame("g1"), mkgame("g2")], "ada")
            db.save_analysis("g1", {"summary": []}, depth=8)
            db.set_current_user("bob")
            db.save_games([mkgame("g3")], "bob")
            db.save_analysis("g3", {"summary": []}, depth=8)
        code, text = self._run("user", "list")
        self.assertEqual(code, 0)
        self.assertRegex(text, r".ada.*партий 2.*анализов 1\n")
        self.assertRegex(text, r".bob.*партий 1.*анализов 1 \*")

    # --- add ---

    def test_add_binds_current(self) -> None:
        code, text = self._run("user", "add", "Petya")
        self.assertEqual(code, 0)
        self.assertIn("Закреплён Petya.", text)
        _, whoami = self._run("user", "whoami")
        self.assertEqual(whoami.strip(), "Petya")

    def test_add_with_fide(self) -> None:
        code, text = self._run("user", "add", "Petya", "--fide", "4130005")
        self.assertEqual(code, 0)
        with Database(self.db_path) as db:
            row = [u for u in db.list_users() if u["nick_lower"] == "petya"][0]
        self.assertEqual(row["fide_id"], "4130005")

    def test_add_invalid_nick(self) -> None:
        code, text = self._run("user", "add", "x")
        self.assertEqual(code, 1)
        self.assertIn("невалидный ник", text)

    # --- switch ---

    def test_switch_yes_rebinds_and_persists(self) -> None:
        self._run("user", "add", "ada")
        self._run("user", "add", "bob")
        code, text = self._run("user", "switch", "--yes", "bob")
        self.assertEqual(code, 0)
        self.assertIn("Закреплён bob.", text)
        _, whoami = self._run("user", "whoami")
        self.assertEqual(whoami.strip(), "bob")

    def test_switch_without_nick_picks_first_when_no_one(self) -> None:
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("kate")
        code, text = self._run("user", "switch", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Закреплён kate.", text)

    def test_switch_unknown_invalid(self) -> None:
        code, text = self._run("user", "switch", "--yes", "###")
        self.assertEqual(code, 1)
        self.assertIn("невалидный ник", text)


class IdentityDispatchTests(unittest.TestCase):
    """main() сам завязывает юзера: --user регистрируется канонически."""

    def setUp(self) -> None:
        self.cli = _load_cli()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = self.tmpdir / "trainer.db"

        real_db_cls = self.cli["Database"]

        def temp_db(path=None):
            return real_db_cls(self.db_path)

        globals_ = self.cli["main"].__globals__
        patcher = patch.dict(globals_, {"Database": temp_db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_user(self, nick: str, prefix: str) -> None:
        """Одна проанализированная партия, чтобы команда вернула код 0."""
        with Database(self.db_path) as db:
            db.init_db()
            db.save_games([mkgame(f"{prefix}1")], nick)
            db.save_analysis(f"{prefix}1", {"summary": []}, depth=8)

    def test_user_registered_after_dispatch(self) -> None:
        self._seed_user("Petya", "p")
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](["--yes", "overview", "--user", "Petya"])
        self.assertEqual(code, 0)
        with Database(self.db_path) as db:
            names = {u["nick_lower"] for u in db.list_users()}
            current = db.get_current_user()
        self.assertIn("petya", names)
        self.assertEqual(current["nick"], "Petya")

    def test_dispatch_without_user_errors_noninteractive(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](["--yes", "progress"])
        self.assertEqual(code, 1)
        self.assertIn("укажи --user или user add", output.getvalue())

    def test_invalid_user_nick_rejected(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](["--yes", "progress", "--user", "x"])
        self.assertEqual(code, 1)
        self.assertIn("невалидный ник", output.getvalue())

    def test_conflicting_user_switches_current_with_yes(self) -> None:
        self._seed_user("bob", "b")
        with Database(self.db_path) as db:
            db.init_db()
            db.set_current_user("ada")
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli["main"](["--yes", "overview", "--user", "bob"])
        self.assertEqual(code, 0)
        with Database(self.db_path) as db:
            self.assertEqual(db.get_current_user()["nick"], "bob")


if __name__ == "__main__":
    unittest.main()