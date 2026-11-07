"""Тесты связки анализатора и тем хода (app.analyzer + app.themes).

CPU-only: Stockfish не запускается, ``analyze_position`` — фейк на
``python-chess``. Реальные партии игроков не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import chess

from app.analyzer import Engine, Eval, MoveAnalysis
from app.games import Game


class FakeEngine(Engine):
    """Минимальный ``Engine`` без бинаря: линии строятся из легальных ходов.

    ``analyze_game``/``_analyze_sequence``/``classify_move_themes`` —
    настоящие, движковыми возвращаются только детерминированные линии
    (разрыв win% между первой и второй ≥ 10, чтобы не попадать в
    ``confidence=low`` по теме). Процент для белых инвертируется для
    чёрных, как в настоящий ``analyze_position``.
    """

    def __init__(self) -> None:
        self.depth = 6
        self.multipv = 2
        self.threads = 1
        self.hash_mb = 16
        self.time_ms = 0
        self.path = None
        self._engine = None

    def analyze_position(self, board: chess.Board):
        legal = list(board.generate_legal_moves())
        if not legal:
            return []
        copy = board.copy(stack=False)
        pv1 = [copy.san(legal[0])]
        try:
            copy.push(legal[0])
        except ValueError:
            pass
        else:
            replies = list(copy.generate_legal_moves())
            if replies:
                pv1.append(copy.san(replies[0]))
        if len(legal) > 1:
            copy2 = board.copy(stack=False)
            pv2 = [copy2.san(legal[1])]
        else:
            pv2 = list(pv1)
        is_white = board.turn == chess.WHITE
        win1 = 60.0 if is_white else 40.0
        win2 = 48.0 if is_white else 52.0
        return [
            {"pv": pv1, "eval": {"cp": 30.0, "mate": None}, "win": win1, "multipv": 1},
            {"pv": pv2, "eval": {"cp": 10.0, "mate": None}, "win": win2, "multipv": 2},
        ]


def _make_game(moves: list[str] | None = None) -> Game:
    return Game(
        id="fake-themes",
        rated=True,
        speed="rapid",
        created_at=0,
        status="win",
        winner="white",
        white={"name": "me"},
        black={"name": "opponent"},
        opening="Synthetic Opening",
        eco="A00",
        moves=list(moves or ["e4", "e5", "Nf3", "Nc6"]),
        clocks=[],
        user_color="white",
        opponent="opponent",
        result_for_user="win",
    )


class AnalyzerThemesWiringTests(unittest.TestCase):
    def test_summary_moves_contain_themes(self) -> None:
        game = _make_game()
        analysis = FakeEngine().analyze_game(game)
        self.assertEqual(len(analysis.moves), len(game.moves))
        summary = analysis.summary()
        self.assertEqual(len(summary["moves"]), len(game.moves))
        for move in summary["moves"]:
            self.assertIn("themes", move)
            themes = move["themes"]
            self.assertIsInstance(themes, dict)
            for key in (
                "theme_opportunities",
                "theme_used",
                "confidence",
                "drop",
                "evidence",
                "note",
            ):
                self.assertIn(key, themes)
            self.assertIsInstance(themes["theme_opportunities"], list)
            self.assertIsInstance(themes["theme_used"], list)

    def test_old_schema_themes_none_omitted_from_to_dict(self) -> None:
        old = MoveAnalysis(
            san="e4",
            before=Eval(cp=10.0),
            after=Eval(cp=0.0),
            win_before=50.0,
            win_after=50.0,
            drop=0.0,
            classification="good",
            themes=None,
        )
        self.assertNotIn("themes", old.to_dict())
        # Пустой dict — не старый кеш: ключ должен остаться.
        new = MoveAnalysis(
            san="e4",
            before=Eval(cp=10.0),
            after=Eval(cp=0.0),
            win_before=50.0,
            win_after=50.0,
            drop=0.0,
            classification="good",
            themes={"theme_opportunities": ["positional"]},
        )
        self.assertIn("themes", new.to_dict())

    def test_classifier_exception_does_not_break_analysis(self) -> None:
        """Модуль тем падает — анализ завершается без ``themes`` (как старый кеш)."""
        game = _make_game(["e4", "e5"])
        with patch(
            "app.analyzer.classify_move_themes",
            side_effect=RuntimeError("synthetic theme failure"),
        ):
            analysis = FakeEngine().analyze_game(game)
        self.assertEqual(len(analysis.moves), 2)
        for move in analysis.moves:
            self.assertIsNone(move.themes)
            self.assertNotIn("themes", move.to_dict())
        for move in analysis.summary()["moves"]:
            self.assertNotIn("themes", move)

    def test_illegal_game_move_truncates_without_crashing(self) -> None:
        """Нелегальный ход в записи — не падение, остаток просто не анализируется."""
        game = _make_game(["e4", "e5", "Zz9"])
        analysis = FakeEngine().analyze_game(game)
        self.assertEqual([move.san for move in analysis.moves], ["e4", "e5"])
        for move in analysis.moves:
            self.assertIsNotNone(move.themes)


if __name__ == "__main__":
    unittest.main()
