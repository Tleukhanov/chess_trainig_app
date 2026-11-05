"""Доменный модуль режима «Моя система» (чтение + ход).

Чистая офлайн-логика без HTTP, БД и Stockfish:

* :data:`COURSE_SOURCE` — путь к контент-бандлу ``assets/reading/my_system.json``;
* :data:`COURSE` — курс, загруженный и строго провалидированный при импорте
  (если бандл отсутствует или невалиден — ``RuntimeError`` с точной причиной);
* типы :class:`Course`, :class:`Chapter`, :class:`ReadingLesson`, :class:`Progress`;
* публичный payload без решения (:func:`lesson_payload`) и раскрытие ответа
  (:func:`revealed_payload`) — только после отправки ответа игроком;
* судья :func:`judge_answer` — детерминированный, по заранее валидированным
  ``accepted_moves`` (MVP: без движка);
* расчёт прогресса главы из строк ``reading_attempts`` (передаются списком
  dict'ов или объектом :class:`Progress`), БД модуль не трогает.

Решение (лучший ход, линия, «почему») наружу до ответа не отдаётся:
:func:`lesson_payload` этих полей не содержит, :func:`revealed_payload`
вызывается только после проверки ответа.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import chess

COURSE_SOURCE = Path(__file__).resolve().parent.parent / "assets" / "reading" / "my_system.json"

#: Максимальная длина текстового плана игрока (в символах, после strip).
MAX_PLAN_CHARS = 600

#: Вердикты, которые засчитываются как «урок пройден».
PASSING_VERDICTS = frozenset({"correct", "acceptable"})

#: Все допустимые вердикты судьи.
VERDICTS = ("correct", "acceptable", "incorrect", "invalid")

#: Статусы главы и урока.
STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

_COURSE_KEYS = {"id", "title", "version", "source", "chapters"}
_SOURCE_KEYS = {"title", "url", "license_note"}
_CHAPTER_KEYS = {"id", "title", "summary", "order", "lessons"}
_LESSON_KEYS = {
    "id", "type", "title", "text", "fen", "side", "question", "plan_prompt",
    "accepted_moves", "best_move", "best_line", "concepts", "why",
    "difficulty", "engine",
}
_ENGINE_KEYS = {"profile", "depth", "validated_at"}

_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_CONCEPT_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_LESSON_TYPE = "move_and_plan"
_MIN_SUMMARY_CHARS = 20
_MIN_LICENSE_NOTE_CHARS = 20

__all__ = [
    "COURSE_SOURCE",
    "COURSE",
    "MAX_PLAN_CHARS",
    "PASSING_VERDICTS",
    "VERDICTS",
    "IGNORED_VERDICTS",
    "STATUS_NOT_STARTED",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "EngineInfo",
    "SourceInfo",
    "ReadingLesson",
    "Chapter",
    "Course",
    "Progress",
    "load_course",
    "build_progress",
    "list_chapters",
    "get_course_meta",
    "get_chapter_meta",
    "find_chapter",
    "find_lesson",
    "pick_next_lesson",
    "create_session",
    "lesson_payload",
    "revealed_payload",
    "judge_answer",
    "validate_plan",
    "normalize_san",
    "same_san",
    "normalize_move",
    "parse_move",
]


# --- типы -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class EngineInfo:
    """Блок ``engine`` из контента: профиль, глубина, дата валидации."""

    profile: str
    depth: int
    validated_at: str


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Источник курса (книга/ссылка/лицензия) — безопасен для публичного API."""

    title: str
    url: str
    license_note: str


@dataclass(frozen=True, slots=True)
class ReadingLesson:
    """Один урок-позиция курса.

    ``best_move``/``best_line``/``why`` наружу до ответа не отдаются
    (см. :func:`lesson_payload` и :func:`revealed_payload`).
    """

    id: str
    type: str
    title: str
    text: str
    fen: str
    side: str
    question: str
    plan_prompt: str
    accepted_moves: tuple[str, ...]
    best_move: str
    best_line: tuple[str, ...]
    concepts: tuple[str, ...]
    why: str
    difficulty: int
    engine: EngineInfo
    chapter_id: str = ""


@dataclass(frozen=True, slots=True)
class Chapter:
    """Глава курса с упорядоченным списком уроков."""

    id: str
    title: str
    summary: str
    order: int
    lessons: tuple[ReadingLesson, ...]


