"""Тесты бейджей ходов для разбора партии (app.review_moves).

Проверяются приоритет правил (книга, упущенный выигрыш, жертва), границы
порогов потери win.% (2/5/10/20), устойчивость к None/bool/NaN/inf и
отсутствующим ключам, а также эвристика жертвы на простых позициях.
"""

from __future__ import annotations

import unittest

import chess

from app.review_moves import (
    BADGE_BEST,
    BADGE_BLUNDER,
    BADGE_BOOK,
    BADGE_BRILLIANT,
    BADGE_EXCELLENT,
    BADGE_GOOD,
    BADGE_GREAT,
    BADGE_INACCURACY,
    BADGE_LABELS_RU,
    BADGE_MISSED_WIN,
    BADGE_MISTAKE,
    BADGE_SYMBOLS,
    badge_for,
    badge_label,
    detect_sacrifice,
)

# Белый ферзь жертвуется на h7 и забирается королём.
QUEEN_SAC = "6k1/5ppp/8/7Q/8/8/5PPP/6K1 w - - 0 1"
# Белый слон отдаётся за пешку h7: потеря ровно 2 единицы.
BISHOP_SAC = "6k1/5ppp/8/8/8/3B4/5PPP/6K1 w - - 0 1"
# Размен ладьями: баланс возвращается к исходному, жертвы нет.
EVEN_EXCHANGE = "r3r2k/8/8/8/8/8/8/4R1K1 w - - 0 1"


def _entry(**overrides: object) -> dict[str, object]:
    """Нейтральный ход: не лучший, без потери win.%."""
    base: dict[str, object] = {
        "san": "Nf3",
        "best_move_san": "e4",
        "drop": 0.0,
        "win_before": 60.0,
        "win_after": 60.0,
        "classification": "good",
    }
    base.update(overrides)
    return base


