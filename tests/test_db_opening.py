"""Сессии «Тренера дебютов»: таблица opening_sessions и аксессоры Database."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database


class OpeningSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "opening.db")
        self.db.init_db()
        self.addCleanup(self.db.close)

    def test_create_get_roundtrip(self) -> None:
        self.db.save_opening_session(
            "s1",
            "Alice",
            my_elo=1500,
            opp_elo=1450,
            style="aggressive",
            color="white",
            moves=["e4", "c5", "Nf3"],
            status="in_progress",
        )
        row = self.db.get_opening_session("s1")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "s1")
        self.assertEqual(row["player"], "Alice")
        self.assertEqual(row["my_elo"], 1500)
        self.assertEqual(row["opp_elo"], 1450)
        self.assertEqual(row["style"], "aggressive")
        self.assertEqual(row["color"], "white")
        self.assertEqual(row["moves"], ["e4", "c5", "Nf3"])
        self.assertEqual(row["status"], "in_progress")
        self.assertIsNone(row["result"])
        self.assertIsNone(row["report"])
        self.assertIsInstance(row["created_at"], int)
        self.assertIsInstance(row["updated_at"], int)

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.db.get_opening_session("nope"))

    def test_upsert_updates_existing(self) -> None:
        self.db.save_opening_session("s1", "Alice", status="in_progress")
        self.db.save_opening_session(
            "s1",
            "Alice",
            status="done",
            result="win",
            moves=["e4", "e5"],
            report={"score": 0.7},
        )
        row = self.db.get_opening_session("s1")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], "win")
        self.assertEqual(row["moves"], ["e4", "e5"])
        self.assertEqual(row["report"], {"score": 0.7})
        with self.db._connection() as conn:
            count = conn.execute("SELECT count(*) FROM opening_sessions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_list_ordering_and_limit(self) -> None:
        for sid in ("s1", "s2", "s3"):
            self.db.save_opening_session(sid, "Alice")
        self.db.save_opening_session("other", "Bob")
        listed = self.db.get_opening_sessions("Alice")
        self.assertEqual([s["id"] for s in listed], ["s3", "s2", "s1"])
        self.assertEqual([s["id"] for s in self.db.get_opening_sessions("Alice", limit=2)],
                         ["s3", "s2"])
        self.assertEqual(self.db.get_opening_sessions("bob")[0]["id"], "other")

    def test_update_only_moves_keeps_report(self) -> None:
        self.db.save_opening_session(
            "s1", "Alice", moves=["e4"], status="in_progress", report={"a": 1}
        )
        with self.db._connection() as conn:
            conn.execute(
                "UPDATE opening_sessions SET created_at = 1, updated_at = 1 WHERE id = 's1'"
            )
        self.db.update_opening_session("s1", moves=["e4", "e5", "Nf3"])
        row = self.db.get_opening_session("s1")
        self.assertEqual(row["moves"], ["e4", "e5", "Nf3"])
        self.assertEqual(row["report"], {"a": 1})
        self.assertEqual(row["status"], "in_progress")
        self.assertGreater(row["updated_at"], 1)

    def test_update_only_report_keeps_moves(self) -> None:
        self.db.save_opening_session("s1", "Alice", moves=["e4", "e5"])
        self.db.update_opening_session("s1", report={"blunders": 2})
        row = self.db.get_opening_session("s1")
        self.assertEqual(row["moves"], ["e4", "e5"])
        self.assertEqual(row["report"], {"blunders": 2})
        self.assertIsNone(row["status"])

    def test_update_status_and_result(self) -> None:
        self.db.save_opening_session("s1", "Alice", status="in_progress")
        self.db.update_opening_session("s1", status="done", result="loss")
        row = self.db.get_opening_session("s1")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], "loss")

    def test_init_db_is_idempotent(self) -> None:
        self.db.save_opening_session("s1", "Alice", moves=["e4"])
        self.db.init_db()
        self.db.init_db()
        with self.db._connection() as conn:
            count = conn.execute("SELECT count(*) FROM opening_sessions").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.db.get_opening_session("s1")["moves"], ["e4"])


if __name__ == "__main__":
    unittest.main()