@dataclass(frozen=True, slots=True)
class Course:
    """Курс «Моя система»: метаданные источника и главы."""

    id: str
    title: str
    version: int
    source: SourceInfo
    chapters: tuple[Chapter, ...]


@dataclass(frozen=True, slots=True)
class Progress:
    """Агрегированный прогресс игрока: попытки из ``reading_attempts``.

    Строки — dict'ы с ключами ``lesson_id``, ``chapter_id`` (необязателен),
    ``verdict``, ``created_at`` (миллисекунды эпохи, необязателен). Модуль
    БД не трогает: строки передаёт вызывающий код (API/тесты).
    """

    attempts: tuple[Mapping[str, Any], ...] = ()


# --- загрузка и строгая валидация бандла ------------------------------------

def _fail(where: str, reason: str) -> RuntimeError:
    return RuntimeError(f"reading course bundle invalid: {where}: {reason}")


def _is_int(value: Any) -> bool:
    """Целое без bool (True — не валидное число для контента)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_fen(where: str, fen: Any, side: Any) -> chess.Board:
    if not isinstance(fen, str) or not fen.strip():
        raise _fail(where, "fen must be a non-empty string")
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise _fail(where, f"invalid FEN: {exc}") from exc
    if not board.is_valid():
        raise _fail(where, f"illegal position (status={board.status()})")
    if side not in ("white", "black"):
        raise _fail(where, f"side must be 'white' or 'black', got {side!r}")
    expected = chess.WHITE if side == "white" else chess.BLACK
    if board.turn != expected:
        raise _fail(where, f"side mismatch (side={side}, FEN turn={'white' if board.turn else 'black'})")
    return board


def _validate_engine(where: str, raw: Any) -> EngineInfo:
    if not isinstance(raw, dict):
        raise _fail(where, "engine must be an object")
    keys = set(raw)
    if keys != _ENGINE_KEYS:
        raise _fail(
            where,
            f"engine keys must be exactly {sorted(_ENGINE_KEYS)}, got {sorted(keys)}",
        )
    profile = raw["profile"]
    if not _nonempty_str(profile):
        raise _fail(where, "engine.profile must be a non-empty string")
    depth = raw["depth"]
    if not _is_int(depth) or depth < 1:
        raise _fail(where, f"engine.depth must be an integer >= 1, got {depth!r}")
    validated_at = raw["validated_at"]
    if not isinstance(validated_at, str):
        raise _fail(where, "engine.validated_at must be a string YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(validated_at)
    except ValueError:
        raise _fail(where, f"engine.validated_at must be a valid date, got {validated_at!r}") from None
    if parsed.isoformat() != validated_at:
        raise _fail(where, f"engine.validated_at must be YYYY-MM-DD, got {validated_at!r}")
    return EngineInfo(profile=profile.strip(), depth=depth, validated_at=validated_at)


def _validate_lesson(
    raw: Any,
    chapter_id: str,
    seen_ids: set[str],
    seen_fens: set[str],
) -> ReadingLesson:
    where = f"lesson {raw.get('id')!r}" if isinstance(raw, dict) else "lesson <not an object>"
    if not isinstance(raw, dict):
        raise _fail("lesson", "lesson must be an object")
    keys = set(raw)
    if keys != _LESSON_KEYS:
        raise _fail(
            where,
            f"lesson keys must be exactly {sorted(_LESSON_KEYS)}, got {sorted(keys)}",
        )

    lesson_id = raw["id"]
    if not isinstance(lesson_id, str) or not _ID_RE.match(lesson_id):
        raise _fail(where, f"lesson id must match {_ID_RE.pattern}, got {lesson_id!r}")
    where = f"lesson {lesson_id!r}"
    if lesson_id in seen_ids:
        raise _fail(where, f"duplicate lesson id: {lesson_id!r}")
    seen_ids.add(lesson_id)

    lesson_type = raw["type"]
    if lesson_type != _LESSON_TYPE:
        raise _fail(where, f"unsupported type {lesson_type!r} (expected {_LESSON_TYPE!r})")

    for field in ("title", "text", "question", "plan_prompt", "why"):
        if not _nonempty_str(raw[field]):
            raise _fail(where, f"{field} must be a non-empty string")

    board = _validate_fen(where, raw["fen"], raw["side"])
    fen = raw["fen"].strip()
    if fen in seen_fens:
        raise _fail(where, f"duplicate FEN: {fen!r}")
    seen_fens.add(fen)

    accepted = raw["accepted_moves"]
    if not isinstance(accepted, list) or not accepted:
        raise _fail(where, "accepted_moves must be a non-empty list of SAN strings")
    for move in accepted:
        if not isinstance(move, str) or not move.strip():
            raise _fail(where, "accepted_moves entries must be non-empty strings")
    if len(set(accepted)) != len(accepted):
        raise _fail(where, "accepted_moves contains duplicates")
    for san in accepted:
        try:
            board.parse_san(san)
        except ValueError:
            raise _fail(where, f"accepted move {san!r} is not a legal SAN in this position") from None

    best_move = raw["best_move"]
    if not isinstance(best_move, str) or not best_move.strip():
        raise _fail(where, "best_move must be a non-empty string")
    if best_move not in accepted:
        raise _fail(where, f"best_move {best_move!r} is not in accepted_moves")

    best_line = raw["best_line"]
    if not isinstance(best_line, list) or not best_line:
        raise _fail(where, "best_line must be a non-empty list of SAN strings")
    line_board = board.copy()
    for index, san in enumerate(best_line, start=1):
        if not isinstance(san, str) or not san.strip():
            raise _fail(where, f"best_line move #{index} must be a non-empty string")
        try:
            move = line_board.parse_san(san)
        except ValueError:
            raise _fail(where, f"best_line move #{index} {san!r} is not legal") from None
        line_board.push(move)

    concepts = raw["concepts"]
    if not isinstance(concepts, list) or not concepts:
        raise _fail(where, "concepts must be a non-empty list of strings")
    for concept in concepts:
        if not isinstance(concept, str) or not _CONCEPT_RE.match(concept):
            raise _fail(where, f"concept {concept!r} must match {_CONCEPT_RE.pattern}")
    if len(set(concepts)) != len(concepts):
        raise _fail(where, "concepts contains duplicates")

    difficulty = raw["difficulty"]
    if not _is_int(difficulty) or not 1 <= difficulty <= 5:
        raise _fail(where, f"difficulty must be an integer in 1..5, got {difficulty!r}")

    engine = _validate_engine(where, raw["engine"])

    return ReadingLesson(
        id=lesson_id,
        type=lesson_type,
        title=raw["title"].strip(),
        text=raw["text"].strip(),
        fen=fen,
        side=raw["side"],
        question=raw["question"].strip(),
        plan_prompt=raw["plan_prompt"].strip(),
        accepted_moves=tuple(accepted),
        best_move=best_move,
        best_line=tuple(best_line),
        concepts=tuple(concepts),
        why=raw["why"].strip(),
        difficulty=difficulty,
        engine=engine,
        chapter_id=chapter_id,
    )


def _validate_bundle(data: Any, path: Path) -> Course:
    """Строгая проверка бандла; любая проблема — ``RuntimeError`` с причиной."""
    where = str(path)
    if not isinstance(data, dict):
        raise _fail(where, "top-level must be a JSON object")
    keys = set(data)
    if keys != _COURSE_KEYS:
        raise _fail(
            where,
            f"top-level keys must be exactly {sorted(_COURSE_KEYS)}, got {sorted(keys)}",
        )

    course_id = data["id"]
    if not isinstance(course_id, str) or not _ID_RE.match(course_id):
        raise _fail(where, f"course id must match {_ID_RE.pattern}, got {course_id!r}")
    if not _nonempty_str(data["title"]):
        raise _fail(where, "course title must be a non-empty string")
    version = data["version"]
    if not _is_int(version) or version < 1:
        raise _fail(where, f"course version must be an integer >= 1, got {version!r}")

    source_raw = data["source"]
    if not isinstance(source_raw, dict):
        raise _fail(where, "source must be an object")
    source_keys = set(source_raw)
    if source_keys != _SOURCE_KEYS:
        raise _fail(
            where,
            f"source keys must be exactly {sorted(_SOURCE_KEYS)}, got {sorted(source_keys)}",
        )
    if not _nonempty_str(source_raw["title"]):
        raise _fail(where, "source.title must be a non-empty string")
    url = source_raw["url"]
    if not isinstance(url, str) or not url.startswith("https://"):
        raise _fail(where, f"source.url must start with 'https://', got {url!r}")
    license_note = source_raw["license_note"]
    if not isinstance(license_note, str) or len(license_note.strip()) < _MIN_LICENSE_NOTE_CHARS:
        raise _fail(where, f"source.license_note must be at least {_MIN_LICENSE_NOTE_CHARS} chars")
    source = SourceInfo(
        title=source_raw["title"].strip(),
        url=url.strip(),
        license_note=license_note.strip(),
    )

    chapters_raw = data["chapters"]
    if not isinstance(chapters_raw, list) or not chapters_raw:
        raise _fail(where, "chapters must be a non-empty list")

    chapters: list[Chapter] = []
    seen_chapter_ids: set[str] = set()
    seen_orders: set[int] = set()
    seen_lesson_ids: set[str] = set()
    seen_fens: set[str] = set()

    for index, raw in enumerate(chapters_raw, start=1):
        ch_where = f"chapter #{index}"
        if not isinstance(raw, dict):
            raise _fail(ch_where, "chapter must be an object")
        ch_keys = set(raw)
        if ch_keys != _CHAPTER_KEYS:
            raise _fail(
                ch_where,
                f"chapter keys must be exactly {sorted(_CHAPTER_KEYS)}, got {sorted(ch_keys)}",
            )
        chapter_id = raw["id"]
        if not isinstance(chapter_id, str) or not _ID_RE.match(chapter_id):
            raise _fail(ch_where, f"chapter id must match {_ID_RE.pattern}, got {chapter_id!r}")
        ch_where = f"chapter {chapter_id!r}"
        if chapter_id in seen_chapter_ids:
            raise _fail(ch_where, f"duplicate chapter id: {chapter_id!r}")
        seen_chapter_ids.add(chapter_id)

        if not _nonempty_str(raw["title"]):
            raise _fail(ch_where, "chapter title must be a non-empty string")
        summary = raw["summary"]
        if not isinstance(summary, str) or len(summary.strip()) < _MIN_SUMMARY_CHARS:
            raise _fail(ch_where, f"chapter summary must be at least {_MIN_SUMMARY_CHARS} chars")
        order = raw["order"]
        if not _is_int(order) or order < 1:
            raise _fail(ch_where, f"chapter order must be an integer >= 1, got {order!r}")
        if order in seen_orders:
            raise _fail(ch_where, f"duplicate chapter order: {order}")
        seen_orders.add(order)

        lessons_raw = raw["lessons"]
        if not isinstance(lessons_raw, list) or not lessons_raw:
            raise _fail(ch_where, "chapter lessons must be a non-empty list")
        lessons = tuple(
            _validate_lesson(item, chapter_id, seen_lesson_ids, seen_fens)
            for item in lessons_raw
        )
        chapters.append(
            Chapter(
                id=chapter_id,
                title=raw["title"].strip(),
                summary=summary.strip(),
                order=order,
                lessons=lessons,
            )
        )

    return Course(
        id=course_id,
        title=data["title"].strip(),
        version=version,
        source=source,
        chapters=tuple(chapters),
    )


def load_course(path: Path | str | None = None) -> Course:
    """Загружает и строго валидирует контент-бандл курса.

    По умолчанию читает :data:`COURSE_SOURCE`. Любая невалидность —
    ``RuntimeError`` с точной причиной (файл не найден / битый JSON /
    нарушенная схема / нелегальная позиция и т.д.).
    """
    bundle_path = Path(path) if path is not None else COURSE_SOURCE
    if not bundle_path.is_file():
        raise RuntimeError(f"reading course bundle not found: {bundle_path}")
    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"reading course bundle invalid JSON: {bundle_path}: {exc}") from exc
    return _validate_bundle(data, bundle_path)


#: Курс, загруженный и провалидированный при импорте модуля.
COURSE = load_course()


# --- прогресс ----------------------------------------------------------------

def build_progress(attempts: Iterable[Mapping[str, Any]] | None) -> Progress:
    """Собирает :class:`Progress` из строк ``reading_attempts`` (или None)."""
    if attempts is None:
        return Progress()
    return Progress(tuple(row for row in attempts if isinstance(row, Mapping)))


#: Вердикты-заглушки (сессия создана / урок пропущен): не считаются
#: ответами в статистике прогресса и не портят accuracy.
IGNORED_VERDICTS = frozenset({"started", "skipped"})

def _attempts_of(progress: Progress | Iterable[Mapping[str, Any]] | None) -> tuple[Mapping[str, Any], ...]:
    if progress is None:
        return ()
    if isinstance(progress, Progress):
        rows = progress.attempts
        return tuple(
            row for row in rows
            if str(row.get("verdict") or "").strip().lower() not in IGNORED_VERDICTS
        )
    return tuple(
        row for row in progress
        if isinstance(row, Mapping)
        and str(row.get("verdict") or "").strip().lower() not in IGNORED_VERDICTS
    )


def _timestamp(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0


def _verdict(row: Mapping[str, Any]) -> str:
    return str(row.get("verdict") or "").strip().lower()


def _is_passing(row: Mapping[str, Any]) -> bool:
    return _verdict(row) in PASSING_VERDICTS


def _chapter_attempts(
    chapter: Chapter,
    attempts: tuple[Mapping[str, Any], ...],
) -> list[Mapping[str, Any]]:
    lesson_ids = {lesson.id for lesson in chapter.lessons}
    rows: list[Mapping[str, Any]] = []
    for row in attempts:
        chapter_id = row.get("chapter_id")
        lesson_id = row.get("lesson_id")
        if chapter_id == chapter.id:
            rows.append(row)
        elif chapter_id in (None, "") and lesson_id in lesson_ids:
            # Строки без chapter_id привязываем по lesson_id.
            rows.append(row)
    return rows


def _chapter_stats(
    chapter: Chapter,
    attempts: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    rows = _chapter_attempts(chapter, attempts)
    lesson_ids = {lesson.id for lesson in chapter.lessons}
    total = len(chapter.lessons)

    passed_lessons: set[str] = set()
    per_lesson: dict[str, list[Mapping[str, Any]]] = {}
    passing_rows = 0
    for row in rows:
        lesson_id = row.get("lesson_id")
        if lesson_id not in lesson_ids:
            continue
        per_lesson.setdefault(lesson_id, []).append(row)
        if _is_passing(row):
            passed_lessons.add(lesson_id)
            passing_rows += 1

    considered = sum(len(items) for items in per_lesson.values())
    accuracy = round(passing_rows / considered, 4) if considered else None

    correct_first_attempt = 0
    for lesson_id, items in per_lesson.items():
        first = min(items, key=_timestamp)
        if _verdict(first) == "correct":
            correct_first_attempt += 1

    completed = len(passed_lessons)
    if completed >= total and total > 0:
        status = STATUS_COMPLETED
    elif considered:
        status = STATUS_IN_PROGRESS
    else:
        status = STATUS_NOT_STARTED
    return {
        "lessons_total": total,
        "completed": completed,
        "accuracy": accuracy,
        "correct_first_attempt": correct_first_attempt,
        "status": status,
    }


def _lesson_statuses(
    chapter: Chapter,
    attempts: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    rows = _chapter_attempts(chapter, attempts)
    per_lesson: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        lesson_id = row.get("lesson_id")
        per_lesson.setdefault(lesson_id, []).append(row)

    result: list[dict[str, Any]] = []
    for lesson in chapter.lessons:
        items = per_lesson.get(lesson.id, [])
        if any(_is_passing(row) for row in items):
            status = STATUS_COMPLETED
        elif items:
            status = STATUS_IN_PROGRESS
        else:
            status = STATUS_NOT_STARTED
        result.append(
            {
                "lesson_id": lesson.id,
                "title": lesson.title,
                "difficulty": lesson.difficulty,
                "status": status,
                "attempts": len(items),
            }
        )
    return result


def _chapter_summary(
    chapter: Chapter,
    attempts: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    stats = _chapter_stats(chapter, attempts)
    return {
        "chapter_id": chapter.id,
        "order": chapter.order,
        "title": chapter.title,
        "summary": chapter.summary,
        **stats,
    }


# --- публичное API -----------------------------------------------------------

def find_chapter(course: Course, chapter_id: str) -> Chapter:
    """Глава по id; нет такой — ``KeyError`` с перечислением доступных."""
    key = (chapter_id or "").strip()
    for chapter in course.chapters:
        if chapter.id == key:
            return chapter
    known = ", ".join(chapter.id for chapter in course.chapters)
    raise KeyError(f"course {course.id!r} has no chapter {key!r}; known: {known}")


def find_lesson(course: Course, chapter_id: str, lesson_id: str) -> ReadingLesson:
    """Урок по id внутри главы; нет такого — ``KeyError``."""
    chapter = find_chapter(course, chapter_id)
    key = (lesson_id or "").strip()
    for lesson in chapter.lessons:
        if lesson.id == key:
            return lesson
    known = ", ".join(lesson.id for lesson in chapter.lessons)
    raise KeyError(f"chapter {chapter.id!r} has no lesson {key!r}; known: {known}")


def list_chapters(
    course: Course,
    progress: Progress | Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Сводка по главам (по ``order``): прогресс и статус по попыткам игрока."""
    attempts = _attempts_of(progress)
    return [
        _chapter_summary(chapter, attempts)
        for chapter in sorted(course.chapters, key=lambda chapter: chapter.order)
    ]


