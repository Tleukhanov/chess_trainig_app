"""Тесты LLM-тренера (mentor): построение запроса и CLI-обвязка."""

from __future__ import annotations

import unittest

from app.games import Game
from app.llm import ChatMessage
from app.mentor import build_mentor_request, format_mentor_reply
from app.plan import build_plan


def _mk_game(
    game_id: str,
    moves: list[str],
    color: str,
    result: str,
    *,
    opening: str = "Test Opening",
) -> Game:
    player = {"user": {"name": "tester"}} if color == "white" else {"user": {"name": "opponent"}}
    return Game(
        id=game_id,
        white=player,
        black=player,
        opening=opening or "",
        eco="",
        moves=moves,
        user_color=color,
        opponent="opponent" if color == "white" else "tester",
        result_for_user=result,
        rated=False,
    )


def _mkanalysis(color: str, acpl: float, move_records: list[tuple]) -> dict:
    moves, blunders, mistakes, missed = [], [], [], []
    for i, (san, cls, *rest) in enumerate(move_records):
        drop = rest[0] if rest else 0.0
        moves.append({"san": san, "classification": cls, "drop": drop})
    return {
        "user_color": color,
        "acpl": acpl,
        "accuracy": 80.0,
        "blunders": blunders,
        "mistakes": mistakes,
        "missed_wins": missed,
        "moves": moves,
    }


def fixture_pairs() -> list[tuple[Game, dict]]:
    w1 = _mk_game("a", ["e4", "e5", "Nf3", "Nc6"], "white", "win", opening="Sicilian Defense")
    a1 = _mkanalysis(
        "white", 10.0, [("e4", "good"), ("e5", "best"), ("Nf3", "mistake", 15.0), ("Nc6", "best")]
    )
    w2 = _mk_game("b", ["d4", "d5", "c4", "e6"], "white", "loss", opening="Queen's Gambit")
    a2 = _mkanalysis(
        "white", 20.0, [("d4", "good"), ("d5", "best"), ("c4", "blunder", 17.0), ("e6", "best")]
    )
    return [(w1, a1), (w2, a2)]

_EXTRA = "Хочу усилить эндшпиль, занимаюсь по 30 минут в день."


def _plan(**kwargs) -> dict:
    return build_plan(fixture_pairs(), **kwargs)


class MentorRequestTests(unittest.TestCase):
    def test_two_messages(self) -> None:
        messages = build_mentor_request(_plan())
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], ChatMessage)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")

    def test_user_message_includes_facts(self) -> None:
        _, user = build_mentor_request(_plan())
        text = user.content
        self.assertIn("ДЕБЮТНЫЙ РЕПЕРТУАР", text)
        self.assertIn("СЛАБЫЕ ДЕБЮТЫ", text)
        self.assertIn("УЗОРЫ ОШИБОК", text)
        self.assertIn("ФАЗЫ", text)
        self.assertIn("ЧЕЛОВЕЧНОСТЬ", text)
        self.assertIn("ДРЕЛИ", text)

    def test_extra_notes_are_included(self) -> None:
        _, user = build_mentor_request(_plan(), extra=_EXTRA)
        self.assertIn(_EXTRA, user.content)
        self.assertIn("ОСОБЫЕ ПОЖЕЛАНИЯ ИГРОКА", user.content)

    def test_no_extra_notes_no_section(self) -> None:
        _, user = build_mentor_request(_plan(), extra="")
        self.assertNotIn("ОСОБЫЕ ПОЖЕЛАНИЯ", user.content)

    def test_wraps_white_black_colors(self) -> None:
        _, user = build_mentor_request(_plan())
        self.assertIn("Белые:", user.content)
        self.assertIn("Чёрные:", user.content)

    def test_empty_plan_is_graceful(self) -> None:
        plan = build_plan([])
        messages = build_mentor_request(plan, extra="")
        _, user = messages
        self.assertIn("нет данных", user.content)


class MentorFormatTests(unittest.TestCase):
    def test_dedents(self) -> None:
        self.assertEqual(format_mentor_reply("  Текст\n    абзац"), "Текст\n  абзац")


if __name__ == "__main__":
    unittest.main()