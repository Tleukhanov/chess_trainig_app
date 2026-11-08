"""Тесты дебютной книги (реальный репертуар из кеша).

Синтетические игры: никаких реальных партий игроков и никаких LLM/движка.
"""

from __future__ import annotations

import unittest

from app.games import Game
from app.opening_book import build_opening_book


def _game(game_id: str, moves: list[str], color: str = "white", result: str = "win") -> Game:
    return Game(
        id=game_id,
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white" if (color == "white" and result == "win") or (color == "black" and result == "loss") else None,
        white={"name": "me" if color == "white" else "opp"},
        black={"name": "opp" if color == "white" else "me"},
        opening="Synthetic Opening",
        eco="A00",
        moves=list(moves),
        clocks=[],
        user_color=color,
        opponent="opp",
        result_for_user=result,
    )


class OpeningBookTests(unittest.TestCase):
    def test_white_main_line_from_e4_e6_d4(self) -> None:
        pairs = []
        # 6 раз e4 e6 d4 d5 Nd2 c5
        for i in range(6):
            pairs.append((Game(id=f"f{i}", rated=True, speed="rapid", created_at=0,
                               status="win", winner="white", white={"name": "me"}, black={"name": "opp"},
                               opening="French Defense", eco="C00", moves=["e4", "e6", "d4", "d5", "Nd2", "c5", "Nf3", "Nf6"],
                               clocks=[], user_color="white", opponent="opp", result_for_user="win"), {}))
        # 3 раза e4 e5 Nf3 Nc6
        for i in range(3):
            pairs.append((Game(id=f"p{i}", rated=True, speed="rapid", created_at=0,
                               status="win", winner="white", white={"name": "me"}, black={"name": "opp"},
                               opening="King Pawn", eco="C20", moves=["e4", "e5", "Nf3", "Nc6", "d4", "exd4"],
                               clocks=[], user_color="white", opponent="opp", result_for_user="win"), {}))
        book = build_opening_book(pairs, "white", max_depth=8)
        self.assertEqual(book["games"], 9)
        self.assertEqual(book["main_line"][:4], ["e4", "e6", "d4", "d5"])
        first = book["first_moves"][0]
        self.assertEqual(first["move"], "e4")
        self.assertEqual(first["count"], 9)
        self.assertEqual(first["share_pct"], 100.0)
        reply = first["replies"][0]
        self.assertEqual(reply["move"], "e6")
        self.assertEqual(reply["count"], 6)
        self.assertEqual(reply["responses"][0]["move"], "d4")

    def test_black_response_tree_from_e4_c5(self) -> None:
        pairs = []
        for i in range(17):
            pairs.append((Game(id=f"b{i}", rated=True, speed="rapid", created_at=0,
                               status="win", winner="black", white={"name": "opp"}, black={"name": "me"},
                               opening="Sicilian Defense", eco="B20", moves=["e4", "c5", "Nf3", "a6", "d4", "cxd4"],
                               clocks=[], user_color="black", opponent="opp", result_for_user="win"), {}))
        for i in range(3):
            pairs.append((Game(id=f"e{i}", rated=True, speed="rapid", created_at=0,
                               status="win", winner="black", white={"name": "opp"}, black={"name": "me"},
                               opening="King Pawn", eco="C20", moves=["e4", "e5", "Nf3", "Nc6", "d4", "exd4"],
                               clocks=[], user_color="black", opponent="opp", result_for_user="win"), {}))
        book = build_opening_book(pairs, "black", max_depth=8)
        self.assertEqual(book["games"], 20)
        # главная линия от корня: e4 c5 Nf3 a6 d4 cxd4
        self.assertEqual(book["main_line"][:4], ["e4", "c5", "Nf3", "a6"])
        first = book["first_moves"][0]
        self.assertEqual(first["move"], "c5")
        self.assertEqual(first["count"], 17)
        reply = first["replies"][0]
        self.assertEqual(reply["move"], "Nf3")
        self.assertEqual(reply["responses"][0]["move"], "a6")

    def test_insufficient_and_empty(self) -> None:
        book = build_opening_book([], "white")
        self.assertEqual(book["games"], 0)
        self.assertEqual(book["first_moves"], [])
        self.assertIsNone(book["tree"]["share_pct"])

        book = build_opening_book([(_game("g1", ["e4", "e6", "d4"]), {})], "white")
        self.assertEqual(book["first_moves"][0]["move"], "e4")
        self.assertEqual(book["first_moves"][0]["replies"][0]["move"], "e6")
        self.assertEqual(book["first_moves"][0]["replies"][0]["responses"][0]["move"], "d4")


if __name__ == "__main__":
    unittest.main()
