"""Recent window для get_analyzed_games: отбор последних партий.

Регрессия на старое поведение: ``ORDER BY created_at ASC LIMIT n`` брал
самые старые партии. Теперь отбираются последние ``limit`` партий по
``created_at`` (tie-break по ``id``), а порядок возврата остаётся
хронологическим (от старых к новым) для совместимости с потребителями.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.db import Database
from app.games import Game


def _game(gid: str, created_at: int) -> Game:
    return Game(
        id=gid,
        rated=True,
        speed="rapid",
        created_at=created_at,
        status="mate",
        winner=None,
        white={"id": "w1", "username": "White"},
        black={"id": "b1", "username": "Black"},
        opening="Synthetic Opening",
        eco="A00",
        moves=["e4", "e5"],
        user_color="white",
        opponent="b1",
        user_rating=1500,
        user_rating_diff=0,
        result_for_user="win",
    )


class RecentWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db = Database(self.tmpdir / "recent.db")
        self.db.init_db()
        self.addCleanup(self.db.close)
        games = [
            _game("g-old", 1_000),
            _game("g-mid", 2_000),
            _game("g-new", 3_000),
        ]
        self.db.save_games(games, "me")
        for game in games:
            self.db.save_analysis(game.id, {"moves": []}, depth=8)

    def test_limit_selects_most_recent_not_oldest(self) -> None:
        pairs = self.db.get_analyzed_games("me", limit=2)
        self.assertEqual([game.id for game, _ in pairs], ["g-mid", "g-new"])

    def test_returned_order_stays_chronological(self) -> None:
        pairs = self.db.get_analyzed_games("me", limit=10)
        self.assertEqual([game.id for game, _ in pairs], ["g-old", "g-mid", "g-new"])

    def test_fewer_games_than_limit_returns_all(self) -> None:
        pairs = self.db.get_analyzed_games("me", limit=200)
        self.assertEqual(len(pairs), 3)

    def test_limit_zero_returns_empty(self) -> None:
        self.assertEqual(self.db.get_analyzed_games("me", limit=0), [])

    def test_equal_timestamps_tie_broken_by_id_deterministically(self) -> None:
        games = [_game("tie-a", 5_000), _game("tie-b", 5_000), _game("tie-c", 5_000)]
        self.db.save_games(games, "tie")
        for game in games:
            self.db.save_analysis(game.id, {"moves": []}, depth=8)
        pairs = self.db.get_analyzed_games("tie", limit=2)
        # Два самых "свежих" при равных created_at — с максимальными id.
        self.assertEqual([game.id for game, _ in pairs], ["tie-b", "tie-c"])
        # Повторный вызов даёт тот же порядок.
        again = self.db.get_analyzed_games("tie", limit=2)
        self.assertEqual([g.id for g, _ in again], [game.id for game, _ in pairs])

    def test_offset_pages_from_the_recent_end(self) -> None:
        pairs = self.db.get_analyzed_games("me", limit=2, offset=2)
        self.assertEqual([game.id for game, _ in pairs], ["g-old"])

    def test_since_until_bounds_filter(self) -> None:
        pairs = self.db.get_analyzed_games("me", limit=10, since_ms=1_500)
        self.assertEqual([game.id for game, _ in pairs], ["g-mid", "g-new"])
        pairs = self.db.get_analyzed_games("me", limit=10, until_ms=2_000)
        self.assertEqual([game.id for game, _ in pairs], ["g-old", "g-mid"])
        pairs = self.db.get_analyzed_games("me", limit=10, since_ms=1_500, until_ms=2_000)
        self.assertEqual([game.id for game, _ in pairs], ["g-mid"])


if __name__ == "__main__":
    unittest.main()
