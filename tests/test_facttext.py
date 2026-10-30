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

    def test_why_text_capture_uses_accusative(self) -> None:
        text = facttext.why_text(
            {"best_move_san": "Nxg5"},
            "rnbqkb1r/pppppppp/8/6n1/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 2 3",
            "h3",
        )
        self.assertEqual(text, "Ты не забрал коня на g5: Nxg5.")
        pawn = facttext.why_text(
            {"best_move_san": "exd5", "best_line": []},
            "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        )
        self.assertEqual(pawn, "Ты не забрал пешку на d5: exd5.")

    def test_why_text_empty_when_best_is_played(self) -> None:
        # Нельзя писать «не забрал», если этот же ход и был лучшим.
        self.assertEqual(
            facttext.why_text(
                {"best_move_san": "Nf3", "best_line": ["d5"]},
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "Nf3",
            ),
            "",
        )

    def test_why_text_check(self) -> None:
        text = facttext.why_text(
            {"best": "Rh8+"},
            "4k3/8/8/8/8/8/8/4K2R w K - 0 1",
            "Rg1",
        )
        self.assertEqual(text, "Ты не дал шах: Rh8+.")

    def test_why_text_mate(self) -> None:
        text = facttext.why_text(
            {"best_move_san": "Ra8#"},
            "7k/6pp/8/8/8/8/8/R3K3 w Q - 0 1",
            "Rb1",
        )
        self.assertEqual(text, "Ты не поставил мат: Ra8#.")

    def test_why_text_promotion(self) -> None:
        text = facttext.why_text(
            {"best": "e8=Q+"},
            "4k3/4P3/8/8/8/8/8/4K3 w - - 0 1",
            "Kf1",
        )
        self.assertEqual(text, "Ты не провёл превращение: e8=Q+.")

    def test_why_text_quiet_uses_engine_line_only(self) -> None:
        text = facttext.why_text(
            {
                "best_move_san": "Nf3",
                "best_line": ["d5", "d4"],
            },
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "Nh3",
        )
        self.assertEqual(text, "Ты сыграл Nh3, но движок считал важнее Nf3: d5 d4.")

    def test_why_text_invents_nothing_without_facts(self) -> None:
        # Нет лучшего хода — нет текста, а не догадка.
        self.assertEqual(facttext.why_text({}, None), "")
        # Без линии остаётся только ход движка: никаких «угроз» и «планов».
        text = facttext.why_text(
            {"best": "Nf3"},
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        )
        self.assertEqual(text, "Движок видел Nf3.")
        self.assertNotIn("угроз", text.lower())
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