class BadgeForTests(unittest.TestCase):
    def test_book_priority_and_gross_error(self) -> None:
        self.assertEqual(badge_for(_entry(drop=0.0), in_book=True), BADGE_BOOK)
        # Неточность в книге — всё ещё теория.
        self.assertEqual(
            badge_for(_entry(drop=12.0, classification="mistake"), in_book=True),
            BADGE_BOOK,
        )
        # Зевок книгу отменяет.
        self.assertEqual(
            badge_for(
                _entry(drop=24.0, win_before=60.0, win_after=36.0, classification="blunder"),
                in_book=True,
            ),
            BADGE_BLUNDER,
        )
        self.assertNotEqual(
            badge_for(_entry(drop=5.0, classification="blunder"), in_book=True),
            BADGE_BOOK,
        )

    def test_missed_win(self) -> None:
        entry = _entry(win_before=96.0, win_after=84.0, drop=12.0, classification="mistake")
        self.assertEqual(badge_for(entry), BADGE_MISSED_WIN)
        # Ровно на границе 95/90 правило ещё срабатывает.
        self.assertEqual(
            badge_for(_entry(win_before=95.0, win_after=90.0, drop=5.0)),
            BADGE_MISSED_WIN,
        )
        # Упущенный выигрыш важнее флага лучшего хода.
        self.assertEqual(
            badge_for(_entry(win_before=97.0, win_after=80.0, drop=17.0), is_best=True),
            BADGE_MISSED_WIN,
        )
        # Без win_after правило не применяется — остаётся потеря win.%.
        self.assertEqual(
            badge_for(_entry(win_before=96.0, win_after=None, drop=12.0)),
            BADGE_MISTAKE,
        )

    def test_brilliant(self) -> None:
        entry = _entry(win_before=80.0, win_after=80.0, drop=0.0)
        self.assertEqual(badge_for(entry, is_best=True, sacrifice=True), BADGE_BRILLIANT)
        # Жертва не лучшим ходом блестящей не считается.
        self.assertEqual(badge_for(entry, is_best=False, sacrifice=True), BADGE_EXCELLENT)

    def test_brilliant_conditions_fall_back(self) -> None:
        # win_before >= 95 — уже не жертва, а лучший ход.
        high = _entry(win_before=96.0, win_after=96.0, drop=0.0)
        self.assertEqual(badge_for(high, is_best=True, sacrifice=True), BADGE_BEST)
        # Без жертвы лучший ход при win_before < 50 — отличный.
        calm = _entry(win_before=40.0, win_after=40.0, drop=0.0)
        self.assertEqual(badge_for(calm, is_best=True, sacrifice=False), BADGE_GREAT)
        # Нет оценки — жертву подтвердить нельзя, остаётся best.
        unknown = _entry(win_before=None, win_after=None, drop=None)
        self.assertEqual(badge_for(unknown, is_best=True, sacrifice=True), BADGE_BEST)

    def test_great_and_best(self) -> None:
        self.assertEqual(
            badge_for(_entry(win_before=45.0, win_after=45.0), is_best=True),
            BADGE_GREAT,
        )
        self.assertEqual(
            badge_for(_entry(win_before=49.99, win_after=49.99), is_best=True),
            BADGE_GREAT,
        )
        self.assertEqual(
            badge_for(_entry(win_before=50.0, win_after=50.0), is_best=True),
            BADGE_BEST,
        )
        self.assertEqual(
            badge_for(_entry(win_before=70.0, win_after=70.0), is_best=True),
            BADGE_BEST,
        )

    def test_best_derived_from_san(self) -> None:
        entry = _entry(san="Nf3", best_move_san="Nf3", win_before=70.0, win_after=70.0)
        self.assertEqual(badge_for(entry), BADGE_BEST)
        # Суффиксы шаха/мата не мешают сравнению, как в analyzer._san_key.
        check_entry = _entry(
            san="Qxf7+", best_move_san="Qxf7#", win_before=70.0, win_after=70.0
        )
        self.assertEqual(badge_for(check_entry), BADGE_BEST)
        # Явный False перекрывает совпадение SAN.
        self.assertEqual(badge_for(entry, is_best=False), BADGE_EXCELLENT)

    def test_drop_thresholds(self) -> None:
        cases = [
            (-4.0, BADGE_EXCELLENT),
            (0.0, BADGE_EXCELLENT),
            (1.99, BADGE_EXCELLENT),
            (2.0, BADGE_GOOD),
            (4.99, BADGE_GOOD),
            (5.0, BADGE_INACCURACY),
            (9.99, BADGE_INACCURACY),
            (10.0, BADGE_MISTAKE),
            (19.99, BADGE_MISTAKE),
            (20.0, BADGE_BLUNDER),
            (35.0, BADGE_BLUNDER),
        ]
        for drop, expected in cases:
            with self.subTest(drop=drop):
                entry = _entry(drop=drop, win_before=60.0, win_after=60.0 - drop)
                self.assertEqual(badge_for(entry), expected)

    def test_robustness(self) -> None:
        self.assertEqual(badge_for({}), BADGE_GOOD)
        self.assertEqual(badge_for(None), BADGE_GOOD)  # type: ignore[arg-type]
        # drop можно восстановить из win_before/win_after.
        self.assertEqual(
            badge_for({"drop": None, "win_before": 60.0, "win_after": 45.0}),
            BADGE_MISTAKE,
        )
        self.assertEqual(
            badge_for({"drop": float("nan"), "win_before": 60.0, "win_after": 58.0}),
            BADGE_GOOD,
        )
        self.assertEqual(
            badge_for({"drop": float("inf"), "win_before": 60.0, "win_after": 56.0}),
            BADGE_GOOD,
        )
        # bool вместо числа — не число.
        self.assertEqual(
            badge_for({"drop": True, "win_before": 60.0, "win_after": 59.5}),
            BADGE_EXCELLENT,
        )
        # Без чисел остаётся classification, иначе good.
        self.assertEqual(
            badge_for({"drop": float("nan"), "classification": "blunder"}),
            BADGE_BLUNDER,
        )
        self.assertEqual(
            badge_for({"drop": None, "classification": "nonsense"}),
            BADGE_GOOD,
        )
        # Нечисловой is_best не ломает вывод из SAN.
        same = _entry(san="Nf3", best_move_san="Nf3", win_before=70.0, win_after=70.0)
        self.assertEqual(badge_for(same, is_best=float("nan")), BADGE_BEST)
        self.assertEqual(badge_for(_entry(win_before=70.0, win_after=70.0), is_best=1), BADGE_BEST)

    def test_labels(self) -> None:
        expected = {
            BADGE_BOOK: "теория",
            BADGE_BRILLIANT: "блестящий ход",
            BADGE_GREAT: "отличный ход",
            BADGE_BEST: "лучший ход",
            BADGE_EXCELLENT: "очень хороший",
            BADGE_GOOD: "хороший ход",
            BADGE_INACCURACY: "неточность",
            BADGE_MISTAKE: "ошибка",
            BADGE_BLUNDER: "зевок",
            BADGE_MISSED_WIN: "упущенный выигрыш",
        }
        self.assertEqual(BADGE_LABELS_RU, expected)
        for badge, label in expected.items():
            with self.subTest(badge=badge):
                self.assertEqual(badge_label(badge), label)
                # У каждого бейджа есть слот для символа в UI.
                self.assertIn(badge, BADGE_SYMBOLS)
        self.assertEqual(badge_label(" BLUNDER "), "зевок")
        self.assertEqual(badge_label("playful"), "неизвестно")
        self.assertEqual(badge_label(None), "неизвестно")


