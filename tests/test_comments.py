"""Тесты шаблонных комментариев и итогового текста (app.comments).

Проверяются все ветки бейджей, уточнения по причине, честность итогов
(никаких цифр и рейтингов, которых нет во входных данных) и устойчивость
к None/пустым значениям.
"""

from __future__ import annotations

import re
import unittest

from app.comments import category_note, game_summary, move_comment

_DIGIT = re.compile(r"\d")


def _entry(**overrides: object) -> dict:
    base: dict = {
        "san": "e4",
        "classification": "good",
        "drop": 0.0,
        "win_before": 60.0,
        "win_after": 60.0,
        "best_move_san": None,
    }
    base.update(overrides)
    return base


class MoveCommentTests(unittest.TestCase):
    """Комментарии к отдельным ходам."""

    def test_book_move(self) -> None:
        self.assertEqual(
            move_comment(_entry(san="e4", classification="book"), "book"),
            "e4 — теоретический ход.",
        )

    def test_best_move_has_no_invented_numbers(self) -> None:
        text = move_comment(
            _entry(san="Nf3", classification="best", drop=0.0), "best"
        )
        self.assertEqual(text, "Nf3 — лучший ход.")
        self.assertNotIn("%", text)
        self.assertNotIn("win%", text)
        self.assertNotIn("лучше было", text)

    def test_inaccuracy_with_drop_and_best(self) -> None:
        text = move_comment(
            _entry(san="c4", drop=6.0, best_move_san="Bb5"),
            "inaccuracy",
        )
        self.assertEqual(text, "c4 — неточность (−6 win%): лучше было Bb5.")

    def test_blunder_takes_best_move_from_entry_and_adds_cause(self) -> None:
        text = move_comment(
            _entry(
                san="Qh5",
                drop=25.0,
                win_before=70.0,
                win_after=45.0,
                best_move_san="Qxe5+",
                classification="blunder",
            ),
            "blunder",
            cause="tactical_oversight",
        )
        self.assertEqual(
            text,
            "Qh5 — зевок (−25 win%): лучше было Qxe5+. Резкая потеря за один ход.",
        )
        self.assertEqual(text.count("."), 2)

    def test_missed_win_reports_real_win_percentages(self) -> None:
        text = move_comment(
            _entry(san="Qh5", win_before=96.0, win_after=88.0, drop=8.0),
            "missed_win",
        )
        self.assertEqual(
            text, "Qh5 — упущен выигрыш: было 96%, стало 88%."
        )

    def test_cause_notes_for_time_pressure_and_opening(self) -> None:
        with self.subTest(cause="time_pressure"):
            text = move_comment(
                _entry(san="Rd1", classification="mistake", drop=None),
                "mistake",
                cause="time_pressure",
            )
            self.assertEqual(
                text, "Rd1 — ошибка. Скорее всего, сказался цейтнот."
            )
            self.assertNotIn("%", text)
            self.assertNotIn("win%", text)

        with self.subTest(cause="strategic"):
            text = move_comment(
                _entry(san="Nc3", drop=7.5, best_move_san="Bb5"),
                "mistake",
                phase="opening",
                cause="strategic",
            )
            self.assertIn("−7,5 win%", text)
            self.assertIn("лучше было Bb5", text)
            self.assertTrue(
                text.endswith(
                    "Ошибка в дебюте — вопрос к плану, а не к одному ходу."
                )
            )

    def test_unknown_badge_and_missing_entry_are_neutral(self) -> None:
        self.assertEqual(
            move_comment({"san": "e4"}, "unknown"),
            "e4 — ход без оценки: данных недостаточно.",
        )
        self.assertEqual(
            move_comment(None, ""),
            "Комментария нет: данных для оценки хода недостаточно.",
        )


class GameSummaryTests(unittest.TestCase):
    """Итоговый текст партии."""

    def test_summary_praises_best_phase_and_points_to_weakest(self) -> None:
        phases = {
            "opening": {"accuracy": 92.0, "count": 12},
            "midgame": {"accuracy": 84.0, "count": 20},
            "endgame": {"accuracy": 58.0, "count": 10},
        }
        text = game_summary(
            phases, 78.0, user_color="white", result="win"
        )
        self.assertEqual(
            text,
            "Точность по фазам: дебют — 92%, миттельшпиль — 84%, "
            "эндшпиль — 58%. Сильная сторона — дебют, слабая — эндшпиль. "
            "Итоговая точность — 78%. "
            "Партия за белых завершилась победой.",
        )
        self.assertNotIn("КМС", text)
        self.assertNotIn("2200", text)
        self.assertNotIn("рейтинг", text)
        self.assertEqual(text.count("."), 4)

    def test_summary_without_data_is_honest(self) -> None:
        text = game_summary(None, None, user_color="black", result="loss")
        self.assertEqual(
            text,
            "Данных для оценки партии за чёрных недостаточно. "
            "Партия за чёрных завершилась поражением.",
        )
        self.assertIsNone(_DIGIT.search(text))
        self.assertNotIn("%", text)

        bad = {"opening": {"accuracy": "92"}, "endgame": {"accuracy": 140.0}}
        text = game_summary(bad, float("nan"), user_color="white")
        self.assertEqual(text, "Данных для оценки партии за белых недостаточно.")
        self.assertIsNone(_DIGIT.search(text))

    def test_summary_falls_back_to_avg_drop_format(self) -> None:
        phases = {
            "opening": {"count": 12, "blunders": 0, "avg_drop": 2.5},
            "endgame": {"count": 8, "blunders": 2, "avg_drop": 14.0},
        }
        text = game_summary(phases, None, user_color="white")
        self.assertEqual(
            text,
            "Средняя потеря по фазам: дебют — 2,5 win%, эндшпиль — 14 win%. "
            "Больше всего потерь — эндшпиль.",
        )
        self.assertNotIn("точность", text.lower())


class CategoryNoteTests(unittest.TestCase):
    """Короткие оценки категорий."""

    def test_known_category_translated_and_graded(self) -> None:
        self.assertEqual(category_note("opening", 92.0), "Дебют — сильно")
        self.assertEqual(
            category_note("tactics", 62.0), "Тактика — есть над чем работать"
        )
        self.assertEqual(category_note("endgame", 40.0), "Эндшпиль — слабое место")

    def test_unknown_category_kept_and_bad_score_is_neutral(self) -> None:
        self.assertEqual(
            category_note("Расчёт вариантов", 85.0),
            "Расчёт вариантов — уверенно",
        )
        for score in (None, -5.0, 120.0, float("inf"), True):
            with self.subTest(score=score):
                text = category_note("tactics", score)  # type: ignore[arg-type]
                self.assertEqual(text, "Тактика — нет данных.")
                self.assertIsNone(_DIGIT.search(text))
        self.assertEqual(category_note("", 80.0), "Категория — нет данных.")


if __name__ == "__main__":
    unittest.main()
