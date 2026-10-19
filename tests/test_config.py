"""Тесты чистого хелпера урезания профиля Stockfish (:mod:`app.config`).

Проверяем ``_clamp_stockfish`` напрямую с ``heroku=True/False`` — без опоры
на глобальное окружение при импорте, чтобы тест не зависел от DYNO.
"""

from __future__ import annotations

import unittest

from app.config import _clamp_stockfish


class ClampStockfishTests(unittest.TestCase):
    def test_heroku_caps_big_values(self) -> None:
        """На Heroku большие значения зажимаются до (32, 1, 800, 12, 1)."""
        self.assertEqual(
            _clamp_stockfish(
                heroku=True,
                hash_mb=512,
                threads=4,
                time_ms=3000,
                depth=14,
                multipv=3,
            ),
            (32, 1, 800, 12, 1),
        )

    def test_heroku_does_not_raise_small_values(self) -> None:
        """min-семантика: маленькие значения не растут до потолков."""
        self.assertEqual(
            _clamp_stockfish(
                heroku=True,
                hash_mb=16,
                threads=1,
                time_ms=500,
                depth=8,
                multipv=1,
            ),
            (16, 1, 500, 8, 1),
        )

    def test_heroku_mixed_values(self) -> None:
        """Каждый параметр зажимается независимо."""
        self.assertEqual(
            _clamp_stockfish(
                heroku=True,
                hash_mb=64,
                threads=2,
                time_ms=100,
                depth=20,
                multipv=5,
            ),
            (32, 1, 100, 12, 1),
        )

    def test_local_returns_inputs_unchanged(self) -> None:
        """Локально (heroku=False) конфиг не трогаем — полная сила анализа."""
        self.assertEqual(
            _clamp_stockfish(
                heroku=False,
                hash_mb=512,
                threads=4,
                time_ms=3000,
                depth=14,
                multipv=3,
            ),
            (512, 4, 3000, 14, 3),
        )

    def test_local_small_values_unchanged(self) -> None:
        self.assertEqual(
            _clamp_stockfish(
                heroku=False,
                hash_mb=8,
                threads=1,
                time_ms=200,
                depth=6,
                multipv=1,
            ),
            (8, 1, 200, 6, 1),
        )


if __name__ == "__main__":
    unittest.main()
