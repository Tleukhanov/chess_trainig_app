"""Тесты окна для недельного источника дрелей (audit M5).

``recent_drills_unnatural`` привязывает humanity.json items к окну
последних N дней по ``created_at`` проанализированных партий — не к
all-time счётчикам.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from app.data import recent_drills_unnatural
from app.db import Database
from app.games import Game

_NOW_MS = int(time.time() * 1000)
_DAY_MS = 86_400_000


def _game(game_id: str, created_at: int) -> Game:
    return Game(
        id=game_id,
        white={"name": "A"},
        black={"name": "B"},
        rated=True,
        speed="rapid",
        created_at=created_at,
        status="win",
        winner=None,
        opening="Ruy Lopez",
        eco="C70",
        moves=["e4", "e5"],
        clocks=[],
        user_color="white",
        opponent="B",
        user_rating=2100,
        user_rating_diff=0,
        result_for_user="loss",
    )


def _analysis() -> dict:
    return {
        "game_id": "g",
        "user_color": "white",
        "result_for_user": "loss",
        "opponent": "B",
        "avg_win_loss": 20.0,
        "avg_win_before": 55.0,
        "blunders": [0],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": [
            {"san": "e4", "classification": "blunder", "drop": 20.0,
             "win_before": 60.0, "win_after": 40.0},
            {"san": "e5", "classification": "best", "drop": 0.0,
             "win_before": 40.0, "win_after": 40.0},
        ],
    }


class RecentDrillsUnnaturalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "trainer.db")
        self.db.init_db()
        self.data_root = self.tmpdir / "data"

    def _save_game(self, game_id: str, created_at: int) -> None:
        with Database(self.tmpdir / "trainer.db") as db:
            db.save_games([_game(game_id, created_at)], "bob")
            db.save_analysis(game_id, {**_analysis(), "game_id": game_id}, depth=8)

    def _write_humanity(self, items: list[dict]) -> None:
        path = self.data_root / "bob" / "humanity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "total_bad": len(items),
                    "verdicts": {
                        "unnatural": sum(
                            1 for i in items if i.get("verdict") == "unnatural"
                        ),
                    },
                    "items": items,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_counts_only_items_from_recent_window(self) -> None:
        self._save_game("recent", _NOW_MS - 2 * _DAY_MS)
        self._save_game("old", _NOW_MS - 60 * _DAY_MS)
        self._write_humanity(
            [
                {"game_id": "recent", "verdict": "unnatural", "drop": 20.0},
                {"game_id": "recent", "verdict": "natural", "drop": 12.0},
                {"game_id": "old", "verdict": "unnatural", "drop": 25.0},
                {"game_id": "old", "verdict": "unnatural", "drop": 30.0},
            ]
        )
        result = recent_drills_unnatural(
            self.db, "bob", days=14, root=self.data_root, now_ms=_NOW_MS
        )
        self.assertEqual(result["source"], "humanity")
        self.assertEqual(result["games"], 1)
        self.assertEqual(result["unnatural"], 1)
        self.assertEqual(result["total_bad"], 2)

    def test_zero_unnatural_is_honest_zero(self) -> None:
        self._save_game("recent", _NOW_MS - _DAY_MS)
        self._write_humanity(
            [{"game_id": "recent", "verdict": "natural", "drop": 12.0}]
        )
        result = recent_drills_unnatural(
            self.db, "bob", days=14, root=self.data_root, now_ms=_NOW_MS
        )
        self.assertEqual(result["unnatural"], 0)
        self.assertEqual(result["total_bad"], 1)

    def test_no_humanity_file_is_honest_zero_not_alltime(self) -> None:
        self._save_game("recent", _NOW_MS - _DAY_MS)
        result = recent_drills_unnatural(
            self.db, "bob", days=14, root=self.data_root, now_ms=_NOW_MS
        )
        self.assertEqual(result["unnatural"], 0)
        self.assertEqual(result["source"], "no-humanity-items")

    def test_no_user_is_honest_zero(self) -> None:
        result = recent_drills_unnatural(
            self.db, None, days=14, root=self.data_root, now_ms=_NOW_MS
        )
        self.assertEqual(result["source"], "no-user")
        self.assertEqual(result["unnatural"], 0)


if __name__ == "__main__":
    unittest.main()
