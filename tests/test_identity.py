"""Тесты M9: FSM идентификации и resolve_identity.

Проверяем чистое поведение автомата (переходы, валидация), оркестрацию
``run`` со скриптованными ответами и логику ``resolve_identity`` — включая
клятвенный пункт «не-TTY не вызывает ask».
"""

from __future__ import annotations

import tempfile
import unittest

from app.db import Database
from app.identity import (
    ABORT,
    CHOOSE_USER,
    CONFIRM_NICK,
    CONFIRM_SWITCH,
    DONE,
    E_INVALID,
    E_NICK_OK,
    E_YES,
    ENTER_NICK,
    handle,
    resolve_identity,
    run,
    validate_nick,
)


def _answers(values: list[str]) -> dict[str, int]:
    box = {"i": 0}

    def ask(_question: str) -> str:
        value = values[box["i"]]
        box["i"] += 1
        return value

    return {"values": values, "box": box, "ask": ask}


class ValidateNickTests(unittest.TestCase):
    def test_valid(self) -> None:
        for nick in ["tleukhanov", "Tleukhanov_1", "a-bored", "XxX", "1_2-3"]:
            with self.subTest(nick=nick):
                self.assertEqual(validate_nick(nick), nick)

    def test_invalid(self) -> None:
        for nick in ["", "  ", "a", "x" * 31, "кирилл", "two words", "a@b"]:
            with self.subTest(nick=nick):
                self.assertIsNone(validate_nick(nick))


class FsmTests(unittest.TestCase):
    def test_transition_table_shape(self) -> None:
        # новые незнакомые пары (state, event) не уводят из состояния
        state, action = handle(CHOOSE_USER, "unknown_event")
        self.assertEqual(state, CHOOSE_USER)
        self.assertIsNone(action)

    def test_nick_flow(self) -> None:
        self.assertEqual(handle(ENTER_NICK, E_NICK_OK)[0], CONFIRM_NICK)
        self.assertEqual(handle(ENTER_NICK, E_INVALID)[0], ENTER_NICK)
        self.assertEqual(handle(CONFIRM_NICK, E_YES)[0], DONE)
        self.assertEqual(handle(CONFIRM_NICK, "no")[0], ABORT)

    def test_switch_flow(self) -> None:
        self.assertEqual(handle(CONFIRM_SWITCH, E_YES)[0], DONE)
        self.assertEqual(handle(CONFIRM_SWITCH, "no")[0], ABORT)

    def test_terminal_states_are_stuck(self) -> None:
        for terminal in (DONE, ABORT):
            with self.subTest(state=terminal):
                state, _ = handle(terminal, "any_event")
                self.assertEqual(state, terminal)


class RunTests(unittest.TestCase):
    def _db(self) -> Database:
        path = tempfile.mktemp(suffix=".db")
        db = Database(path)
        db.init_db()
        return db

    def test_empty_db_binds_new_user(self) -> None:
        db = self._db()
        answers = _answers(["tleukhanov", "д"])
        picked = run(db, ask=answers["ask"])
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick, "tleukhanov")
        self.assertEqual(picked.action, "bind")

    def test_empty_db_refusal_binds_nothing(self) -> None:
        db = self._db()
        answers = _answers(["tleukhanov", "н"])
        picked = run(db, ask=answers["ask"])
        self.assertIsNone(picked)
        self.assertEqual(db.list_users(), [])

    def test_invalid_nick_stays_in_enter(self) -> None:
        db = self._db()
        answers = _answers(["кирилл", "tleukhanov", "д"])
        picked = run(db, ask=answers["ask"])
        self.assertEqual(answers["box"]["i"], 3)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick_lower, "tleukhanov")

    def test_existing_users_list_pick_second(self) -> None:
        db = self._db()
        # upsert не делает current — список выбора работает при отсутствии
        # закреплённого
        db.upsert_user("alice")
        db.upsert_user("bob")
        answers = _answers(["2"])
        picked = run(db, ask=answers["ask"])
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick, "bob")
        self.assertEqual(picked.action, "switch")

    def test_existing_users_empty_enter_takes_first(self) -> None:
        db = self._db()
        db.upsert_user("alice")
        db.upsert_user("bob")
        answers = _answers([""])
        picked = run(db, ask=answers["ask"])
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick, "alice")

    def test_requested_conflict_needs_confirm(self) -> None:
        db = self._db()
        db.set_current_user("alice")
        answers = _answers(["д"])
        picked = run(db, ask=answers["ask"], requested="bob")
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick, "bob")
        self.assertEqual(picked.action, "switch")

    def test_requested_conflict_refused(self) -> None:
        db = self._db()
        db.set_current_user("alice")
        answers = _answers(["н"])
        picked = run(db, ask=answers["ask"], requested="bob")
        self.assertIsNone(picked)

    def test_yes_skips_confirmation(self) -> None:
        db = self._db()
        db.set_current_user("alice")
        picked = run(db, ask=lambda _q: _raise("prompt must not be called"), requested="bob", yes=True)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.nick, "bob")

    def test_empty_db_with_yes_returns_none(self) -> None:
        db = self._db()
        picked = run(db, ask=lambda _q: _raise("prompt must not be called"), yes=True)
        self.assertIsNone(picked)


