"""Тесты импорта PGN (app.pgn_import)."""

from __future__ import annotations

import unittest

from app.pgn_import import parse_pgn_games

PGN_ONE = """[Event "Test"]
[Site "https://lichess.org/abcd1234"]
[Date "2026.10.25"]
[White "Tleukhanov"]
[Black "Rival"]
[Result "1-0"]
[WhiteElo "2200"]
[BlackElo "2100"]
[ECO "C60"]
[Opening "Ruy Lopez"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0
"""

PGN_NO_SITE = """[Event "Test"]
[White "Someone"]
[Black "Other"]
[Result "1/2-1/2"]

1. d4 d5 2. c4 e6 1/2-1/2
"""

PGN_TWO = PGN_ONE + "\n" + PGN_NO_SITE.replace('1/2-1/2', '0-1')


class ParsePgnTests(unittest.TestCase):
    def test_single_game_with_color_from_headers(self) -> None:
        games = parse_pgn_games(PGN_ONE, "Tleukhanov")
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.user_color, "white")
        self.assertEqual(game.opponent, "Rival")
        self.assertEqual(game.result_for_user, "win")
        self.assertEqual(game.moves, ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"])
        self.assertEqual(game.id, "abcd1234")
        self.assertEqual(game.opening, "Ruy Lopez")

    def test_forced_black(self) -> None:
        games = parse_pgn_games(PGN_ONE, "Anyone", user_color="black")
        game = games[0]
        self.assertEqual(game.user_color, "black")
        self.assertEqual(game.opponent, "Tleukhanov")
        self.assertEqual(game.result_for_user, "loss")
        self.assertEqual(game.user_rating, 2100)

    def test_missing_site_gets_deterministic_id(self) -> None:
        first = parse_pgn_games(PGN_NO_SITE, "Someone")[0]
        second = parse_pgn_games(PGN_NO_SITE, "Someone")[0]
        self.assertTrue(first.id.startswith("pgn"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.result_for_user, "draw")

    def test_two_games(self) -> None:
        games = parse_pgn_games(PGN_TWO, "Someone", limit=5)
        self.assertEqual(len(games), 2)
        self.assertNotEqual(games[0].id, games[1].id)

    def test_empty_and_garbage(self) -> None:
        self.assertEqual(parse_pgn_games("", "u"), [])
        self.assertEqual(parse_pgn_games("   ", "u"), [])
        self.assertEqual(parse_pgn_games("not a pgn at all", "u"), [])

    def test_invalid_color(self) -> None:
        with self.assertRaises(ValueError):
            parse_pgn_games(PGN_ONE, "u", user_color="red")

    def test_limit(self) -> None:
        games = parse_pgn_games(PGN_TWO + "\n" + PGN_ONE, "Someone", limit=2)
        self.assertEqual(len(games), 2)


if __name__ == "__main__":
    unittest.main()