class DetectSacrificeTests(unittest.TestCase):
    def test_detects_queen_and_bishop_sacrifices(self) -> None:
        queen_board = chess.Board(QUEEN_SAC)
        fen_before = queen_board.fen()
        self.assertTrue(detect_sacrifice(queen_board, "Qxh7+", ["Kxh7"]))
        # Доска не мутируется.
        self.assertEqual(queen_board.fen(), fen_before)
        # Жертву не приняли — материала никто не терял.
        self.assertFalse(detect_sacrifice(queen_board, "Qxh7+", ["Kf8"]))

        bishop_board = chess.Board(BISHOP_SAC)
        # Слон за пешку — ровно две единицы, порог включительный.
        self.assertTrue(detect_sacrifice(bishop_board, "Bxh7+", ["Kxh7"]))
        # Без продолжения потеря ещё не видна.
        self.assertFalse(detect_sacrifice(bishop_board, "Bxh7+", []))
        self.assertFalse(detect_sacrifice(bishop_board, "Bxh7+", ["Kxh7"], plies=0))

    def test_even_exchange_is_not_sacrifice(self) -> None:
        board = chess.Board(EVEN_EXCHANGE)
        # Rxe8+ Rxe8: белые сначала выигрывают ладью, потом отдают свою.
        self.assertFalse(detect_sacrifice(board, "Rxe8+", ["Rxe8"]))
        # Обычное развитие фигур — тоже не жертва.
        self.assertFalse(detect_sacrifice(chess.Board(), "e4", ["e5", "Nf3"]))

    def test_bad_input_is_safe(self) -> None:
        self.assertFalse(detect_sacrifice(chess.Board(), "Qd5", []))
        self.assertFalse(detect_sacrifice(chess.Board(), "", ["e5"]))
        # Нелегальное продолжение просто обрывает просмотр.
        self.assertFalse(detect_sacrifice(chess.Board(), "e4", ["not-a-move"]))
        self.assertFalse(detect_sacrifice(chess.Board(), "e4", "e5"))  # type: ignore[arg-type]
        self.assertFalse(
            detect_sacrifice(chess.Board(), "e4", ["e5"], plies=float("nan"))  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    unittest.main()
