"""Детерминированные тексты не должны выдумывать ходы и поля."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app import facttext
from app.intent_review import review_intents


class FactTextTests(unittest.TestCase):
    def test_move_fact_grammar(self) -> None:
        self.assertEqual(
            facttext.move_fact("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "e4"),
            "ход пешкой на e4",
        )
        self.assertEqual(
            facttext.move_fact("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "Nf3"),
            "ход конём на f3",
        )
        self.assertEqual(
            facttext.move_fact(
                "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                "exd5",
            ),
            "взятие пешки на d5",
        )

    def test_full_line_starts_with_best(self) -> None:
        self.assertEqual(facttext.full_line("Nc5", ["axb4", "Rxa1"]), "Nc5 axb4 Rxa1")
        self.assertEqual(facttext.full_line("Nc5", ["Nc5", "axb4"]), "Nc5 axb4")

    def test_error_text_has_engine_line_and_alternatives(self) -> None:
        text = facttext.error_text(
            {
                "san": "cxd5",
                "classification": "inaccuracy",
                "drop": 5.8,
                "best_move_san": "Qc7",
                "best_line": ["Rb1", "h5"],
                "alternatives": ["h5", "Bh6"],
            },
            "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        )
        self.assertIn("неточность", text)
        self.assertIn("Движок предлагал Qc7", text)
        self.assertIn("Линия движка: Qc7 Rb1 h5", text)
        self.assertIn("Рядом по оценке: h5, Bh6", text)

    def test_opening_summary_plural(self) -> None:
        entries = [
            {"san": "bxa3", "classification": "blunder", "drop": 21.2},
            {"san": "cxd5", "classification": "inaccuracy", "drop": 5.8},
        ]
        summary = facttext.opening_summary(entries, "English Opening")
        self.assertIn("2 ошибочных хода", summary)
        self.assertIn("Худший — bxa3", summary)

    def test_task_text_only_facts(self) -> None:
        text = facttext.task_text(
            {
                "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "san_played": "Nh3",
                "best_san": "Nf3",
                "best_line": ["d5", "d4"],
                "win_before": 50.0,
                "win_after": 44.0,
                "drop": 6.0,
            }
        )
        self.assertIn("В партии ты сыграл Nh3", text)
        self.assertIn("Движок считал лучшим Nf3", text)
        self.assertIn("Линия движка: Nf3 d5 d4", text)
        self.assertNotIn("активизировать", text.lower())


class IntentReviewFactsTests(unittest.TestCase):
    def test_review_is_deterministic_and_engine_backed(self) -> None:
        game = SimpleNamespace(
            moves=["Nh3", "e5"],
            user_color="white",
            opening="Test",
            eco="X00",
            result_for_user="draw",
        )
        analysis = {
            "moves": [
                {
                    "san": "Nh3",
                    "classification": "mistake",
                    "drop": 12.0,
                    "best_move_san": "Nf3",
                    "best_line": ["d5", "d4"],
                },
                {"san": "e5"},
            ]
        }
        text = review_intents(game, analysis, {0: "хотел вывести коня"})
        self.assertIn("Ты сам увидел ошибку", text)
        self.assertIn("Движок видел Nf3", text)
        self.assertIn("линия: Nf3 d5 d4", text)
        self.assertNotIn("LLM", text)


if __name__ == "__main__":
    unittest.main()