def get_course_meta(course: Course) -> dict[str, Any]:
    """Мета курса и глав без решений: безопасно отдавать клиенту до ответа."""
    return {
        "course": {
            "id": course.id,
            "title": course.title,
            "version": course.version,
        },
        "source": {
            "title": course.source.title,
            "url": course.source.url,
            "license_note": course.source.license_note,
        },
        "chapters": [
            {
                "chapter_id": chapter.id,
                "order": chapter.order,
                "title": chapter.title,
                "summary": chapter.summary,
                "lessons_total": len(chapter.lessons),
            }
            for chapter in sorted(course.chapters, key=lambda chapter: chapter.order)
        ],
    }


def get_chapter_meta(
    course: Course,
    progress: Progress | Iterable[Mapping[str, Any]] | None,
    chapter_id: str,
) -> dict[str, Any]:
    """Прогресс по одной главе: сводка + статус каждого урока."""
    chapter = find_chapter(course, chapter_id)
    attempts = _attempts_of(progress)
    return {
        "chapter": {
            "chapter_id": chapter.id,
            "order": chapter.order,
            "title": chapter.title,
            "summary": chapter.summary,
        },
        "progress": _chapter_stats(chapter, attempts),
        "lessons": _lesson_statuses(chapter, attempts),
    }


