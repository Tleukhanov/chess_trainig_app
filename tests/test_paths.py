"""Персональные пути артефактов: своя папка пишется, чужая/общая читается."""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from app import paths
from app.config import settings


class PathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.old_settings = paths.settings
        paths.settings = dataclasses.replace(settings, data_dir=self.tmpdir / "data")
        self.addCleanup(setattr, paths, "settings", self.old_settings)

    def _touch(self, rel: str) -> Path:
        path = self.tmpdir / "data" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        return path

    def test_artifact_writes_to_own_folder(self) -> None:
        own = paths.artifact("humanity.json", "Tleukhanov")
        self.assertEqual(own, self.tmpdir / "data" / "tleukhanov" / "humanity.json")
        self.assertTrue(own.parent.exists())

    def test_two_users_do_not_clash(self) -> None:
        a = paths.artifact("humanity.json", "Alice")
        b = paths.artifact("humanity.json", "alice")
        self.assertEqual(a, b)
        other = paths.artifact("humanity.json", "bob")
        self.assertNotEqual(a, other)

    def test_artifact_read_prefers_own(self) -> None:
        legacy = self._touch("humanity.json")
        own = paths.artifact("humanity.json", "tleukhanov")
        own.write_text("own", encoding="utf-8")
        self.assertEqual(paths.artifact_read("humanity.json", "tleukhanov"), own)
        self.assertNotEqual(paths.artifact_read("humanity.json", "tleukhanov"), legacy)

    def test_artifact_read_falls_back_to_legacy(self) -> None:
        legacy = self._touch("humanity.json")
        self.assertEqual(paths.artifact_read("humanity.json", "tleukhanov"), legacy)

    def test_artifact_read_none_when_missing(self) -> None:
        self.assertIsNone(paths.artifact_read("progress.json", "tleukhanov"))
        self.assertIsNone(paths.artifact_read("progress.json", None))

    def test_artifact_read_without_user_uses_legacy(self) -> None:
        legacy = self._touch("fide.json")
        self.assertEqual(paths.artifact_read("fide.json", None), legacy)

    def test_drills_dir_creates_folder(self) -> None:
        d = paths.drills_dir("Bob")
        self.assertEqual(d, self.tmpdir / "data" / "bob")
        self.assertTrue(d.exists())


if __name__ == "__main__":
    unittest.main()