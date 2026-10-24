"""Тесты классификации причин ошибок (app.mistakes).

Проверяются приоритет правил, границы порогов (20 секунд / 10 win.%,
20 win.%), дебют и повторяемость, а также устойчивость к None, bool
вместо чисел, отрицательным и неконечным значениям.
"""

from __future__ import annotations

import unittest

from app.mistakes import (
    CAUSE_LABELS,
    CAUSE_RANDOM,
    CAUSE_STRATEGIC,
    CAUSE_TACTICAL_OVERSIGHT,
    CAUSE_TIME_PRESSURE,
    cause_label,
    classify_cause,
    explain_cause,
)


def _entry(**overrides: object) -> dict[str, object]:
    """Запись хода с нейтральными значениями; часы и флаг не заданы."""
    base: dict[str, object] = {
        "san": "Qh5",
        "drop": 12.0,
        "classification": "mistake",
        "time_pressure": None,
        "clock_used": None,
        "win_before": 60.0,
        "win_after": 48.0,
    }
    base.update(overrides)
    return base


class ClassifyCauseTests(unittest.TestCase):
    def test_time_pressure_flag_wins_over_other_rules(self) -> None:
        entry = _entry(drop=30.0, time_pressure=True)
        self.assertEqual(classify_cause(entry, "middlegame"), CAUSE_TIME_PRESSURE)
        self.assertEqual(
            classify_cause(entry, "opening", repeated=True), CAUSE_TIME_PRESSURE
        )

    def test_clock_used_alone_is_not_time_pressure(self) -> None:
        """clock_used — потраченное время, а не остаток; сам по себе не цейтнот."""
        self.assertEqual(
            classify_cause(_entry(clock_used=20.0, drop=10.0), "middlegame"),
            CAUSE_RANDOM,
        )
        # Долгое раздумье с крупной потерей — тактический зевок, не цейтнот.
        self.assertEqual(
            classify_cause(_entry(clock_used=25.0, drop=30.0), "middlegame"),
            CAUSE_TACTICAL_OVERSIGHT,
        )

    def test_tactical_oversight(self) -> None:
        self.assertEqual(
            classify_cause(_entry(drop=20.0), "middlegame"),
            CAUSE_TACTICAL_OVERSIGHT,
        )
        self.assertEqual(
            classify_cause(_entry(drop=25.0), "endgame"),
            CAUSE_TACTICAL_OVERSIGHT,
        )
        self.assertEqual(
            classify_cause(_entry(drop=19.99), "middlegame"), CAUSE_RANDOM
        )

    def test_opening_is_always_strategic(self) -> None:
        self.assertEqual(
            classify_cause(_entry(drop=30.0), "opening"), CAUSE_STRATEGIC
        )
        self.assertEqual(
            classify_cause(_entry(drop=30.0, clock_used=5.0), "opening"),
            CAUSE_STRATEGIC,
        )

    def test_repeated_small_error_is_strategic(self) -> None:
        self.assertEqual(
            classify_cause(_entry(drop=15.0), "middlegame", repeated=True),
            CAUSE_STRATEGIC,
        )
        self.assertEqual(
            classify_cause(_entry(drop=0.0), "endgame", repeated=True),
            CAUSE_STRATEGIC,
        )

    def test_repeated_large_error_stays_tactical(self) -> None:
        """Повторный крупный зевок вне дебюта не тактика (repeated) и не
        strategic (drop >= 20), поэтому по правилам ТЗ попадает в random."""
        self.assertEqual(
            classify_cause(_entry(drop=25.0), "middlegame", repeated=True),
            CAUSE_TACTICAL_OVERSIGHT,
        )

    def test_random_defaults(self) -> None:
        self.assertEqual(classify_cause(_entry(drop=5.0), "middlegame"), CAUSE_RANDOM)
        self.assertEqual(classify_cause({}, "middlegame"), CAUSE_RANDOM)
        self.assertEqual(classify_cause(None, None), CAUSE_RANDOM)

    def test_bool_negative_and_non_finite_values(self) -> None:
        """bool не считается числом, отрицательная потеря — нулевая."""
        self.assertEqual(
            classify_cause(_entry(drop=True, clock_used=True), "middlegame"),
            CAUSE_RANDOM,
        )
        self.assertEqual(
            classify_cause(_entry(drop=-25.0, clock_used=-5.0), "middlegame"),
            CAUSE_RANDOM,
        )
        self.assertEqual(
            classify_cause(_entry(drop=float("nan")), "middlegame"), CAUSE_RANDOM
        )
        self.assertEqual(
            classify_cause(_entry(drop=float("inf")), "middlegame"), CAUSE_RANDOM
        )
        # maia_prob на текущие правила не влияет.
        self.assertEqual(
            classify_cause(_entry(drop=25.0), "middlegame", maia_prob=0.9),
            CAUSE_TACTICAL_OVERSIGHT,
        )
        self.assertEqual(
            classify_cause(_entry(drop=25.0), "middlegame", maia_prob=None),
            CAUSE_TACTICAL_OVERSIGHT,
        )

    def test_phase_aliases_and_unknown_phase(self) -> None:
        self.assertEqual(
            classify_cause(_entry(drop=30.0), "midgame"), CAUSE_TACTICAL_OVERSIGHT
        )
        self.assertEqual(
            classify_cause(_entry(drop=30.0), " OPENING "), CAUSE_STRATEGIC
        )
        with self.assertRaises(ValueError):
            classify_cause(_entry(drop=30.0), "blitz")

    def test_labels_and_explanations(self) -> None:
        self.assertEqual(CAUSE_LABELS[CAUSE_TIME_PRESSURE], "цейтнот")
        self.assertEqual(CAUSE_LABELS[CAUSE_TACTICAL_OVERSIGHT], "тактический зевок")
        self.assertEqual(CAUSE_LABELS[CAUSE_STRATEGIC], "стратегическая ошибка")
        self.assertEqual(CAUSE_LABELS[CAUSE_RANDOM], "случайная")
        self.assertEqual(cause_label(CAUSE_RANDOM), "случайная")
        self.assertEqual(cause_label("nope"), "неизвестная причина")

        timed = _entry(drop=25.0, clock_used=5.0)
        self.assertEqual(
            explain_cause(CAUSE_TIME_PRESSURE, timed),
            "потеря 25 win% на последних секундах — скорее цейтнот",
        )
        self.assertIn("цейтнот", explain_cause(CAUSE_TIME_PRESSURE, _entry()))

        tactical = explain_cause(CAUSE_TACTICAL_OVERSIGHT, _entry(drop=25.0))
        self.assertIn("25 win%", tactical)
        self.assertIn("тактика", tactical)

        opening = _entry(drop=25.0)
        self.assertEqual(
            explain_cause(CAUSE_STRATEGIC, opening, repeated=True, phase="opening"),
            "повторяющаяся ошибка в дебюте — вопрос к плану, а не к ходу",
        )
        # Фаза читается и из entry["phase"], если аргумент не передан.
        self.assertEqual(
            explain_cause(
                CAUSE_STRATEGIC,
                _entry(drop=25.0, phase="opening"),
                repeated=True,
            ),
            "повторяющаяся ошибка в дебюте — вопрос к плану, а не к ходу",
        )
        self.assertIn("разовый", explain_cause(CAUSE_RANDOM, _entry(drop=5.0)))
        self.assertIn("не определена", explain_cause("nope", _entry()))


if __name__ == "__main__":
    unittest.main()