def pick_next_lesson(
    course: Course,
    progress: Progress | Iterable[Mapping[str, Any]] | None,
    chapter_id: str,
) -> ReadingLesson:
    """Следующий незачтённый урок главы (в порядке контента).

    Если все уроки главы пройдены, возвращается первый урок главы
    (режим повторения). Неизвестная глава — ``KeyError``.
    """
    chapter = find_chapter(course, chapter_id)
    attempts = _attempts_of(progress)
    rows = _chapter_attempts(chapter, attempts)
    completed = {
        row.get("lesson_id") for row in rows if _is_passing(row)
    }
    for lesson in chapter.lessons:
        if lesson.id not in completed:
            return lesson
    return chapter.lessons[0]


def create_session(
    course: Course,
    player: str,
    chapter_id: str,
    lesson_id: str | None = None,
    progress: Progress | Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Готовый dict сессии (без сохранения — БД ведёт API-слой).

    ``lesson_id`` не задан — берётся :func:`pick_next_lesson`. Неизвестные
    глава/урок — ``KeyError``, пустой игрок — ``ValueError``. Решение в
    ``lesson`` не попадает.
    """
    name = (player or "").strip()
    if not name:
        raise ValueError("player is required")
    if lesson_id is None:
        lesson = pick_next_lesson(course, progress, chapter_id)
    else:
        lesson = find_lesson(course, chapter_id, lesson_id)
    return {
        "session_id": uuid.uuid4().hex,
        "player": name,
        "course_id": course.id,
        "course_version": course.version,
        "chapter_id": lesson.chapter_id,
        "lesson_id": lesson.id,
        "status": "active",
        "created_at": int(time.time() * 1000),
        "lesson": lesson_payload(lesson),
    }


# --- payloads -----------------------------------------------------------------

def lesson_payload(lesson: ReadingLesson) -> dict[str, Any]:
    """Публичный payload урока: БЕЗ решения (best_move/line/why/concepts)."""
    return {
        "lesson_id": lesson.id,
        "chapter_id": lesson.chapter_id,
        "type": lesson.type,
        "title": lesson.title,
        "text": lesson.text,
        "fen": lesson.fen,
        "side": lesson.side,
        "turn": lesson.side,
        "question": lesson.question,
        "plan_prompt": lesson.plan_prompt,
        "difficulty": lesson.difficulty,
    }


def revealed_payload(lesson: ReadingLesson) -> dict[str, Any]:
    """Раскрытие ответа: только после отправки/проверки ответа игроком."""
    return {
        "lesson_id": lesson.id,
        "chapter_id": lesson.chapter_id,
        "accepted_moves": list(lesson.accepted_moves),
        "best_move": lesson.best_move,
        "best_line": list(lesson.best_line),
        "why": lesson.why,
        "concepts": list(lesson.concepts),
    }


# --- разбор хода и судья ------------------------------------------------------

def normalize_san(san: str) -> str:
    """SAN без суффиксов шаха/мата и аннотаций (для сравнения ходов)."""
    if not isinstance(san, str):
        return ""
    return san.strip().rstrip("+#!?")


def same_san(left: Any, right: Any) -> bool:
    """Сравнение SAN-ходов без суффиксов шаха/мата и аннотаций."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return normalize_san(left) == normalize_san(right)


def parse_move(board: chess.Board, raw: Any) -> tuple[chess.Move | None, str | None]:
    """Ход игрока: SAN или UCI. Нечитаемое/нелегальное — ``(None, None)``."""
    text = str(raw or "").strip()
    if not text:
        return None, None
    try:
        move = board.parse_san(text)
        return move, board.san(move)
    except ValueError:
        pass
    try:
        move = chess.Move.from_uci(text)
    except ValueError:
        return None, None
    if move not in board.legal_moves:
        return None, None
    return move, board.san(move)


def normalize_move(board: chess.Board, raw: Any) -> str | None:
    """Канонический SAN хода игрока (или None, если ход не разобран)."""
    move, san = parse_move(board, raw)
    return san if move is not None else None


def validate_plan(plan: Any) -> str:
    """Нормализует текстовый план: strip и лимит :data:`MAX_PLAN_CHARS`.

    Пустой план допустим (поле необязательно). Слишком длинный —
    ``ValueError`` с русским сообщением.
    """
    clean = plan.strip() if isinstance(plan, str) else ""
    if len(clean) > MAX_PLAN_CHARS:
        raise ValueError(
            f"План слишком длинный: {len(clean)} символов (максимум {MAX_PLAN_CHARS})"
        )
    return clean


def _lesson_in_course(course: Course, lesson: ReadingLesson) -> bool:
    return any(
        candidate == lesson
        for chapter in course.chapters
        for candidate in chapter.lessons
    )


def judge_answer(
    course: Course,
    lesson: ReadingLesson,
    move: Any,
    plan: str = "",
) -> dict[str, Any]:
    """Судья ответа (MVP: только заранее валидированные ``accepted_moves``).

    Возвращает dict с ``verdict``: ``correct`` (совпал лучший ход),
    ``acceptable`` (альтернатива из ``accepted_moves``), ``incorrect``
    (легальный ход мимо списка) или ``invalid`` (ход не разобран / план
    превышает лимит). ``judge_kind == "curated"`` для проверенных ходов.

    Раскрытие решения (:func:`revealed_payload`) — отдельным вызовом
    после проверки, здесь его нет. ``accepted_moves`` показываются только
    для разобранных ходов (correct/acceptable/incorrect).
    """
    if not _lesson_in_course(course, lesson):
        raise ValueError(f"lesson {lesson.id!r} does not belong to course {course.id!r}")

    try:
        plan_clean = validate_plan(plan)
    except ValueError as exc:
        return {
            "verdict": "invalid",
            "judge_kind": None,
            "reason": "plan_too_long",
            "message": str(exc),
            "move": {"played_san": None, "uci": None, "legal": False},
            "plan": None,
        }

    board = chess.Board(lesson.fen)
    parsed, played_san = parse_move(board, move)
    if parsed is None:
        return {
            "verdict": "invalid",
            "judge_kind": None,
            "reason": "unparseable_move",
            "message": f"Ход не разобран или нелегален: {move!r}",
            "move": {"played_san": None, "uci": None, "legal": False},
            "plan": plan_clean,
        }

    if same_san(played_san, lesson.best_move):
        verdict = "correct"
    elif any(same_san(played_san, accepted) for accepted in lesson.accepted_moves):
        verdict = "acceptable"
    else:
        verdict = "incorrect"

    return {
        "verdict": verdict,
        "judge_kind": "curated",
        "move": {
            "played_san": played_san,
            "uci": parsed.uci(),
            "legal": True,
        },
        "plan": plan_clean,
        "accepted_moves": list(lesson.accepted_moves),
    }
