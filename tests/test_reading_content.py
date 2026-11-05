"""Строгий валидатор читательского бандла «Моя система» (assets/reading/my_system.json).

Проверяет структуру курса, легальность позиций и соответствие ответов
шахматным правилам. Движок не поднимается — только python-chess.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import date
from pathlib import Path

import chess

_BUNDLE_PATH = Path(__file__).resolve().parent.parent / "assets" / "reading" / "my_system.json"
_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_LESSON_KEYS = {
    "id", "type", "title", "text", "fen", "side", "question", "plan_prompt",
    "accepted_moves", "best_move", "best_line", "concepts", "why",
    "difficulty", "engine",
}
_CHAPTER_KEYS = {"id", "title", "summary", "order", "lessons"}
_COURSE_KEYS = {"id", "title", "version", "source", "chapters"}
_ENGINE_KEYS = {"profile", "depth", "validated_at"}
_SOURCE_KEYS = {"title", "url", "license_note"}
_FORBIDDEN_TOKENS = ("todo", "lorem", "xxx", "fixme", "placeholder")


def _load_bundle() -> dict:
    with _BUNDLE_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, dict), "course bundle must be a JSON object"
    return data


def _chapters() -> list[dict]:
    return _load_bundle()["chapters"]


def _lessons() -> list[dict]:
    return [lesson for chapter in _chapters() for lesson in chapter["lessons"]]


def _assert_clean_text(value: str, where: str) -> None:
    assert value.strip(), f"{where}: empty string"
    lowered = value.lower()
    for token in _FORBIDDEN_TOKENS:
        assert token not in lowered, f"{where}: forbidden token {token!r}"


class CourseShapeTests(unittest.TestCase):
    def test_course_top_level_keys(self) -> None:
        bundle = _load_bundle()
        self.assertEqual(set(bundle), _COURSE_KEYS, "course must have exactly the planned keys")
        self.assertEqual(bundle["id"], "my_system")
        self.assertEqual(bundle["version"], 1)
        self.assertIn("Моя система", bundle["title"])

    def test_source_block(self) -> None:
        source = _load_bundle()["source"]
        self.assertEqual(set(source), _SOURCE_KEYS)
        self.assertTrue(source["title"].strip())
        self.assertTrue(source["url"].startswith("https://"))
        self.assertGreaterEqual(len(source["license_note"].strip()), 20)

    def test_exactly_six_chapters_in_order(self) -> None:
        chapters = _chapters()
        self.assertEqual(len(chapters), 6)
        self.assertEqual([chapter["order"] for chapter in chapters], [1, 2, 3, 4, 5, 6])
        orders = [chapter["order"] for chapter in chapters]
        self.assertEqual(len(set(orders)), 6, "chapter order values must be unique")

    def test_chapter_ids_unique_and_kebab_case(self) -> None:
        ids = [chapter["id"] for chapter in _chapters()]
        self.assertEqual(len(set(ids)), 6)
        for chapter_id in ids:
            self.assertRegex(chapter_id, _ID_RE)

    def test_chapter_keys_and_summary(self) -> None:
        for chapter in _chapters():
            self.assertEqual(set(chapter), _CHAPTER_KEYS, f"chapter {chapter.get('id')}: unexpected keys")
            self.assertRegex(chapter["id"], _ID_RE)
            self.assertTrue(chapter["title"].strip())
            self.assertGreaterEqual(len(chapter["summary"].strip()), 20)
            self.assertIsInstance(chapter["lessons"], list)

    def test_three_lessons_per_chapter_eighteen_total(self) -> None:
        chapters = _chapters()
        self.assertEqual([len(chapter["lessons"]) for chapter in chapters], [3, 3, 3, 3, 3, 3])
        self.assertEqual(len(_lessons()), 18)

    def test_lesson_ids_unique(self) -> None:
        ids = [lesson["id"] for lesson in _lessons()]
        self.assertEqual(len(set(ids)), 18, "lesson ids must be unique across the course")
        for lesson_id in ids:
            self.assertRegex(lesson_id, _ID_RE)

    def test_every_chapter_has_a_challenge_lesson(self) -> None:
        for chapter in _chapters():
            challenges = [
                lesson for lesson in chapter["lessons"]
                if "challenge" in [str(c).lower() for c in lesson["concepts"]]
            ]
            self.assertTrue(challenges, f"chapter {chapter['id']}: no challenge/check lesson")


class LessonShapeTests(unittest.TestCase):
    def test_lesson_keys_exact(self) -> None:
        for lesson in _lessons():
            self.assertEqual(
                set(lesson), _LESSON_KEYS,
                f"lesson {lesson.get('id')}: keys must match the plan exactly",
            )

    def test_lesson_types_and_sides(self) -> None:
        for lesson in _lessons():
            self.assertEqual(lesson["type"], "move_and_plan", lesson["id"])
            self.assertIn(lesson["side"], ("white", "black"), lesson["id"])

    def test_lesson_texts_present(self) -> None:
        for lesson in _lessons():
            where = f"lesson {lesson['id']}"
            self.assertTrue(lesson["title"].strip(), where)
            _assert_clean_text(lesson["text"], f"{where}.text")
            _assert_clean_text(lesson["question"], f"{where}.question")
            _assert_clean_text(lesson["plan_prompt"], f"{where}.plan_prompt")
            _assert_clean_text(lesson["why"], f"{where}.why")
            self.assertLessEqual(len(lesson["text"]), 800, f"{where}.text: one screen max")

    def test_difficulty_range_and_type(self) -> None:
        for lesson in _lessons():
            self.assertIsInstance(lesson["difficulty"], int, lesson["id"])
            self.assertTrue(1 <= lesson["difficulty"] <= 5, lesson["id"])

    def test_concepts_nonempty_unique_lower(self) -> None:
        for lesson in _lessons():
            concepts = lesson["concepts"]
            self.assertIsInstance(concepts, list, lesson["id"])
            self.assertTrue(concepts, f"lesson {lesson['id']}: concepts must not be empty")
            self.assertEqual(len(concepts), len(set(concepts)), f"lesson {lesson['id']}: duplicate concepts")
            for concept in concepts:
                self.assertIsInstance(concept, str, lesson["id"])
                self.assertRegex(concept, r"^[a-z0-9_]{2,40}$")

    def test_best_move_is_in_accepted_moves(self) -> None:
        for lesson in _lessons():
            self.assertIn(lesson["best_move"], lesson["accepted_moves"], lesson["id"])

    def test_accepted_moves_unique(self) -> None:
        for lesson in _lessons():
            moves = lesson["accepted_moves"]
            self.assertEqual(len(moves), len(set(moves)), f"lesson {lesson['id']}: duplicate accepted moves")

    def test_engine_block(self) -> None:
        for lesson in _lessons():
            engine = lesson["engine"]
            self.assertEqual(set(engine), _ENGINE_KEYS, lesson["id"])
            self.assertEqual(engine["profile"], "standard", lesson["id"])
            self.assertIsInstance(engine["depth"], int, lesson["id"])
            self.assertEqual(engine["depth"], 14, lesson["id"])
            parsed = date.fromisoformat(engine["validated_at"])
            self.assertEqual(parsed.isoformat(), engine["validated_at"], lesson["id"])


class ChessRulesTests(unittest.TestCase):
    def test_fen_valid_and_side_matches(self) -> None:
        for lesson in _lessons():
            board = chess.Board(lesson["fen"])
            self.assertTrue(board.is_valid(), f"lesson {lesson['id']}: illegal FEN ({board.status()})")
            expected = chess.WHITE if lesson["side"] == "white" else chess.BLACK
            self.assertEqual(board.turn, expected, f"lesson {lesson['id']}: side does not match FEN turn")

    def test_fens_are_unique(self) -> None:
        fens = [lesson["fen"] for lesson in _lessons()]
        self.assertEqual(len(fens), len(set(fens)), "duplicate FEN positions in course")

    def test_accepted_moves_are_legal(self) -> None:
        for lesson in _lessons():
            board = chess.Board(lesson["fen"])
            for san in lesson["accepted_moves"]:
                try:
                    move = board.parse_san(san)
                except ValueError:
                    self.fail(f"lesson {lesson['id']}: accepted_moves entry {san!r} is not a legal move")
                self.assertEqual(board.color_at(move.from_square), board.turn, lesson["id"])

    def test_best_line_is_legal_and_has_no_capture_symbols(self) -> None:
        for lesson in _lessons():
            line = lesson["best_line"]
            self.assertIsInstance(line, list, lesson["id"])
            self.assertTrue(line, f"lesson {lesson['id']}: best_line must not be empty")
            board = chess.Board(lesson["fen"])
            for san in line:
                self.assertIsInstance(san, str, lesson["id"])
                self.assertNotIn("x", san, f"lesson {lesson['id']}: capture symbol 'x' in best_line ({san!r})")
                self.assertNotIn(":", san, f"lesson {lesson['id']}: capture symbol ':' in best_line ({san!r})")
                try:
                    move = board.parse_san(san)
                except ValueError:
                    self.fail(f"lesson {lesson['id']}: best_line move {san!r} is not legal")
                board.push(move)

    def test_positions_are_not_immediately_decided(self) -> None:
        """Ни одна позиция не должна быть матом/патом до хода ученика."""
        for lesson in _lessons():
            board = chess.Board(lesson["fen"])
            self.assertFalse(board.is_checkmate(), f"lesson {lesson['id']}: already checkmate")
            self.assertFalse(board.is_stalemate(), f"lesson {lesson['id']}: already stalemate")
            self.assertFalse(board.is_insufficient_material(), f"lesson {lesson['id']}: insufficient material")


if __name__ == "__main__":
    unittest.main()
