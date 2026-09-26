"""Тесты чтения метрик качества игры (app.metrics).

Ключевой сценарий: в кеше анализа остаются записи, сделанные до
переименования метрик, поэтому чтение должно понимать старые имена.
"""

import unittest

from app.metrics import LEGACY_METRIC_KEYS, METRIC_LABELS, metric


class MetricReadTests(unittest.TestCase):
    def test_reads_new_key(self) -> None:
        self.assertEqual(metric({"avg_win_loss": 3.5}, "avg_win_loss"), 3.5)
        self.assertEqual(metric({"avg_win_before": 82.0}, "avg_win_before"), 82.0)

    def test_reads_legacy_key(self) -> None:
        """Запись из кеша, сделанная до переименования, читается корректно."""
        self.assertEqual(metric({"acpl": 3.14}, "avg_win_loss"), 3.14)
        self.assertEqual(metric({"accuracy": 52.67}, "avg_win_before"), 52.67)

    def test_new_key_wins_over_legacy(self) -> None:
        both = {"avg_win_loss": 5.0, "acpl": 3.14}
        self.assertEqual(metric(both, "avg_win_loss"), 5.0)

    def test_default_when_absent(self) -> None:
        self.assertEqual(metric({}, "avg_win_loss"), 0.0)
        self.assertIsNone(metric({}, "avg_win_loss", default=None))

    def test_none_value_falls_back_to_default(self) -> None:
        self.assertEqual(metric({"avg_win_loss": None}, "avg_win_loss", default=1.5), 1.5)
        self.assertIsNone(metric({"avg_win_loss": None}, "avg_win_loss", default=None))

    def test_legacy_value_used_when_new_is_none(self) -> None:
        """Новый ключ есть, но пуст — берём старое значение, а не default."""
        analysis = {"avg_win_loss": None, "acpl": 2.5}
        self.assertEqual(metric(analysis, "avg_win_loss", default=0.0), 2.5)

    def test_coerces_numeric_strings(self) -> None:
        self.assertEqual(metric({"acpl": "4.5"}, "avg_win_loss"), 4.5)

    def test_bool_is_not_a_metric(self) -> None:
        self.assertEqual(metric({"avg_win_loss": True}, "avg_win_loss", default=9.0), 9.0)

    def test_garbage_value_falls_back(self) -> None:
        self.assertEqual(metric({"avg_win_loss": "n/a"}, "avg_win_loss", default=7.0), 7.0)
        self.assertIsNone(metric({"avg_win_loss": []}, "avg_win_loss", default=None))

    def test_int_values_are_read(self) -> None:
        self.assertEqual(metric({"acpl": 3}, "avg_win_loss"), 3.0)
        self.assertIsInstance(metric({"acpl": 3}, "avg_win_loss"), float)

    def test_unknown_key_returns_default(self) -> None:
        self.assertEqual(metric({"acpl": 3.14}, "blunders_per_game", default=0.0), 0.0)


class MetricTableTests(unittest.TestCase):
    def test_legacy_mapping_points_to_old_names(self) -> None:
        self.assertEqual(LEGACY_METRIC_KEYS["avg_win_loss"], "acpl")
        self.assertEqual(LEGACY_METRIC_KEYS["avg_win_before"], "accuracy")

    def test_every_legacy_key_has_a_label(self) -> None:
        for key in LEGACY_METRIC_KEYS:
            self.assertIn(key, METRIC_LABELS)

    def test_labels_avoid_acpl_and_accuracy_words(self) -> None:
        for label in METRIC_LABELS.values():
            lowered = label.lower()
            self.assertNotIn("acpl", lowered)
            self.assertNotIn("точн", lowered)


if __name__ == "__main__":
    unittest.main()
