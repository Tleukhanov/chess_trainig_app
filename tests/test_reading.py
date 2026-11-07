"""Модульные тесты доменного модуля «Моя система» (app/reading.py).

Офлайн: движок не поднимается, БД и сеть не используются. Временные
бандлы пишутся во временные каталоги и удаляются сами.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import chess

from app import reading
from app.reading import (
    COURSE,
    COURSE_SOURCE,
    MAX_PLAN_CHARS,
    build_progress,
    create_session,
    get_chapter_meta,
    get_course_meta,
    judge_answer,
    lesson_payload,
    list_chapters,
    load_course,
    normalize_move,
    normalize_san,
    pick_next_lesson,
    revealed_payload,
    same_san,
    validate_plan,
)


def _bundle_dict() -> dict:
    return json.loads(COURSE_SOURCE.read_text(encoding="utf-8"))


def _write_bundle(directory: str, data) -> Path:
    path = Path(directory) / "bundle.json"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _lesson(lesson_id: str):
    for chapter in COURSE.chapters:
        for lesson in chapter.lessons:
            if lesson.id == lesson_id:
                return lesson
    raise AssertionError(f"lesson {lesson_id} not found in COURSE")


def _chapter(chapter_id: str):
    for chapter in COURSE.chapters:
        if chapter.id == chapter_id:
            return chapter
    raise AssertionError(f"chapter {chapter_id} not found in COURSE")


def _attempt(lesson_id: str, verdict: str, created_at: int, chapter_id: str | None = None) -> dict:
    if chapter_id is None:
        chapter_id = next(
            chapter.id
            for chapter in COURSE.chapters
            for lesson in chapter.lessons
            if lesson.id == lesson_id
        )
    return {
        "id": f"{lesson_id}-{created_at}",
        "session_id": "s",
        "player": "tester",
        "course_id": COURSE.id,
        "chapter_id": chapter_id,
        "lesson_id": lesson_id,
        "verdict": verdict,
        "created_at": created_at,
    }


class LoadCourseTests(unittest.TestCase):
    def test_load_course_ok(self) -> None:
        course = load_course()
        self.assertEqual(course, COURSE)  # равенство по всем полям dataclass'а
        self.assertEqual(course.id, "my_system")
        self.assertEqual(course.version, 1)
        self.assertEqual(len(course.chapters), 6)
        self.assertEqual(sum(len(ch.lessons) for ch in course.chapters), 18)
        self.assertEqual([ch.order for ch in course.chapters], [1, 2, 3, 4, 5, 6])

    def test_module_exposes_course_on_import(self) -> None:
        self.assertEqual(COURSE.id, "my_system")

    def test_missing_file_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with self.assertRaises(RuntimeError) as ctx:
                load_course(missing)
            self.assertIn("not found", str(ctx.exception))
            self.assertIn(str(missing), str(ctx.exception))

    def test_invalid_json_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, "{ not valid json")
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_top_level_keys_rejected(self) -> None:
        bundle = _bundle_dict()
        del bundle["chapters"]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("top-level keys", str(ctx.exception))

    def test_duplicate_lesson_id_rejected(self) -> None:
        bundle = _bundle_dict()
        bundle["chapters"][1]["lessons"][0]["id"] = bundle["chapters"][0]["lessons"][0]["id"]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("duplicate lesson id", str(ctx.exception))

    def test_duplicate_fen_rejected(self) -> None:
        bundle = _bundle_dict()
        bundle["chapters"][1]["lessons"][0]["fen"] = bundle["chapters"][0]["lessons"][0]["fen"]
        bundle["chapters"][1]["lessons"][0]["side"] = bundle["chapters"][0]["lessons"][0]["side"]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("duplicate FEN", str(ctx.exception))

    def test_garbage_fen_rejected(self) -> None:
        bundle = _bundle_dict()
        bundle["chapters"][0]["lessons"][0]["fen"] = "definitely not a fen"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("invalid FEN", str(ctx.exception))

    def test_illegal_position_rejected(self) -> None:
        bundle = _bundle_dict()
        # Нет королей — python-chess считает позицию невалидной.
        bundle["chapters"][0]["lessons"][0]["fen"] = "8/8/8/8/8/8/8/8 w - - 0 1"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("illegal position", str(ctx.exception))

    def test_side_mismatch_rejected(self) -> None:
        bundle = _bundle_dict()
        lesson = bundle["chapters"][0]["lessons"][0]
        lesson["side"] = "black" if lesson["side"] == "white" else "white"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("side mismatch", str(ctx.exception))

    def test_best_move_must_be_accepted(self) -> None:
        bundle = _bundle_dict()
        lesson = bundle["chapters"][0]["lessons"][0]
        lesson["best_move"] = "a3"  # легально поле-не-принято? неважно: главное — не из accepted_moves
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("best_move", str(ctx.exception))

    def test_illegal_accepted_move_rejected(self) -> None:
        bundle = _bundle_dict()
        lesson = bundle["chapters"][0]["lessons"][0]
        lesson["accepted_moves"] = ["Ke9"]
        lesson["best_move"] = "Ke9"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("accepted move", str(ctx.exception))

    def test_illegal_best_line_rejected(self) -> None:
        bundle = _bundle_dict()
        lesson = bundle["chapters"][0]["lessons"][0]
        lesson["best_line"] = ["Ke9"]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("best_line", str(ctx.exception))

    def test_invalid_engine_depth_rejected(self) -> None:
        bundle = _bundle_dict()
        bundle["chapters"][0]["lessons"][0]["engine"]["depth"] = "deep"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("engine.depth", str(ctx.exception))


class BestLineValidationTests(unittest.TestCase):
    """Валидация ``best_line`` из 4445f0b: линия — после best_move."""

    def test_empty_best_line_only_for_mate_in_one(self) -> None:
        # rank_02: мат в один ход, пустая линия допустима.
        lesson = _lesson("rank_02")
        self.assertEqual(lesson.best_line, ())
        board = chess.Board(lesson.fen)
        board.push_san(lesson.best_move)
        self.assertTrue(board.is_checkmate(), "rank_02 должен быть матом в один ход")
        # Сам бандл уже проверен при импорте: пустая линия мата не отвергнута.
        course = load_course()
        self.assertEqual(course, COURSE)

    def test_non_mate_empty_best_line_rejected(self) -> None:
        bundle = _bundle_dict()
        # center_01: best_move Rad1 не мат — пустая линия недопустима.
        lesson = next(
            item
            for chapter in bundle["chapters"]
            for item in chapter["lessons"]
            if item["id"] == "center_01"
        )
        board = chess.Board(lesson["fen"])
        board.push_san(lesson["best_move"])
        self.assertFalse(board.is_checkmate())
        lesson["best_line"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("best_line must be non-empty unless best_move is mate", str(ctx.exception))

    def test_illegal_continuation_after_push_rejected(self) -> None:
        # Rfd1 легальна ДО Rad1, но нелегальна ПОСЛЕ (ход чёрных) —
        # именно это отличает проверку продолжения после push.
        bundle = _bundle_dict()
        lesson = next(
            item
            for chapter in bundle["chapters"]
            for item in chapter["lessons"]
            if item["id"] == "center_01"
        )
        before = chess.Board(lesson["fen"])
        before.parse_san("Rfd1")  # легальна до best_move
        lesson["best_line"] = ["Rfd1"]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bundle(tmp, bundle)
            with self.assertRaises(RuntimeError) as ctx:
                load_course(path)
            self.assertIn("best_line move #1", str(ctx.exception))
            self.assertIn("is not legal", str(ctx.exception))

    def test_first_continuation_move_checked_in_position_after_best_move(self) -> None:
        # Ne4 — первый ход существующей линии center_01: он легален
        # именно после Rad1 (чёрные), и бандл с ним валиден по построению.
        lesson = _lesson("center_01")
        self.assertEqual(lesson.best_line[0], "Ne4")
        board = chess.Board(lesson.fen)
        board.push_san(lesson.best_move)
        board.parse_san(lesson.best_line[0])  # не бросает


class CourseIntegrityTests(unittest.TestCase):
    def test_ids_unique_everywhere(self) -> None:
        lesson_ids = [lesson.id for ch in COURSE.chapters for lesson in ch.lessons]
        chapter_ids = [ch.id for ch in COURSE.chapters]
        self.assertEqual(len(lesson_ids), len(set(lesson_ids)))
        self.assertEqual(len(chapter_ids), len(set(chapter_ids)))

    def test_fens_unique_and_side_matches_turn(self) -> None:
        fens = [lesson.fen for ch in COURSE.chapters for lesson in ch.lessons]
        self.assertEqual(len(fens), len(set(fens)))
        for lesson in (l for ch in COURSE.chapters for l in ch.lessons):
            board = chess.Board(lesson.fen)
            self.assertTrue(board.is_valid(), lesson.id)
            expected = chess.WHITE if lesson.side == "white" else chess.BLACK
            self.assertEqual(board.turn, expected, lesson.id)

    def test_accepted_moves_legal_and_best_accepted(self) -> None:
        for lesson in (l for ch in COURSE.chapters for l in ch.lessons):
            board = chess.Board(lesson.fen)
            for san in lesson.accepted_moves:
                board.parse_san(san)  # бросит ValueError, если нелегально
            self.assertIn(lesson.best_move, lesson.accepted_moves)


class ProgressTests(unittest.TestCase):
    def test_empty_progress(self) -> None:
        chapters = list_chapters(COURSE, None)
        self.assertEqual(len(chapters), 6)
        for item in chapters:
            self.assertEqual(item["completed"], 0)
            self.assertEqual(item["lessons_total"], 3)
            self.assertIsNone(item["accuracy"])
            self.assertEqual(item["correct_first_attempt"], 0)
            self.assertEqual(item["status"], "not_started")

    def test_partial_progress(self) -> None:
        attempts = [
            _attempt("center_01", "correct", 1000),
            _attempt("center_02", "incorrect", 2000),
        ]
        chapters = {item["chapter_id"]: item for item in list_chapters(COURSE, attempts)}
        center = chapters["center"]
        self.assertEqual(center["completed"], 1)
        self.assertEqual(center["status"], "in_progress")
        self.assertEqual(center["accuracy"], 0.5)
        self.assertEqual(center["correct_first_attempt"], 1)
        pawns = chapters["pawns"]
        self.assertEqual(pawns["completed"], 0)
        self.assertEqual(pawns["status"], "not_started")

    def test_completed_chapter(self) -> None:
        attempts = [
            _attempt("center_01", "correct", 1000),
            _attempt("center_02", "acceptable", 2000),
            _attempt("center_03", "incorrect", 3000),
            _attempt("center_03", "correct", 4000),
        ]
        chapters = {item["chapter_id"]: item for item in list_chapters(COURSE, attempts)}
        center = chapters["center"]
        self.assertEqual(center["completed"], 3)
        self.assertEqual(center["status"], "completed")
        # 3 проходных из 4 попыток
        self.assertEqual(center["accuracy"], 0.75)
        # первых попыток correct: center_01 да, center_02 (acceptable) нет, center_03 нет
        self.assertEqual(center["correct_first_attempt"], 1)

    def test_first_attempt_uses_earliest_created_at(self) -> None:
        attempts = [
            _attempt("center_01", "incorrect", 5000),
            _attempt("center_01", "correct", 1000),
        ]
        chapters = {item["chapter_id"]: item for item in list_chapters(COURSE, attempts)}
        self.assertEqual(chapters["center"]["correct_first_attempt"], 1)

    def test_build_progress_accepts_none_and_rows(self) -> None:
        self.assertEqual(build_progress(None).attempts, ())
        rows = [_attempt("center_01", "correct", 1000), "мусор"]
        progress = build_progress(rows)
        self.assertEqual(len(progress.attempts), 1)


class PayloadTests(unittest.TestCase):
    def test_lesson_payload_hides_solution(self) -> None:
        lesson = _lesson("center_01")
        payload = lesson_payload(lesson)
        dumped = json.dumps(payload, ensure_ascii=False)
        for secret in ("best_move", "best_line", "why", "concepts", "accepted_moves", "engine"):
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, dumped)
        self.assertNotIn(lesson.best_move, dumped)

    def test_revealed_payload_includes_solution(self) -> None:
        lesson = _lesson("center_01")
        revealed = revealed_payload(lesson)
        self.assertEqual(revealed["best_move"], lesson.best_move)
        self.assertEqual(revealed["accepted_moves"], list(lesson.accepted_moves))
        self.assertEqual(revealed["best_line"], list(lesson.best_line))
        self.assertTrue(revealed["why"])
        self.assertEqual(revealed["concepts"], list(lesson.concepts))


class JudgeAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lesson = _lesson("center_01")  # accepted: Rad1 (best), Rfd1

    def test_correct_by_san(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Rad1")
        self.assertEqual(result["verdict"], "correct")
        self.assertEqual(result["judge_kind"], "curated")
        self.assertTrue(result["move"]["legal"])

    def test_correct_by_uci(self) -> None:
        result = judge_answer(COURSE, self.lesson, "a1d1")
        self.assertEqual(result["verdict"], "correct")
        self.assertEqual(result["move"]["played_san"], "Rad1")

    def test_acceptable_alternative(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Rfd1")
        self.assertEqual(result["verdict"], "acceptable")

    def test_san_suffixes_are_ignored(self) -> None:
        # Шах/мат в SAN не должен ломать сравнение (rank_02: Qd8#).
        lesson = _lesson("rank_02")
        result = judge_answer(COURSE, lesson, "Qd8")
        self.assertEqual(result["verdict"], "correct")

    def test_incorrect_legal_move(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Qd2")
        self.assertEqual(result["verdict"], "incorrect")
        self.assertEqual(result["judge_kind"], "curated")

    def test_invalid_garbage_move(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Zz9")
        self.assertEqual(result["verdict"], "invalid")
        self.assertIsNone(result["judge_kind"])
        self.assertEqual(result["reason"], "unparseable_move")
        self.assertNotIn("accepted_moves", result)

    def test_invalid_wrong_side_uci(self) -> None:
        # Ход чёрных, когда ход белых: не входит в legal_moves.
        result = judge_answer(COURSE, self.lesson, "g8f6")
        self.assertEqual(result["verdict"], "invalid")

    def test_invalid_move_not_of_player_color(self) -> None:
        # UCI чёрной ладьи при ходе белых.
        result = judge_answer(COURSE, self.lesson, "a8d8")
        self.assertEqual(result["verdict"], "invalid")

    def test_plan_too_long_rejected(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Rad1", plan="x" * (MAX_PLAN_CHARS + 1))
        self.assertEqual(result["verdict"], "invalid")
        self.assertEqual(result["reason"], "plan_too_long")

    def test_plan_at_limit_accepted(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Rad1", plan="x" * MAX_PLAN_CHARS)
        self.assertEqual(result["verdict"], "correct")
        self.assertEqual(len(result["plan"]), MAX_PLAN_CHARS)

    def test_validate_plan_strips_and_limits(self) -> None:
        self.assertEqual(validate_plan("  идея  "), "идея")
        self.assertEqual(validate_plan(""), "")
        self.assertEqual(validate_plan(None), "")
        with self.assertRaises(ValueError):
            validate_plan("я" * (MAX_PLAN_CHARS + 1))

    def test_lesson_from_other_course_rejected(self) -> None:
        from dataclasses import replace

        other_lesson = replace(self.lesson, why="подмена")  # то же id, другой контент
        with self.assertRaises(ValueError):
            judge_answer(COURSE, other_lesson, "Rad1")

    def test_solution_not_in_judge_result(self) -> None:
        result = judge_answer(COURSE, self.lesson, "Qd2")
        self.assertNotIn("best_move", result)
        self.assertNotIn("why", result)
        self.assertNotIn("best_line", result)


class MoveParsingTests(unittest.TestCase):
    def test_normalize_move_returns_canonical_san(self) -> None:
        board = chess.Board(_lesson("center_01").fen)
        self.assertEqual(normalize_move(board, "Rad1"), "Rad1")
        self.assertEqual(normalize_move(board, "a1d1"), "Rad1")
        self.assertIsNone(normalize_move(board, "не ход"))

    def test_normalize_san_and_same_san(self) -> None:
        self.assertEqual(normalize_san("Qd8#"), "Qd8")
        self.assertEqual(normalize_san(" h6+ "), "h6")
        self.assertTrue(same_san("Qd8#", "Qd8"))
        self.assertTrue(same_san("h6+", "h6"))
        self.assertFalse(same_san("Rad1", "Rfd1"))
        self.assertFalse(same_san(None, "h6"))


class MetaAndSessionTests(unittest.TestCase):
    def test_get_course_meta_has_no_solutions(self) -> None:
        meta = get_course_meta(COURSE)
        dumped = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn("best_move", dumped)
        self.assertNotIn("best_line", dumped)
        self.assertNotIn("why", dumped)
        self.assertEqual(meta["course"]["id"], "my_system")
        self.assertEqual(len(meta["chapters"]), 6)
        self.assertTrue(meta["source"]["url"].startswith("https://"))

    def test_get_chapter_meta_reports_progress(self) -> None:
        attempts = [_attempt("center_01", "correct", 1000)]
        meta = get_chapter_meta(COURSE, attempts, "center")
        self.assertEqual(meta["progress"]["completed"], 1)
        self.assertEqual(meta["progress"]["lessons_total"], 3)
        self.assertEqual(len(meta["lessons"]), 3)
        by_id = {item["lesson_id"]: item for item in meta["lessons"]}
        self.assertEqual(by_id["center_01"]["status"], "completed")
        self.assertEqual(by_id["center_02"]["status"], "not_started")

    def test_get_chapter_meta_unknown_chapter(self) -> None:
        with self.assertRaises(KeyError):
            get_chapter_meta(COURSE, None, "no-such-chapter")

    def test_list_chapters_sorted_by_order(self) -> None:
        chapters = list_chapters(COURSE, None)
        self.assertEqual([item["order"] for item in chapters], [1, 2, 3, 4, 5, 6])

    def test_pick_next_lesson_empty_progress(self) -> None:
        lesson = pick_next_lesson(COURSE, None, "center")
        self.assertEqual(lesson.id, "center_01")

    def test_pick_next_lesson_skips_completed(self) -> None:
        attempts = [_attempt("center_01", "correct", 1000)]
        lesson = pick_next_lesson(COURSE, attempts, "center")
        self.assertEqual(lesson.id, "center_02")

    def test_pick_next_lesson_all_completed_returns_first(self) -> None:
        attempts = [
            _attempt("center_01", "correct", 1000),
            _attempt("center_02", "acceptable", 2000),
            _attempt("center_03", "correct", 3000),
        ]
        lesson = pick_next_lesson(COURSE, attempts, "center")
        self.assertEqual(lesson.id, "center_01")

    def test_pick_next_lesson_unknown_chapter(self) -> None:
        with self.assertRaises(KeyError):
            pick_next_lesson(COURSE, None, "no-such-chapter")

    def test_create_session_without_solution(self) -> None:
        session = create_session(COURSE, "tester", "center", progress=None)
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["chapter_id"], "center")
        self.assertEqual(session["lesson_id"], "center_01")
        self.assertEqual(session["course_version"], COURSE.version)
        dumped = json.dumps(session["lesson"], ensure_ascii=False)
        self.assertNotIn("best_move", dumped)
        self.assertNotIn("why", dumped)

    def test_create_session_with_explicit_lesson(self) -> None:
        session = create_session(COURSE, "tester", "center", lesson_id="center_03")
        self.assertEqual(session["lesson_id"], "center_03")

    def test_create_session_unknown_lesson(self) -> None:
        with self.assertRaises(KeyError):
            create_session(COURSE, "tester", "center", lesson_id="nope")

    def test_create_session_unknown_chapter(self) -> None:
        with self.assertRaises(KeyError):
            create_session(COURSE, "tester", "no-such-chapter")

    def test_create_session_empty_player(self) -> None:
        with self.assertRaises(ValueError):
            create_session(COURSE, "   ", "center")


if __name__ == "__main__":
    unittest.main()
