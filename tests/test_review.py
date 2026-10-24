"""Тесты поиска повторяющихся ошибок (app.review)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.review import repeated_mistakes


def _game(gid: str, opening: str = "Italian", created: int = 1):
    return SimpleNamespace(id=gid, opening=opening, created_at=created)


def _analysis(moves):
    return {"moves": moves}


class RepeatedMistakesTests(unittest.TestCase):
    def test_finds_same_ply_and_san(self) -> None:
        current = _game("cur")
        cur_a = _analysis([{"san": "Nf3", "classification": "blunder", "drop": 30}])
        history = [
            (_game("old", created=10), _analysis([{"san": "Nf3", "classification": "blunder", "drop": 25}])),
        ]
        out = repeated_mistakes(current, cur_a, history)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["san"], "Nf3")
        self.assertEqual(out[0]["game_id"], "old")

    def test_ignores_different_ply_or_san(self) -> None:
        current = _game("cur")
        cur_a = _analysis([{"san": "Nf3", "classification": "blunder"}])
        history = [
            (_game("old"), _analysis([{"san": "e4", "classification": "blunder"}, {"san": "Nf3", "classification": "blunder"}])),
        ]
        self.assertEqual(repeated_mistakes(current, cur_a, history), [])

    def test_ignores_non_errors(self) -> None:
        current = _game("cur")
        cur_a = _analysis([{"san": "Nf3", "classification": "good"}])
        history = [(_game("old"), _analysis([{"san": "Nf3", "classification": "blunder"}]))]
        self.assertEqual(repeated_mistakes(current, cur_a, history), [])

    def test_excludes_current_game(self) -> None:
        current = _game("cur")
        cur_a = _analysis([{"san": "Nf3", "classification": "blunder"}])
        history = [(_game("cur"), _analysis([{"san": "Nf3", "classification": "blunder"}]))]
        self.assertEqual(repeated_mistakes(current, cur_a, history), [])

    def test_limit(self) -> None:
        current = _game("cur")
        cur_a = _analysis([{"san": "Nf3", "classification": "blunder"}, {"san": "e4", "classification": "blunder"}])
        history = [
            (_game("old1"), _analysis([{"san": "Nf3", "classification": "blunder"}, {"san": "e4", "classification": "blunder"}])),
        ]
        self.assertEqual(len(repeated_mistakes(current, cur_a, history, limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
