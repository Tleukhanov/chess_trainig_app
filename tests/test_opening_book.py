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


class OpeningTreeTests(unittest.TestCase):
    """Регрессия на _to_node: корень обязан строить вложенное дерево.

    Раньше корень с prefix=() не проходил guard (`if prefix and ...`),
    а рекурсивный вызов ссылался на несуществующий ``top_n``/``top_children``,
    из-за чего /api/repertoire получал пустое ``tree["children"]`` либо NameError.
    """

    @staticmethod
    def _pairs(moves_list: list[list[str]], color: str = "white"):
        return [
            (_game(f"g{i}", moves, color=color), {})
            for i, moves in enumerate(moves_list)
        ]

    def test_root_has_children(self) -> None:
        pairs = self._pairs([["e4", "e6", "d4"], ["e4", "e6", "d4"], ["d4", "d5"]])
        book = build_opening_book(pairs, "white", max_depth=8, top_n=5)
        tree = book["tree"]
        self.assertEqual(tree["prefix"], [])
        self.assertEqual(tree["count"], 3)
        self.assertEqual([child["move"] for child in tree["children"]], ["e4", "d4"])
        self.assertEqual(tree["children"][0]["count"], 2)
        self.assertEqual(tree["top_children"][0]["move"], "e4")

    def test_nested_children_counts_and_prefixes(self) -> None:
        pairs = self._pairs(
            [["e4", "e6", "d4", "d5"]] * 4
            + [["e4", "e6", "Nf3", "d5"]] * 2
            + [["e4", "c5", "Nf3"]]
            + [["d4", "d5"]]
        )
        book = build_opening_book(pairs, "white", max_depth=8, top_n=5)
        tree = book["tree"]
        e4 = next(child for child in tree["children"] if child["move"] == "e4")
        self.assertEqual(e4["prefix"], ["e4"])
        self.assertEqual(e4["ply"], 1)
        self.assertEqual(e4["count"], 7)
        self.assertEqual(e4["share_pct"], 87.5)
        self.assertEqual([child["move"] for child in e4["children"]], ["e6", "c5"])
        e6 = next(child for child in e4["children"] if child["move"] == "e6")
        self.assertEqual(e6["prefix"], ["e4", "e6"])
        self.assertEqual(e6["count"], 6)
        self.assertEqual([child["move"] for child in e6["children"]], ["d4", "Nf3"])
        d4_node = next(child for child in e6["children"] if child["move"] == "d4")
        self.assertEqual(d4_node["prefix"], ["e4", "e6", "d4"])
        self.assertEqual(d4_node["count"], 4)
        self.assertEqual(d4_node["children"][0]["move"], "d5")
        self.assertEqual(d4_node["children"][0]["ply"], 4)
        # /api/repertoire потребляет tree как вложенный JSON: у листа нет детей.
        self.assertEqual(d4_node["children"][0]["children"], [])

    def test_max_depth_bounds_recursion(self) -> None:
        pairs = self._pairs([["e4", "e6", "d4", "d5", "Nd2", "c5"]] * 3)
        book = build_opening_book(pairs, "white", max_depth=2, top_n=5)
        tree = book["tree"]
        self.assertEqual([child["move"] for child in tree["children"]], ["e4"])
        e4 = tree["children"][0]
        self.assertEqual([child["move"] for child in e4["children"]], ["e6"])
        e6 = e4["children"][0]
        self.assertEqual(e6["ply"], 2)
        self.assertEqual(e6["children"], [])

    def test_max_depth_one_keeps_only_root_children(self) -> None:
        pairs = self._pairs([["e4", "e6"], ["d4", "d5"]])
        book = build_opening_book(pairs, "white", max_depth=1, top_n=5)
        tree = book["tree"]
        self.assertEqual(len(tree["children"]), 2)
        for child in tree["children"]:
            self.assertEqual(child["children"], [])
            self.assertEqual(child["top_children"], [])

    def test_empty_input_yields_empty_tree(self) -> None:
        book = build_opening_book([], "white", max_depth=8, top_n=5)
        tree = book["tree"]
        self.assertEqual(tree["prefix"], [])
        self.assertEqual(tree["count"], 0)
        self.assertIsNone(tree["points_pct"])
        self.assertEqual(tree["top_children"], [])
        self.assertEqual(tree["children"], [])

    def test_deterministic_ordering_and_top_n_limit(self) -> None:
        # Равное число партий на ход: порядок по SAN, а не по случайности.
        pairs = self._pairs([["d4", "d5"], ["e4", "e5"], ["c4", "e5"], ["g3", "g6"]])
        book = build_opening_book(pairs, "white", max_depth=2, top_n=2)
        tree = book["tree"]
        self.assertEqual([child["move"] for child in tree["children"]], ["c4", "d4"])
        self.assertEqual([row["move"] for row in tree["top_children"]], ["c4", "d4"])
        # Повторный вызов даёт идентичный результат.
        again = build_opening_book(pairs, "white", max_depth=2, top_n=2)
        self.assertEqual(again["tree"], tree)

    def test_top_n_truncates_children(self) -> None:
        pairs = self._pairs(
            [["e4", "e5"]] * 4 + [["d4", "d5"]] * 3 + [["c4", "e5"]] * 2 + [["g3", "g6"]]
        )
        book = build_opening_book(pairs, "white", max_depth=2, top_n=2)
        self.assertEqual([child["move"] for child in book["tree"]["children"]], ["e4", "d4"])


if __name__ == "__main__":
    unittest.main()