def _raise(message: str) -> str:
    raise AssertionError(message)


class ResolveIdentityTests(unittest.TestCase):
    def test_no_tty_no_user_uses_current(self) -> None:
        db = temp_db()
        db.set_current_user("alice")
        nick, err = resolve_identity(
            db, None, isatty=False, ask=lambda _q: _raise("must not ask"),
            notify=lambda _t: None,
        )
        self.assertIsNone(err)
        self.assertEqual(nick, "alice")

    def test_no_tty_user_conflict_works_and_notifies(self) -> None:
        db = temp_db()
        db.set_current_user("alice")
        notes: list[str] = []
        nick, err = resolve_identity(
            db, "Bob", isatty=False, ask=lambda _q: _raise("must not ask"),
            notify=notes.append,
        )
        self.assertIsNone(err)
        self.assertEqual(nick, "Bob")
        self.assertTrue(notes)

    def test_tty_user_conflict_asks(self) -> None:
        db = temp_db()
        db.set_current_user("alice")
        asked: list[str] = []

        def ask(q: str) -> str:
            asked.append(q)
            return ""

        nick, err = resolve_identity(db, "Bob", isatty=True, ask=ask, notify=lambda _t: None)
        self.assertIsNone(err)
        self.assertEqual(nick, "Bob")
        self.assertTrue(asked)

    def test_tty_no_user_asks_who(self) -> None:
        db = temp_db()
        db.set_current_user("alice")
        asked: list[str] = []

        def ask(q: str) -> str:
            asked.append(q)
            return ""

        nick, err = resolve_identity(db, None, isatty=True, ask=ask, notify=lambda _t: None)
        self.assertIsNone(err)
        self.assertEqual(nick, "alice")
        self.assertTrue(asked)

    def test_no_tty_no_users_errors(self) -> None:
        db = temp_db()
        nick, err = resolve_identity(
            db, None, isatty=False, ask=lambda _q: _raise("must not ask"),
            notify=lambda _t: None,
        )
        self.assertIsNone(nick)
        self.assertIn("--user", err or "")

    def test_invalid_nick_errors(self) -> None:
        db = temp_db()
        nick, err = resolve_identity(
            db, "кирилл", isatty=True, ask=lambda _q: "", notify=lambda _t: None,
        )
        self.assertIsNone(nick)
        self.assertIn("невалидный ник", err or "")

    def test_tty_no_users_fsm_flow(self) -> None:
        db = temp_db()
        answers = _answers(["tleukhanov", "д"])
        nick, err = resolve_identity(
            db, None, isatty=True, ask=answers["ask"], notify=lambda _t: None,
        )
        self.assertIsNone(err)
        self.assertEqual(nick, "tleukhanov")

    def test_tty_users_empty_select_first(self) -> None:
        db = temp_db()
        db.upsert_user("alice")
        answers = _answers([""])
        nick, err = resolve_identity(
            db, None, isatty=True, ask=answers["ask"], notify=lambda _t: None,
        )
        self.assertIsNone(err)
        self.assertEqual(nick, "alice")


def temp_db() -> Database:
    path = tempfile.mktemp(suffix=".db")
    db = Database(path)
    db.init_db()
    return db


if __name__ == "__main__":
    unittest.main()