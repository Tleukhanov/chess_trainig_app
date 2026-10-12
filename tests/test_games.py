"""Тесты ссылки на партию (Game.link): парсеры API и PGN.

Ссылка рождается в парсере, фронт её только рисует: API-игра всегда
личессовская (link из id), PGN — из заголовка Link, иначе из Site-URL,
иначе None. Сеть и движок не используются.
"""

from __future__ import annotations

import io
import unittest

import chess.pgn

from app.games import _game_from_api, _game_from_pgn


def _pgn_game(headers: dict[str, str]) -> chess.pgn.Game:
    """PGN-партия из заголовков (без ходов) через chess.pgn.read_game."""
    lines = "".join(f'[{k} "{v}"]\n' for k, v in headers.items())
    game = chess.pgn.read_game(io.StringIO(lines + "\n*\n"))
    assert game is not None
    return game


_BASE_HEADERS = {
    "Event": "Rated Rapid game",
    "White": "alice",
    "Black": "bob",
    "Result": "1-0",
    "UTCDate": "2026.01.01",
    "UTCTime": "12:00:00",
    "WhiteElo": "1500",
    "BlackElo": "1500",
    "Termination": "Normal",
}


class ApiLinkTests(unittest.TestCase):
    def test_api_game_link_from_id(self) -> None:
        game = _game_from_api({"id": "abcd1234"}, "alice")
        self.assertEqual(game.id, "abcd1234")
        self.assertEqual(game.link, "https://lichess.org/abcd1234")

    def test_api_game_empty_id_no_link(self) -> None:
        game = _game_from_api({}, "alice")
        self.assertEqual(game.id, "")
        self.assertIsNone(game.link)


class PgnLinkTests(unittest.TestCase):
    def test_pgn_link_header_wins_over_site(self) -> None:
        game = _game_from_pgn(
            _pgn_game(
                {
                    **_BASE_HEADERS,
                    "Site": "https://example.com/games/xyz",
                    "Link": "https://lichess.org/abcd1234",
                }
            ),
            "alice",
        )
        self.assertEqual(game.link, "https://lichess.org/abcd1234")

    def test_pgn_site_url_becomes_link(self) -> None:
        game = _game_from_pgn(
            _pgn_game(
                {**_BASE_HEADERS, "Site": "https://lichess.org/abcd1234/"}
            ),
            "alice",
        )
        self.assertEqual(game.id, "abcd1234")
        self.assertEqual(game.link, "https://lichess.org/abcd1234")

    def test_pgn_without_url_no_link(self) -> None:
        game = _game_from_pgn(
            _pgn_game({**_BASE_HEADERS, "Site": "chess-trainer"}),
            "alice",
        )
        self.assertIsNone(game.link)

    def test_pgn_without_site_no_link(self) -> None:
        game = _game_from_pgn(_pgn_game(dict(_BASE_HEADERS)), "alice")
        self.assertIsNone(game.link)


if __name__ == "__main__":
    unittest.main()
