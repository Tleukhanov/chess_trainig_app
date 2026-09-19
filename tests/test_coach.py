"""Тесты LLM-коуча: отбор ключевых моментов, промпты и разбор ответа модели."""

from __future__ import annotations

import unittest

from app.coach import (
    SYSTEM_PROMPT,
    build_request,
    parse_game_answer,
    played_line,
    run_coach,
    select_key_moments,
)
from app.games import Game
from app.llm import ChatMessage


def _move(
    san: str,
    cls: str,
    drop: float,
    win_before: float | None = None,
    win_after: float | None = None,
    best: str | None = None,
    **extra,
) -> dict:
    """Запись хода в формате summary() анализа с необязательными best_line/played_line."""
    wb = win_before if win_before is not None else 60.0
    wa = win_after if win_after is not None else wb - drop
    entry = {
        "san": san,
        "before": {"cp": 0.0, "mate": None},
        "after": {"cp": 0.0, "mate": None},
        "win_before": wb,
        "win_after": wa,
        "drop": drop,
        "classification": cls,
        "best_move_san": best,
        "best_eval": None,
        "best_win": wb,
        "cp_loss": drop,
        "clock_used": None,
        "time_pressure": None,
    }
    entry.update(extra)
    return entry


def _synthetic_analysis() -> dict:
    """Анализ партии белыми: 7 ходов юзера на чётных полуходах (0..12)."""
    moves: list[dict] = []
    classifications = {
        0: ("good", 0.0),
        2: ("blunder", 30.0),
        4: ("best", 0.0),
        6: ("mistake", 15.0),
        8: ("inaccuracy", 7.0),
        10: ("blunder", 40.0),
        12: ("best", 0.0),
    }
    for ply in range(13):
        if ply % 2 == 0:
            cls, drop = classifications[ply]
            best = "Nf3" if ply == 2 else "Qh5" if ply == 10 else None
            moves.append(_move(f"m{ply}", cls, drop, 60.0 - ply, 60.0 - ply - drop, best))
        else:
            moves.append(_move(f"m{ply}", "good", 0.0))
    return {
        "game_id": "abc",
        "user_color": "white",
        "result_for_user": "win",
        "opponent": "nick",
        "acpl": 20.0,
        "accuracy": 80.0,
        "blunders": [2, 10],
        "mistakes": [6],
        "inaccuracies": [8],
        "missed_wins": [8],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def _clean_analysis() -> dict:
    """Анализ без проблемных ходов: отбор должен быть пустым."""
    moves = [_move(f"m{i}", "good" if i % 2 == 0 else "best", 0.0) for i in range(6)]
    return {
        "user_color": "white",
        "result_for_user": "draw",
        "acpl": 3.0,
        "accuracy": 96.0,
        "blunders": [],
        "mistakes": [],
        "inaccuracies": [],
        "missed_wins": [],
        "time_pressure_blunders": [],
        "moves": moves,
    }


def _game() -> Game:
    return Game(
        id="abc",
        white={"user": {"name": "alex"}, "rating": 2100},
        black={"name": "nick"},
        opening="Итальянская партия",
        moves=[f"m{i}" for i in range(13)],
        user_color="white",
        result_for_user="win",
        opponent="nick",
    )


class _FakeLLM:
    def __init__(self, response: str = "", error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[list[ChatMessage], dict]] = []

    def chat(self, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append((messages, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class SelectKeyMomentsTests(unittest.TestCase):
    def test_returns_problematic_moments_ascending(self) -> None:
        result = select_key_moments(_synthetic_analysis())
        self.assertEqual(result, [2, 6, 8, 10])

    def test_respects_limit_keeping_most_important(self) -> None:
        result = select_key_moments(_synthetic_analysis(), max_moments=2)
        self.assertEqual(result, [2, 10])

    def test_orders_by_importance(self) -> None:
        result = select_key_moments(_synthetic_analysis(), max_moments=3)
        self.assertEqual(result, [2, 6, 10])

    def test_only_problems_selected(self) -> None:
        result = select_key_moments(_synthetic_analysis())
        for bad in (0, 4, 12):
            self.assertNotIn(bad, result)

    def test_empty_when_nothing_problematic(self) -> None:
        self.assertEqual(select_key_moments(_clean_analysis()), [])


class PlayedLineTests(unittest.TestCase):
    def test_full_line_limited_to_four(self) -> None:
        game = Game(id="g", moves=["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "O-O"])
        self.assertEqual(played_line(game, 0), ["e5", "Nf3", "Nc6", "Bc4"])

    def test_short_at_game_end(self) -> None:
        game = Game(id="g", moves=["e4", "e5", "Nf3"])
        self.assertEqual(played_line(game, 1), ["Nf3"])
        self.assertEqual(played_line(game, 2), [])


class BuildRequestTests(unittest.TestCase):
    def test_messages_include_system_and_user(self) -> None:
        messages, idx = build_request(_game(), _synthetic_analysis())
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], ChatMessage)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].content, SYSTEM_PROMPT)
        self.assertEqual(messages[1].role, "user")
        self.assertEqual(idx, [2, 6, 8, 10])

    def test_user_content_has_required_parts(self) -> None:
        messages, idx = build_request(_game(), _synthetic_analysis())
        user = messages[1].content
        self.assertIn("сыграно", user)
        self.assertIn("Лучший ход", user)
        self.assertIn("полуход", user)
        self.assertIn("Партия", user)
        self.assertEqual(len(idx), 4)

    def test_missing_best_line_and_played_line_not_breaking(self) -> None:
        messages, _ = build_request(_game(), _synthetic_analysis())
        user = messages[1].content
        self.assertIn("В партии далее:", user)
        self.assertIn("Лучший ход:", user)

    def test_best_line_rendered_when_present(self) -> None:
        analysis = _synthetic_analysis()
        analysis["moves"][10]["best_line"] = ["Be3", "Nc6"]
        messages, idx = build_request(_game(), analysis)
        user = messages[1].content
        self.assertIn("линия: Be3 Nc6", user)
        self.assertEqual(len(idx), 4)

    def test_empty_selection_returns_empty(self) -> None:
        messages, idx = build_request(_game(), _clean_analysis())
        self.assertEqual(messages, [])
        self.assertEqual(idx, [])


class ParseGameAnswerTests(unittest.TestCase):
    def test_clean_json(self) -> None:
        text = '{"moments": {"2": "объяснение 2", "6": "объяснение 6"}, "summary": "итог"}'
        moments, summary = parse_game_answer(text, [2, 6])
        self.assertEqual(moments, {2: "объяснение 2", 6: "объяснение 6"})
        self.assertEqual(summary, "итог")

    def test_json_in_fences_with_garbage(self) -> None:
        text = (
            "тут какой-то текст\n"
            "```json\n"
            '{"moments": {"2": "объяснение"}, "summary": "итог"}\n'
            "```\n"
            "и ещё текст"
        )
        moments, summary = parse_game_answer(text, [2])
        self.assertEqual(moments, {2: "объяснение"})
        self.assertEqual(summary, "итог")

    def test_fallback_line_by_line(self) -> None:
        text = "Момент 1: зевнул ферзя.\n2) не увидел связку."
        moments, summary = parse_game_answer(text, [2, 6])
        self.assertEqual(moments, {2: "зевнул ферзя.", 6: "не увидел связку."})
        self.assertIsNone(summary)

    def test_partial_moments_filled_with_empty(self) -> None:
        text = '{"moments": {"6": "только это"}, "summary": "итог"}'
        moments, summary = parse_game_answer(text, [2, 6, 8])
        self.assertEqual(moments, {6: "только это", 2: "", 8: ""})
        self.assertEqual(summary, "итог")

    def test_no_json_and_no_lines_returns_empty(self) -> None:
        moments, summary = parse_game_answer("совсем без структуры", [2])
        self.assertEqual(moments, {})
        self.assertIsNone(summary)

    def test_moments_not_dict_returns_summary_only(self) -> None:
        text = '{"moments": [], "summary": "итог"}'
        moments, summary = parse_game_answer(text, [2])
        self.assertEqual(moments, {})
        self.assertEqual(summary, "итог")

    def test_empty_text_returns_empty(self) -> None:
        moments, summary = parse_game_answer("", [2])
        self.assertEqual(moments, {})
        self.assertIsNone(summary)


class RunCoachTests(unittest.TestCase):
    def test_returns_parsed_answer(self) -> None:
        fake = _FakeLLM(
            response='{"moments": {"2": "объяснение 2"}, "summary": "итог партии"}'
        )
        moments, summary = run_coach(_game(), _synthetic_analysis(), fake)
        self.assertEqual(
            moments, {2: "объяснение 2", 6: "", 8: "", 10: ""}
        )
        self.assertEqual(summary, "итог партии")

    def test_calls_chat_with_json_mode(self) -> None:
        fake = _FakeLLM(response='{"moments": {}, "summary": null}')
        run_coach(_game(), _synthetic_analysis(), fake)
        self.assertEqual(len(fake.calls), 1)
        messages, kwargs = fake.calls[0]
        self.assertEqual(len(messages), 2)
        self.assertIs(kwargs["json_mode"], True)
        self.assertEqual(kwargs["temperature"], 0.4)
        self.assertEqual(kwargs["max_tokens"], 1200)

    def test_no_moments_returns_empty_without_chat(self) -> None:
        fake = _FakeLLM()
        moments, summary = run_coach(_game(), _clean_analysis(), fake)
        self.assertEqual(moments, {})
        self.assertIsNone(summary)
        self.assertEqual(fake.calls, [])

    def test_llm_error_propagates(self) -> None:
        fake = _FakeLLM(error=RuntimeError("LLM упал"))
        with self.assertRaisesRegex(RuntimeError, "LLM упал"):
            run_coach(_game(), _synthetic_analysis(), fake)


if __name__ == "__main__":
    unittest.main()