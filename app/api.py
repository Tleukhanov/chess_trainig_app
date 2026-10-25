"""Chess Trainer API — чистый JSON API для веб-интерфейса.

Бэкенд не знает о шаблонах/HTML. Фронтенд — отдельные статические файлы
в папке frontend/, которые потребляют этот API.
"""

from __future__ import annotations

import csv
import datetime
import gc
import hashlib
import io
import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field

import chess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import data as shared_data
from . import middlegame as mg
from . import theory
from . import timeutil
from .analyzer import Engine, win_percent
from .coach import _extract_json, build_request, run_coach
from .config import settings
from .dayplan import build_tasks
from .db import Database, hash_password, is_admin, verify_password
from .drills import drills_to_json, drills_to_pgn
from .fide import (
    TournamentGame,
    build_fide_trend,
    build_tournament_report,
    fetch_fide_player,
    fetch_fide_ratings,
)
from .games import Game, fetch_lichess_rating, fetch_user_games
from .humanize import humanize_report
from .intent_review import intent_cache_key, review_intents
from .review import repeated_mistakes
from .llm import ChatMessage, LLMClient, _is_local_host, clean_llm_text, looks_like_refusal, looks_like_thinking
from .maia import MaiaLitePolicy
from .mentor import build_mentor_request, format_mentor_reply, run_mentor
from .metrics import metric
from .mistakes import cause_label, classify_cause, explain_cause
from .opponent import build_confrontations, build_opponent_profile
from .openings import (
    STYLE_DESCRIPTIONS_RU,
    STYLE_LABELS_RU,
    STYLES,
    OpeningGame,
    board_from_moves,
    classify_opening,
    is_finished,
    new_game,
    opponent_color,
    opponent_move,
    result,
    theory_moves,
    user_move,
)
from .accuracy import accuracy as acc_accuracy, category_stats, estimate_rating, phase_stats
from .comments import game_summary, move_comment
from .paths import user_dir
from .patterns import phase
from .review_moves import badge_for, badge_label, detect_sacrifice
from .pgn_import import MAX_PGN_BYTES, parse_pgn_games
from .plan import build_plan
from .progress import build_progress
from .repertoire import Repertoire, opening_stats
from .report import build_report

# --- job registry ----------------------------------------------------------

logger = logging.getLogger(__name__)

@dataclass
class CoachJob:
    id: str
    user: str
    status: str = "running"  # running | done | error
    phase: str = "Загрузка партий"
    total: int = 0
    done: int = 0
    current: str = ""
    summary: str = ""
    error: str = ""
    result: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "summary": self.summary,
            "error": self.error,
            "result": self.result,
            "started": self.started,
            "finished": self.finished,
        }

_JOBS: dict[str, CoachJob] = {}
_JOBS_LOCK = threading.Lock()

# Запущенные фоновые разборы миттельшпиля: session_id -> job_id. Нужен, чтобы
# повторный /analyze для той же сессии отвечал 409, а не упирался в лимит 503.
_MIDDLEGAME_JOBS: dict[str, str] = {}
_MIDDLEGAME_JOBS_LOCK = threading.Lock()

def _register_job(job: CoachJob) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job

def _get_job(job_id: str) -> CoachJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


# --- persisted job snapshots (переживают перезапуск процесса) --------------

_JOB_META_PREFIX = "job:"


def _persist_job(
    db_path: Path, job: CoachJob, *, session_id: str | None = None
) -> None:
    """Best-effort снапшот job'а в meta: сбой записи не ломает саму задачу.

    Кладёт ``{**job.as_dict(), "session_id": ..., "updated": time.time()}`` под
    ключом ``job:<id>``. Служит для опроса после перезапуска dyno, когда
    in-memory ``_JOBS`` уже пуст.
    """
    try:
        payload = {**job.as_dict(), "session_id": session_id, "updated": time.time()}
        with Database(db_path) as db:
            db.init_db()
            db.set_meta(
                f"{_JOB_META_PREFIX}{job.id}",
                json.dumps(payload, ensure_ascii=False),
            )
    except Exception:
        logger.warning("Не удалось сохранить job %s в meta", job.id, exc_info=True)


def _load_job_meta(db_path: Path, job_id: str) -> dict[str, Any] | None:
    """Читает снапшот job'а из meta; None если записи нет или она битая."""
    try:
        with Database(db_path) as db:
            db.init_db()
            raw = db.get_meta(f"{_JOB_META_PREFIX}{job_id}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# На Heroku eco (512MB) два движка/задачи одновременно не помещаются:
# держим ровно одну глобальную фоновую задачу, чтобы не ловить R14.
JOBS_GLOBAL_LIMIT = 1
JOBS_PER_USER_LIMIT = 1

#: Пресеты глубины анализа: профиль -> (depth, multipv). Default-профиль
#: «quick» экономит время на 50 партиях, «deep» — прежнее поведение (14/3).
_COACH_PROFILES: dict[str, tuple[int, int]] = {
    "quick": (10, 1),
    "standard": (12, 2),
    "deep": (14, 3),
}


def _running_jobs_count() -> int:
    with _JOBS_LOCK:
        return sum(1 for j in _JOBS.values() if j.status == "running")


def _user_running_jobs_count(nick: str) -> int:
    with _JOBS_LOCK:
        return sum(
            1 for j in _JOBS.values() if j.status == "running" and j.user == nick
        )


def _check_job_limits(nick: str) -> None:
    if _user_running_jobs_count(nick) >= JOBS_PER_USER_LIMIT:
        raise HTTPException(
            503, "У тебя уже выполняется задача — дождись завершения"
        )
    if _running_jobs_count() >= JOBS_GLOBAL_LIMIT:
        raise HTTPException(
            503,
            "Сейчас анализируются задачи других игроков — попробуй через минуту",
        )

# --- background job --------------------------------------------------------

def _run_coach_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: загрузка → анализ → сохранение."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()

            job.phase = "Загрузка партий"
            if params["cached_only"]:
                games = db.get_games(job.user, params["max"])
            else:
                lichess_nick = _lichess_of(db, job.user)
                games = fetch_user_games(
                    lichess_nick,
                    since_ts=params["since"],
                    max_games=params["max"],
                    perf=params["perf"],
                )
                db.save_games(games, job.user)

            qualified = [g for g in games if g.is_finished() and g.moves]
            if params["refresh"]:
                target = qualified
            else:
                ids = [g.id for g in qualified]
                unanalyzed = set(db.get_unanalyzed(ids))
                target = [g for g in qualified if g.id in unanalyzed]

            # Свежие партии анализируем первыми: пользователь быстрее видит
            # результаты последних игр, а старые догоняются в фоне.
            target.sort(key=lambda g: g.created_at or 0, reverse=True)

            job.total = len(target)
            job.phase = "Анализ движком"

            if target:
                failed = 0
                for i, game in enumerate(target, 1):
                    # Отмена из /api/jobs/{id}/cancel: проверяем статус перед
                    # каждой партией, чтобы не начинать новую порцию работы.
                    if job.status != "running":
                        break
                    # Движок открываем заново на каждую партию: свежий процесс
                    # не тащит утечки памяти/состояния от прошлой партии, а
                    # try/except гарантирует, что одна битая партия (NaN, обрыв
                    # UCI, экзотика) пропустится, а не уронит всю задачу.
                    try:
                        with Engine(depth=params["depth"], multipv=params["multipv"]) as engine:
                            summary = engine.analyze_game(game).summary()
                        db.save_analysis(game.id, summary, depth=params["depth"])
                    except Exception as exc:
                        failed += 1
                        logger.warning("Анализ партии %s пропущен: %s", game.id, exc)
                        job.phase = f"Анализ движком (пропущено битых: {failed})"
                        continue
                    finally:
                        # Память на Eco не резиновая: после каждой партии
                        # принудительно отдаём Python-кучу и проверяем, что
                        # движок точно убит (зомби-процессы копят hash).
                        gc.collect()
                    job.done = i
                    job.current = game.id
                if failed:
                    job.summary = (
                        f"Проанализировано {job.done} из {job.total} партий, "
                        f"пропущено битых: {failed}."
                    )
            else:
                job.phase = "Ничего анализировать"
                job.summary = "Все загруженные партии уже проанализированы."

            if job.status == "running":
                job.status = "done"
                job.summary = job.summary or f"Проанализировано {job.total} партий."

    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()

def _run_prepare_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: его партии → анализ → профиль + точки встречи."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()

            your_pairs = db.get_analyzed_games(job.user, limit=500)
            if not your_pairs:
                raise RuntimeError(
                    f"Нет твоих проанализированных партий для {job.user} — "
                    "сначала запусти анализ на странице «Анализ»."
                )

            job.phase = "Загрузка партий соперника"
            games = fetch_user_games(
                params["opponent"],
                since_ts=None,
                max_games=params["max"],
                perf=params["perf"],
            )
            db.save_games(games, params["opponent"])

            qualified = [g for g in games if g.is_finished() and g.moves]
            ids = [g.id for g in qualified]
            unanalyzed = set(db.get_unanalyzed(ids)) if ids else set()
            target = [g for g in qualified if g.id in unanalyzed]

            job.total = len(target)
            job.phase = "Анализ движком"

            fresh: dict[str, dict[str, Any]] = {}
            if target:
                with Engine(
                    depth=settings.stockfish_depth,
                    multipv=settings.stockfish_multipv,
                ) as engine:
                    for i, game in enumerate(target, 1):
                        summary = engine.analyze_game(game).summary()
                        db.save_analysis(game.id, summary, depth=settings.stockfish_depth)
                        fresh[game.id] = summary
                        job.done = i
                        job.current = game.id

            job.phase = "Профиль соперника"
            his_pairs: list[tuple[Any, dict[str, Any]]] = []
            for game in qualified:
                summary = fresh.get(game.id) or db.get_analysis(game.id)
                if summary is not None:
                    his_pairs.append((game, summary))
            if not his_pairs:
                raise RuntimeError("Нет партий соперника с анализом — готовиться не к чему.")

            profile = build_opponent_profile(his_pairs, params["opponent"], top=8, color="both")
            confrontations = build_confrontations(profile, your_pairs, top=8)
            job.result = {
                "profile": {k: v for k, v in profile.items() if k != "progress"},
                "confrontations": confrontations,
            }
            job.status = "done"
            job.summary = f"Партий соперника с анализом: {len(his_pairs)}"

    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()

def _run_humanize_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: оценка человечности ошибок (MaiaLite) → humanity.json."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()
            pairs = db.get_analyzed_games(job.user, limit=500)
        if not pairs:
            raise RuntimeError(
                f"Нет проанализированных партий для {job.user} — сначала запусти анализ."
            )

        job.total = len(pairs)
        job.phase = "Оценка человечности"
        with MaiaLitePolicy() as policy:
            report = humanize_report(pairs, policy, k=8)

        out = user_dir(job.user, root=app.state.data_dir) / "humanity.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        job.result = _humanity_payload(report)
        job.status = "done"
        job.summary = f"Человечность посчитана: {job.result['summary']['total']} ошибок"

    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()

def _run_fide_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: профиль + история рейтингов FIDE (сеть, кеш)."""
    try:
        nick = job.user or None

        job.phase = "Загрузка профиля FIDE"
        player = fetch_fide_player(params["id"], user=nick)

        job.phase = "Загрузка истории рейтингов"
        ratings = fetch_fide_ratings(params["id"], user=nick)
        trend = build_fide_trend(ratings, windows=params["windows"])

        job.result = {
            "id": params["id"],
            "player": {
                "id": player.id,
                "name": player.name,
                "federation": player.federation,
                "birth_year": player.birth_year,
                "standard": player.standard,
                "rapid": player.rapid,
                "blitz": player.blitz,
            },
            "trend": trend,
        }
        job.status = "done"
        job.summary = f"FIDE {params['id']}: {player.name or 'профиль получен'}"

    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()


def _ensure_llm_key() -> None:
    """Проверяет, что LLM-ключ доступен (иначе job сразу уйдёт в error)."""
    if os.environ.get("LLM_API_KEY"):
        return
    base = os.environ.get("LLM_BASE_URL") or settings.llm_base_url
    host = urllib.parse.urlparse(base).hostname
    if not _is_local_host(host):
        raise RuntimeError(
            "Нет LLM_API_KEY — задай его в переменной окружения или .env "
            "(см. README, раздел про LLM)"
        )


def _select_analyzed_pairs(
    db: Database,
    user: str,
    game_id: str | None = None,
    max_games: int | None = None,
) -> list:
    """Отбирает пары (игра, анализ) для LLM-инструментов: конкретная
    партия либо топ-партии по blunders+mistakes. Ошибки — RuntimeError."""
    if game_id:
        game = db.get_game(game_id)
        if game is None:
            raise RuntimeError(f"Партия {game_id} не найдена в кеше")
        analysis = db.get_analysis(game_id)
        if analysis is None:
            raise RuntimeError(
                f"Партия {game_id} не проанализирована — сначала запусти анализ."
            )
        return [(game, analysis)]
    pairs = db.get_analyzed_games(user, limit=500)
    if not pairs:
        raise RuntimeError(
            f"Нет проанализированных партий для {user} — сначала запусти анализ "
            "(страница «Анализ»)."
        )
    pairs.sort(
        key=lambda p: (
            -(len(p[1].get("blunders") or []) + len(p[1].get("mistakes") or [])),
            -(metric(p[1], "avg_win_loss") or 0.0),
        )
    )
    if max_games:
        pairs = pairs[:max_games]
    return pairs


def _player_name(player: dict | None) -> str:
    """Имя игрока из словаря white/black партии, '—' если нет."""
    if not player:
        return "—"
    name = player.get("name") or player.get("username")
    return str(name) if name else "—"


def _review_entry(game, analysis, idx, moments: dict, summary: str | None) -> dict:
    """Формирует запись «Одна партия» для разбора (поля как в CLI-выводе)."""
    moves = analysis.get("moves") or []
    entry_moments = []
    for ply in idx:
        move = moves[ply] if ply < len(moves) else {}
        drop = move.get("drop")
        entry_moments.append(
            {
                "ply": ply + 1,
                "san": move.get("san"),
                "classification": move.get("classification"),
                "drop": drop if isinstance(drop, (int, float)) else None,
                "text": moments.get(ply),
            }
        )
    return {
        "game_id": game.id,
        "white": _player_name(game.white),
        "black": _player_name(game.black),
        "result": analysis.get("result_for_user") or game.result_for_user or "draw",
        "opening": game.opening or "",
        "avg_win_loss": metric(analysis, "avg_win_loss", default=None),
        "moments": entry_moments,
        "summary": summary,
    }


def _run_review_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: LLM-разбор ключевых моментов партий (команда review)."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()
            pairs = _select_analyzed_pairs(
                db, job.user, params.get("game_id"), params["max"]
            )

        job.phase = "Разбор партий"
        llm = None
        if not params["dry_run"]:
            _ensure_llm_key()
            llm = LLMClient()

        games = []
        prompts = []
        for game, analysis in pairs:
            messages, idx = build_request(game, analysis, params["moments"])
            if not messages:
                continue
            if params["dry_run"]:
                prompts.extend(
                    {"game_id": game.id, "role": m.role, "content": m.content}
                    for m in messages
                )
                games.append(_review_entry(game, analysis, idx, {}, None))
                continue
            moments, summary = run_coach(
                game, analysis, llm, max_moments=params["moments"]
            )
            moments = {
                ply: clean_llm_text(text) for ply, text in moments.items()
            }
            if summary:
                summary = clean_llm_text(summary)
            games.append(_review_entry(game, analysis, idx, moments, summary))

        job.result = {"games": games}
        if params["dry_run"]:
            job.result["dry_run"] = True
            job.result["prompts"] = prompts
            job.summary = f"Промптов собрано: {len(prompts)} для {len(games)} партий"
        else:
            job.summary = f"Разобрано партий: {len(games)}"
        job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()


def _run_mentor_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: LLM-тренер (план по партиям + прогресс + заметки)."""
    try:
        nick = job.user
        with Database(app.state.db_path) as db:
            db.init_db()
            pairs = _select_analyzed_pairs(db, nick, params.get("game_id"), None)

        job.phase = "Сборка плана"
        humanity_path = shared_data.humanity_path(nick, root=app.state.data_dir)
        plan_report = build_plan(
            pairs,
            user=nick,
            humanity=shared_data.load_humanity(humanity_path),
            drills_total=shared_data.count_drills(
                "drills.pgn", nick, root=app.state.data_dir
            ),
            drills_unnatural=shared_data.count_drills(
                "drills_unnatural.pgn", nick, root=app.state.data_dir
            ),
            max_depth=params["max_depth"],
        )

        progress_report = None
        if not params["no_progress"]:
            job.phase = "Сборка прогресса"
            progress_report = build_progress(
                pairs,
                user=nick,
                humanity=shared_data.load_humanity_raw(humanity_path),
                windows=5,
            )

        request = build_mentor_request(
            plan_report,
            extra=params.get("notes") or "",
            progress=progress_report,
        )

        if params["dry_run"]:
            job.result = {
                "dry_run": True,
                "user": nick,
                "notes": params.get("notes") or "",
                "prompts": [{"role": m.role, "content": m.content} for m in request],
            }
            job.summary = "Mentor: собраны промпты без вызова LLM"
        else:
            job.phase = "Ответ тренера"
            _ensure_llm_key()
            llm = LLMClient()
            reply = run_mentor(llm, request)
            job.result = {
                "user": plan_report.get("user"),
                "model": llm.model,
                "notes": params.get("notes") or "",
                "reply": format_mentor_reply(clean_llm_text(reply)),
            }
            job.summary = f"План тренера по {plan_report.get('games', 0)} партиям"
        job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()


def _run_task_explain_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: LLM-объяснение «Задачи дня» → таблица task_explanations."""
    try:
        _ensure_llm_key()
        payload = params["payload"]
        job.phase = "Объяснение задачи"
        text = _task_explain_llm(payload)
        if not text:
            raise RuntimeError("LLM вернул пустое объяснение")
        with Database(app.state.db_path) as db:
            db.init_db()
            db.save_task_explanation(
                payload["task_key"],
                job.user,
                payload["game_id"],
                payload["ply"],
                text,
            )
        job.result = {"explanation": text}
        job.summary = "Объяснение задачи готово"
        job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()

# --- Тренер дебютов: сессии, соперник-политика, фоновый анализ --------------

_OPENING_IN_PROGRESS = "in_progress"
_OPENING_DONE = "done"
_OPENING_ANALYZED = "analyzed"


class OpeningStartBody(BaseModel):
    """Тело POST /api/opening/start."""

    style: str = ""
    color: str = ""
    opp_elo: int | None = None
    my_elo: int | None = None
    human: bool = False
    #: Стартовая линия (SAN): тренировать конкретный дебют, а не рандом.
    start_moves: list[str] | None = None


class OpeningMoveBody(BaseModel):
    """Тело POST /api/opening/move."""

    session_id: str = ""
    san: str = ""


class OpeningAnalyzeBody(BaseModel):
    """Тело POST /api/opening/analyze."""

    session_id: str = ""


class OpeningReplyBody(BaseModel):
    """Тело POST /api/opening/reply."""

    session_id: str = ""
    human: bool = False


class MiddlegamePlanBody(BaseModel):
    """Тело POST /api/middlegame/session/{id}/plan."""

    plan: str = ""
    strength: str = "equal"


class MiddlegameMoveBody(BaseModel):
    """Тело POST /api/middlegame/session/{id}/move."""

    move: str = ""


class MoveNoteBody(BaseModel):
    """Тело POST /api/games/{id}/notes: заметка (идея хода) на полуход."""

    ply: int
    intent: str = ""


class DayPlanToggleBody(BaseModel):
    """Тело POST /api/dayplan/toggle: дата, задача и новое состояние."""

    date: str
    task_id: str
    done: bool = True


# --- Шахматные утилиты: свободный разбор позиции (stateless) -----------------


class ChessPositionBody(BaseModel):
    """Тело POST /api/chess/legal: произвольная позиция в FEN."""

    fen: str


class ChessMoveBody(BaseModel):
    """Тело POST /api/chess/move.

    ``from`` — ключевое слово Python, поэтому поле называется ``from_sq`` и
    принимается из JSON по alias ``from`` (либо по имени благодаря
    ``populate_by_name``).
    """

    model_config = {"populate_by_name": True}

    fen: str
    from_sq: str = Field(alias="from")
    to: str
    promotion: str | None = None


class ChessEvalBody(BaseModel):
    """Тело POST /api/chess/eval: FEN и желаемая глубина (6..14)."""

    fen: str
    depth: int = 10


class TheoryIdeasBody(BaseModel):
    """Тело POST /api/theory/ideas: FEN и главная линия (шаги Explorer)."""

    fen: str = ""
    line: list[Any] = Field(default_factory=list)


#: Границы глубины быстрой оценки позиции (экран разбора задачи дня).
CHESS_EVAL_DEPTH_MIN = 6
CHESS_EVAL_DEPTH_MAX = 14
CHESS_EVAL_TIME_MS = 800


def _fen_board(fen: str) -> chess.Board:
    """Доска из FEN или HTTP 400 при невалидной строке/позиции."""
    try:
        return chess.Board(fen)
    except ValueError as exc:
        raise HTTPException(400, f"Невалидный FEN: {exc}") from exc


def _chess_move_from_body(board: chess.Board, body: ChessMoveBody) -> chess.Move:
    """UCI-ход из тела запроса; HTTP 400 если ход не легален в позиции."""
    uci = f"{body.from_sq}{body.to}{body.promotion or ''}".lower()
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise HTTPException(400, f"Невалидный ход: {uci}") from exc
    if move not in board.legal_moves:
        raise HTTPException(400, f"Нелегальный ход: {uci}")
    return move


def _opening_policy(opp_elo: int, my_elo: int) -> tuple[Any, str]:
    """Политика соперника: Maia-2 ONNX, гибрид Maia+Stockfish или MaiaLite.

    ``opp_elo < 0`` означает режим гибрида (Maia-кандидаты, отфильтрованные
    Stockfish): Maia играет с верхнего бина (>=2000), фильтр отсекает зевки.
    Возвращает ``(policy, engine)``, где ``engine`` — ``"maia2"``,
    ``"hybrid"`` либо ``"maia_lite"``.
    """
    if opp_elo is not None and int(opp_elo) < 0:
        try:
            from .hybrid import HybridPolicy

            return HybridPolicy(self_elo=2500, opp_elo=my_elo), "hybrid"
        except Exception:  # noqa: BLE001
            # Фолбэк на чистую Maia, если гибрид не поднялся.
            try:
                from .maia_onnx import get_policy

                return get_policy(self_elo=2000, opp_elo=my_elo), "maia2"
            except RuntimeError:
                policy = MaiaLitePolicy()
                policy.__enter__()
                return policy, "maia_lite"

    try:
        from .maia_onnx import get_policy

        return get_policy(self_elo=opp_elo, opp_elo=my_elo), "maia2"
    except RuntimeError:
        policy = MaiaLitePolicy()
        policy.__enter__()
        return policy, "maia_lite"


def _close_opening_policy(policy: Any, engine: str) -> None:
    """Освобождает ресурсы фолбэка MaiaLite (у него свой процесс Stockfish).

    Singleton Maia-2 не закрываем: его ONNX-сессия переиспользуется между
    ходами, а память освобождает :func:`_unload_maia` перед анализом.
    """
    if engine not in ("maia_lite", "hybrid"):
        return
    close = getattr(policy, "close", None)
    if callable(close):
        close()


def _unload_maia() -> None:
    """Memory guard: освобождает ONNX-сессию Maia перед запуском Stockfish.

    В 512MB-контейнере (Heroku eco) Maia и Stockfish не должны жить вместе:
    вызов идёт строго до создания :class:`Engine` в фоновом job'е. После
    сброса сессии дополнительно зовём ``gc.collect()`` и (на Linux)
    ``malloc_trim(0)``, чтобы вернуть память ОС — best-effort, ошибки глотаем.
    """
    try:
        from . import maia_onnx

        maia_onnx.unload()
    except Exception:
        pass
    try:
        gc.collect()
    except Exception:
        pass
    try:
        from .maia_onnx import release_memory

        release_memory()
    except Exception:
        pass


def _opening_game_from_session(session: dict[str, Any]) -> OpeningGame:
    """Восстанавливает :class:`OpeningGame` из строки ``opening_sessions``."""
    return OpeningGame(
        moves=list(session.get("moves") or []),
        my_elo=int(session.get("my_elo") or settings.maia_default_elo),
        opp_elo=int(session.get("opp_elo") or settings.maia_default_elo),
        style=session.get("style") or "universal",
        color=session.get("color") or "white",
        plies=int(settings.opening_plies),
    )


def _opening_need_reply(game: OpeningGame) -> bool:
    """Нужен ли ответ соперника: его очередь и у него есть ход."""
    if is_finished(game):
        return False
    board = board_from_moves(game.moves)
    return board.turn == opponent_color(game) and any(board.legal_moves)


def _opening_state(
    session: dict[str, Any],
    *,
    engine: str | None = None,
    last_reply: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Публичное состояние сессии для фронтенда (единый формат)."""
    game = _opening_game_from_session(session)
    board = board_from_moves(game.moves)
    state: dict[str, Any] = {
        "id": session.get("id"),
        "status": session.get("status"),
        "moves": list(game.moves),
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "color": game.color,
        "style": game.style,
        "my_elo": game.my_elo,
        "opp_elo": game.opp_elo,
        "plies": game.plies,
        "finished": is_finished(game),
        "need_reply": _opening_need_reply(game),
        "result": session.get("result"),
        "report": session.get("report"),
    }
    if engine is not None:
        state["engine"] = engine
    if last_reply is not None:
        state["last_reply"] = last_reply
    return state


def _opening_resolve_elos(
    db: Database, nick: str, body: OpeningStartBody
) -> tuple[int, int]:
    """Резолвит ``(my_elo, opp_elo)``: явные значения или рейтинг игрока.

    ``my_elo`` — живой рейтинг игрока (Lichess → последняя партия → 1500),
    ``opp_elo`` — на 100 выше, если не задан явно.
    """
    level = _plan_player_level(db, nick)
    rating = level.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating <= 0:
        rating = None
    base = rating or settings.maia_default_elo
    my_elo = body.my_elo if body.my_elo is not None else base
    opp_elo = body.opp_elo if body.opp_elo is not None else base + 100
    return int(my_elo), int(opp_elo)


_OPENING_REPORT_SYSTEM = """Ты — шахматный тренер по дебюту для сильного любителя. Говори по-русски, коротко и конкретно.
Опирайся ТОЛЬКО на переданные факты: ходы дебюта, оценки win%, классификации и лучшие ходы движка. НЕ выдумывай варианты, угрозы и оценки, которых нет в данных.
Ответь строго одним JSON-объектом такой структуры:
{
  "summary": "краткий обзор: вышел ли ты из дебюта нормально (2-3 предложения)",
  "opening": "название дебюта",
  "moments": [
    {"ply": <номер полухода>, "san": "<ход>", "kind": "good|better|bad", "text": "почему", "better": "<лучший ход или —>"}
  ],
  "opponent": "как играл соперник в заданном стиле (1-2 предложения)",
  "recommendation": "что тренировать и по репертуару (1-2 предложения)"
}
В "moments" 2-4 хода: где ты сыграл хорошо, где мог сыграть лучше (и чем именно), где сыграл хуже. Только русский язык, без английского и без разбора по шагам."""


def _opening_fmt_win(value: Any) -> str:
    """Форматирует win% одним знаком или '—' для None/нечисла."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{float(value):.1f}"


def _opening_user_entries(
    analysis: dict[str, Any], user_color: str, plies: int
) -> list[dict[str, Any]]:
    """Ходы пользователя из анализа движка (в пределах дебюта ``plies``)."""
    entries = (analysis.get("moves") or [])[:plies]
    user_is_white = user_color == "white"
    out: list[dict[str, Any]] = []
    for ply, entry in enumerate(entries):
        if (ply % 2 == 0) != user_is_white or not isinstance(entry, dict):
            continue
        out.append(
            {
                "ply": ply + 1,
                "san": entry.get("san"),
                "classification": entry.get("classification"),
                "drop": entry.get("drop"),
                "win_before": entry.get("win_before"),
                "win_after": entry.get("win_after"),
                "best": entry.get("best_move_san"),
            }
        )
    return out


def _opening_analyze_messages(entry: dict[str, Any]) -> list[ChatMessage]:
    """Промпт LLM-отчёта по дебюту: факты построчно + требование JSON."""
    lines = [
        f"Дебют: {entry['opening_name'] or '—'} ({entry['eco'] or '—'}).",
        f"Ты играл за {entry['color_ru']}; соперник — человеко-подобный стиль "
        f"«{entry['style_label']}» ({entry['style_desc']}).",
        f"Рейтинги: ты ~{entry['my_elo']}, соперник ~{entry['opp_elo']}. "
        f"Сыграно полуходов: {entry['plies']}.",
        "Ходы дебюта: " + (" ".join(entry["moves"]) or "—"),
        "",
        "Твои ходы (полуход, ход, win% до→после, потеря, классификация, лучший ход):",
    ]
    if entry["user_entries"]:
        for item in entry["user_entries"]:
            lines.append(
                f"- {item['ply']}. {item['san']} — "
                f"{_opening_fmt_win(item['win_before'])}→{_opening_fmt_win(item['win_after'])}, "
                f"потеря {_opening_fmt_win(item['drop'])} ({item['classification'] or '—'}), "
                f"лучше: {item['best'] or '—'}"
            )
    else:
        lines.append("- (нет данных движка по ходам пользователя)")
    lines.append("")
    lines.append("Ответ — только JSON по заданной структуре.")
    return [
        ChatMessage("system", _OPENING_REPORT_SYSTEM),
        ChatMessage("user", "\n".join(lines)),
    ]


def _normalize_opening_moments(
    raw: Any, fallback_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Нормализует моменты от LLM; без них — детерминированно из ошибок."""
    moments: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            moments.append(
                {
                    "ply": item.get("ply"),
                    "san": item.get("san"),
                    "kind": item.get("kind"),
                    "text": item.get("text"),
                    "better": item.get("better"),
                }
            )
    if not moments:
        for item in fallback_entries:
            if item["classification"] in ("blunder", "mistake", "inaccuracy"):
                moments.append(
                    {
                        "ply": item["ply"],
                        "san": item["san"],
                        "kind": "bad" if item["classification"] == "blunder" else "better",
                        "text": None,
                        "better": item["best"],
                    }
                )
    return moments[:4]


#: Лимит completion для JSON-отчётов: reasoning-модели иначе съедают весь бюджет.
_REPORT_MAX_TOKENS = 7000


def _llm_json_with_retries(
    messages: list[ChatMessage],
    *,
    error_prefix: str,
    temperature: float = 0.4,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """JSON-ответ LLM с тремя попытками.

    Free-модели в ``json_mode`` иногда истрачивают весь лимит на reasoning и
    возвращают пустоту (``finish_reason=length``), а отключение reasoning у
    провайдера тоже может не сработать. Порядок: обычный запрос → без reasoning
    → снова обычный с жёстким запретом текста вокруг JSON.
    """
    llm = LLMClient()
    user_hint = "\nБез пошагового разбора. Сразу один JSON-объект."
    strict_hint = (
        "\nОтветь ТОЛЬКО валидным JSON-объектом: без markdown, "
        "без текста до и после."
    )
    last = messages[-1]
    attempts: list[tuple[list[ChatMessage], dict[str, Any]]] = [
        (messages, {}),
        (
            messages[:-1] + [ChatMessage("user", last.content + user_hint)],
            {"reasoning_max_tokens": 0},
        ),
        (
            messages[:-1] + [ChatMessage("user", last.content + strict_hint)],
            {},
        ),
    ]
    last_error: RuntimeError | None = None
    for attempt_messages, kwargs in attempts:
        try:
            text = clean_llm_text(
                llm.chat(
                    attempt_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=True,
                    **kwargs,
                )
            )
            data = _extract_json(text) if isinstance(text, str) else None
        except RuntimeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data
    detail = f" (последняя ошибка: {last_error})" if last_error else ""
    raise RuntimeError(f"{error_prefix}: 3 попытки не дали JSON{detail}")


def _opening_report_llm(entry: dict[str, Any]) -> dict[str, Any]:
    """LLM-отчёт по дебюту как JSON-словарь (3 попытки, см. хелпер)."""
    return _llm_json_with_retries(
        _opening_analyze_messages(entry),
        error_prefix="LLM не вернул JSON-отчёт по дебюту",
        max_tokens=_REPORT_MAX_TOKENS,
    )


def _build_opening_report(
    session: dict[str, Any],
    moves: list[str],
    user_entries: list[dict[str, Any]],
    opening: tuple[str | None, str | None],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Stockfish-анализ + LLM-отчёт в одном JSON (русский, как coach.py).

    Бросает ``RuntimeError`` при сбое LLM — job уходит в error, сессия не
    меняется (report в БД не пишется).
    """
    name, eco = opening
    color = session.get("color") or "white"
    style = session.get("style") or "universal"
    entry = {
        "opening_name": name,
        "eco": eco,
        "color_ru": "белые" if color == "white" else "чёрные",
        "style_label": STYLE_LABELS_RU.get(style, style),
        "style_desc": STYLE_DESCRIPTIONS_RU.get(style, ""),
        "my_elo": session.get("my_elo"),
        "opp_elo": session.get("opp_elo"),
        "plies": len(moves),
        "moves": moves,
        "user_entries": user_entries,
    }
    _ensure_llm_key()
    data = _opening_report_llm(entry)
    return {
        "opening": {"name": name or data.get("opening"), "eco": eco},
        "summary": data.get("summary"),
        "moments": _normalize_opening_moments(data.get("moments"), user_entries),
        "opponent": data.get("opponent"),
        "recommendation": data.get("recommendation"),
        "style": style,
        "my_elo": session.get("my_elo"),
        "opp_elo": session.get("opp_elo"),
        "engine": "stockfish",
    }


def _run_opening_analyze_job(
    app: FastAPI, job: CoachJob, session_id: str
) -> None:
    """Фоновый поток: Maia off → Stockfish-анализ дебюта → LLM-отчёт (JSON)."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()
            session = db.get_opening_session(session_id)
        if session is None:
            raise RuntimeError("Сессия тренажёра дебютов не найдена")
        moves = list(session.get("moves") or [])
        if not moves:
            raise RuntimeError("Сессия пуста — анализировать нечего")

        # Память на Eco не резиновая: Maia (ONNX ~179MB) и Stockfish вместе
        # не помещаются, поэтому до подъёма движка освобождаем сессию Maia.
        job.phase = "Освобождение Maia"
        _persist_job(app.state.db_path, job, session_id=session_id)
        _unload_maia()

        job.phase = "Анализ движком"
        _persist_job(app.state.db_path, job, session_id=session_id)
        game = Game(
            id=f"opening-{session_id}",
            moves=moves,
            user_color=session.get("color") or "white",
            opponent="Maia",
            result_for_user="draw",
            status="normal",
        )
        with Engine(
            depth=settings.stockfish_depth, multipv=settings.stockfish_multipv
        ) as engine:
            analysis = engine.analyze_game(game).summary()

        user_entries = _opening_user_entries(
            analysis, session.get("color") or "white", int(settings.opening_plies)
        )
        opening = classify_opening(moves)

        job.phase = "Отчёт тренера"
        _persist_job(app.state.db_path, job, session_id=session_id)
        report = _build_opening_report(
            session, moves, user_entries, opening, analysis
        )

        with Database(app.state.db_path) as db:
            db.init_db()
            db.update_opening_session(
                session_id, report=report, status=_OPENING_ANALYZED
            )
        job.result = {"report": report}
        job.summary = f"Отчёт по дебюту готов ({len(moves)} полуходов)"
        job.status = "done"
        _persist_job(app.state.db_path, job, session_id=session_id)
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        _persist_job(app.state.db_path, job, session_id=session_id)
    finally:
        job.finished = time.time()


# --- Тренер миттельшпиля: сессии, Maia и фоновый разбор ---------------------

_MIDDLEGAME_STRENGTHS = {"equal": 0, "plus100": 100, "plus200": 200}
_MIDDLEGAME_ELO_MIN = 100
_MIDDLEGAME_ELO_MAX = 3500


def _middlegame_base_elo(db: Database, nick: str) -> int:
    """Рейтинг игрока для силы соперника: тот же источник, что у дебютов."""
    level = _plan_player_level(db, nick)
    rating = level.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating <= 0:
        rating = settings.maia_default_elo
    return max(_MIDDLEGAME_ELO_MIN, min(_MIDDLEGAME_ELO_MAX, int(rating)))


def _middlegame_opp_elo(base: int, strength: str) -> int:
    """Elo соперника по выбранной силе. hybrid -> -1 (sentinel для гибрида)."""
    if strength == "hybrid":
        return -1
    delta = _MIDDLEGAME_STRENGTHS[strength]
    return max(_MIDDLEGAME_ELO_MIN, min(_MIDDLEGAME_ELO_MAX, int(base) + delta))


def _middlegame_payload(session: mg.MiddlegameSession) -> dict[str, Any]:
    """JSON сессии для API: opp_elo всегда есть, даже до выбора силы."""
    data = session.to_dict()
    data.setdefault("opp_elo", None)
    return data


def _middlegame_pick_san(candidates: list[tuple[str, float]]) -> str | None:
    """Сэмплирует SAN по вероятностям Maia (как ход соперника в дебютах)."""
    cleaned: list[tuple[str, float]] = []
    for san, prob in candidates or []:
        try:
            weight = float(prob)
        except (TypeError, ValueError):
            continue
        if not san or weight <= 0:
            continue
        cleaned.append((str(san), weight))
    if not cleaned:
        return None
    threshold = random.random() * sum(weight for _, weight in cleaned)
    acc = 0.0
    for san, weight in cleaned:
        acc += weight
        if threshold <= acc:
            return san
    return cleaned[-1][0]


def _middlegame_side_win(eval_white: dict[str, Any], white_side: bool) -> float:
    """Win% стороны по оценке из перспективы белых (cp/mate)."""
    mate = eval_white.get("mate")
    cp = eval_white.get("cp")
    try:
        mate_value = int(mate) if mate is not None else None
    except (TypeError, ValueError):
        mate_value = None
    cp_value: float | None
    try:
        cp_value = float(cp) if cp is not None else None
    except (TypeError, ValueError):
        cp_value = None
    win_white = win_percent(cp_value, mate_value)
    return win_white if white_side else 100.0 - win_white


def _middlegame_engine_facts(session: mg.MiddlegameSession) -> list[dict[str, Any]]:
    """Факты Stockfish по каждому полуходу от стартового FEN сессии."""
    facts: list[dict[str, Any]] = []
    board = chess.Board(session.fen)
    moves = list(session.moves[: mg.MAX_PLIES])
    with Engine(
        depth=settings.stockfish_depth, multipv=settings.stockfish_multipv
    ) as engine:
        lines = engine.analyze_position(board)
        for ply, uci in enumerate(moves, start=1):
            if not lines or board.is_game_over(claim_draw=False):
                break
            mover_white = board.turn == chess.WHITE
            top = lines[0]
            eval_before = dict(top.get("eval") or {})
            pv_before = [str(move) for move in (top.get("pv") or [])][:4]
            best_san = pv_before[0] if pv_before else None
            second_san = None
            if len(lines) > 1:
                second_pv = lines[1].get("pv") or []
                second_san = second_pv[0] if second_pv else None
            win_before = _middlegame_side_win(eval_before, mover_white)
            try:
                move = chess.Move.from_uci(uci)
                san = board.san(move)
            except ValueError:
                break
            board.push(move)
            if board.is_game_over(claim_draw=False):
                if board.is_checkmate():
                    eval_after = {
                        "cp": None,
                        "mate": 1 if mover_white else -1,
                    }
                else:
                    eval_after = {"cp": 0.0, "mate": None}
                pv_after: list[str] = []
                lines = []
            else:
                next_lines = engine.analyze_position(board)
                if not next_lines:
                    break
                eval_after = dict(next_lines[0].get("eval") or {})
                pv_after = [
                    str(chess_move)
                    for chess_move in (next_lines[0].get("pv") or [])
                ][:4]
                lines = next_lines
            win_after = _middlegame_side_win(eval_after, mover_white)
            facts.append(
                {
                    "ply": ply,
                    "san": san,
                    "uci": move.uci(),
                    "eval_before": eval_before,
                    "eval_after": eval_after,
                    "delta": round(max(0.0, win_before - win_after), 2),
                    "best_san": best_san,
                    "second_san": second_san,
                    "pv": pv_before,
                    "pv_after": pv_after,
                }
            )
    return facts


def _middlegame_report_llm(prompt: str) -> dict[str, Any]:
    """LLM-отчёт по миттельшпилю (3 попытки, см. хелпер)."""
    _ensure_llm_key()
    return _llm_json_with_retries(
        [ChatMessage("user", prompt)],
        error_prefix="LLM не вернул JSON-отчёт по миттельшпилю",
        temperature=0.3,
        max_tokens=_REPORT_MAX_TOKENS,
    )


def _run_middlegame_analyze_job(
    app: FastAPI, job: CoachJob, session_id: str
) -> None:
    """Фоновый поток: Maia off → Stockfish-факты → LLM-отчёт (JSON)."""
    try:
        with Database(app.state.db_path) as db:
            db.init_db()
            session = db.get_middlegame_session(session_id)
        if session is None:
            raise RuntimeError("Сессия тренажёра миттельшпиля не найдена")
        moves = list(session.moves[: mg.MAX_PLIES])
        if not moves:
            raise RuntimeError("Сессия пуста — анализировать нечего")

        job.phase = "Освобождение Maia"
        _persist_job(app.state.db_path, job, session_id=session_id)
        _unload_maia()

        job.phase = "Анализ движком"
        _persist_job(app.state.db_path, job, session_id=session_id)
        engine_facts = _middlegame_engine_facts(session)

        job.phase = "Отчёт тренера"
        _persist_job(app.state.db_path, job, session_id=session_id)
        prompt = mg.build_report_prompt(
            session,
            engine_facts=engine_facts,
            position=mg.POSITION_BY_KEY.get(session.position_key),
        )
        report = mg.ground_report(
            mg.normalize_report(_middlegame_report_llm(prompt)),
            engine_facts,
            fen=session.fen,
            moves=list(session.moves),
        )

        with Database(app.state.db_path) as db:
            db.init_db()
            fresh = db.get_middlegame_session(session_id) or session
            fresh.report = report
            fresh.status = "analyzed"
            db.update_middlegame_session(fresh)
        job.result = {"report": report}
        job.summary = f"Отчёт по миттельшпилю готов ({len(moves)} полуходов)"
        job.status = "done"
        _persist_job(app.state.db_path, job, session_id=session_id)
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        _persist_job(app.state.db_path, job, session_id=session_id)
    finally:
        job.finished = time.time()
        with _MIDDLEGAME_JOBS_LOCK:
            if _MIDDLEGAME_JOBS.get(session_id) == job.id:
                _MIDDLEGAME_JOBS.pop(session_id, None)


# --- helpers ---------------------------------------------------------------

_SESSION_COOKIE = "ct_session"
_SESSION_DAYS = 30
_SESSION_MAX_AGE = _SESSION_DAYS * 24 * 3600


def _is_active_record(rec: dict | None) -> bool:
    """Запись пользователя активна (is_active != 0; NULL/мусор → активна)."""
    if rec is None:
        return False
    try:
        return int(rec.get("is_active") if rec.get("is_active") is not None else 1) != 0
    except (TypeError, ValueError):
        return True


def _session_nick(request: Request | None, db: Database) -> str | None:
    """Ник по сессии из cookie (только активные записи), иначе None."""
    token = request.cookies.get(_SESSION_COOKIE) if request is not None else None
    if not token:
        return None
    nick = db.get_session_nick(token)
    if not nick:
        return None
    rec = db.get_user_record(nick)
    if not rec or not _is_active_record(rec):
        return None
    return rec["nick"]


def _current_user(db_path: Path, request: Request | None = None) -> dict | None:
    """Текущий пользователь: сначала сессия из cookie, иначе meta current_user.

    Fallback на meta нужен CLI и старым клиентам без cookie. Неактивный
    пользователь (is_active=0) — везде None (HTTP 404).
    """
    with Database(db_path) as db:
        db.init_db()
        if request is not None:
            token = request.cookies.get(_SESSION_COOKIE)
            if token:
                nick = db.get_session_nick(token)
                if nick:
                    rec = db.get_user_record(nick)
                    if rec is not None:
                        if not _is_active_record(rec):
                            return None
                        return rec
        user = db.get_current_user()
        if user is not None and not _is_active_record(user):
            return None
        return user


def _lichess_of(db: Database, nick: str) -> str:
    """Lichess-ник пользователя для забора партий; fallback — ник сайта."""
    try:
        rec = db.get_user_record(nick)
    except (ValueError, TypeError):
        rec = None
    if rec:
        lich = (rec.get("lichess") or "").strip()
        if lich:
            return lich
    return nick


def _set_session_cookie(response: Response, token: str) -> None:
    """Выставляет cookie сессии (httponly, samesite=lax, 30 дней)."""
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _call_data(fn, db_path: Path):
    """Вызывает слой данных; RuntimeError («нет партий») → HTTP 404.

    ValueError тоже → 404: битый JSON в БД (JSONDecodeError — подкласс
    ValueError) иначе давал 500 вместо честного «не найдено».
    """
    try:
        with Database(db_path) as db:
            return fn(db)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


def _engine_ready() -> bool:
    """Есть ли бинарь Stockfish по настроенному пути (pre-flight до job'ы).

    Проверяется синхронно в /api/coach/run: если движка нет, отвечаем 503
    с понятным текстом, а не создаём job, который упадёт в error внутри потока.
    """
    return bool(settings.stockfish_path) and os.path.exists(settings.stockfish_path)


def _humanity_payload(raw: dict) -> dict:
    """Читаемый срез humanity.json: сводка + все ошибки по порядку."""
    verdicts = raw.get("verdicts") or {}
    return {
        "summary": {
            "total": int(raw.get("total_bad") or 0),
            "natural": int(verdicts.get("natural") or 0),
            "borderline": int(verdicts.get("borderline") or 0),
            "unnatural": int(verdicts.get("unnatural") or 0),
            "no_data": int(verdicts.get("no-data") or 0),
        },
        "avg_beta": raw.get("avg_beta"),
        "items": [item for item in (raw.get("items") or []) if isinstance(item, dict)],
    }

# --- Задача дня -------------------------------------------------------------

_TASK_ERROR_CLASSES = ("blunder", "mistake")

_TASK_EXPLAIN_SYSTEM = """Ты — шахматный тренер сильного любителя. Говори по-русски, коротко и конкретно.
Опирайся ТОЛЬКО на переданные факты: сыгранный ход, лучший ход движка, оценки win%,
классификацию и контекст ходов. НЕ выдумывай варианты, угрозы и оценки, которых нет в данных.
Отвечай СРАЗУ готовым объяснением, без промежуточного разбора. Только русский язык. 3–5 коротких предложений, живым тоном тренера: 1) что за ошибка, 2) почему сыгранный ход плох, 3) идея лучшего хода, 4) на что смотреть в похожих позициях. Не выдумывай варианты и угрозы вне данных. Никакого английского."""


def _today() -> str:
    """Сегодняшняя дата приложения (``APP_TZ``, по умолчанию Asia/Almaty)."""
    return timeutil.today()


def _task_choice_key(date: str, user: str) -> str:
    """Ключ meta с зафиксированным выбором задачи на дату (на пользователя)."""
    return f"daily_task_{date}_{user}"


def _task_done_key(date: str, user: str) -> str:
    """Ключ meta: задача за дату решена (ответ дан)."""
    return f"daily_done_{date}_{user}"


def _task_pool(db: Database, user: str) -> list[dict[str, Any]]:
    """Пул кандидатов в задачу дня: ошибки (blunder/mistake) самого пользователя.

    FEN позиции ДО хода считается на месте при проходе партии; попутно
    проверяется parity — если сторона, чей ход, не совпала с цветом игрока,
    запись пропускается. Сортировка стабильная: (created_at, game_id, ply).
    """
    pool: list[dict[str, Any]] = []
    for game, analysis in db.get_analyzed_games(user, limit=500):
        user_is_white = game.user_color == "white"
        entries = analysis.get("moves") or []
        board = chess.Board()
        for ply, san in enumerate(game.moves):
            entry = entries[ply] if ply < len(entries) else None
            is_user_move = (ply % 2 == 0) == user_is_white
            user_side = chess.WHITE if user_is_white else chess.BLACK
            if (
                is_user_move
                and board.turn == user_side
                and isinstance(entry, dict)
                and entry.get("classification") in _TASK_ERROR_CLASSES
                and isinstance(entry.get("drop"), (int, float))
                and entry.get("best_move_san")
            ):
                pool.append(
                    {
                        "game": game,
                        "ply": ply,
                        "fen": board.fen(),
                        "move": entry,
                        "created_at": game.created_at,
                        "task_key": f"{game.id}|{ply}",
                    }
                )
            try:
                board.push_san(san)
            except ValueError:
                break
    pool.sort(key=lambda item: (item["created_at"], item["game"].id, item["ply"]))
    return pool


def _pick_daily_task(db: Database, user: str, date: str) -> dict[str, Any]:
    """Одна детерминированная задача на день: выбор фиксируется в meta.

    Первый запрос за день выбирает задачу (sha256(дата+user) % len(pool))
    и записывает её; повторные читают записанную. Если записанная задача
    больше не найдена в пуле (удалили партию) — выбирается новая.
    """
    pool = _task_pool(db, user)
    if not pool:
        raise RuntimeError(
            f"Для {user} нет проанализированных ошибок — сначала запусти анализ "
            "(страница «Анализ»), тогда появится задача дня."
        )
    key = _task_choice_key(date, user)
    stored = db.get_meta(key)
    if stored:
        for item in pool:
            if item["task_key"] == stored:
                return item
    digest = int(hashlib.sha256(f"{date}{user}".encode("utf-8")).hexdigest(), 16)
    chosen = pool[digest % len(pool)]
    db.set_meta(key, chosen["task_key"])
    return chosen


def _is_bad_explanation(text: Any) -> bool:
    """True, если объяснение — мусор: thinking-разбор, safety-отказ или огрызок.

    Объяснение обязано быть 3–5 предложений, поэтому текст короче 80 символов
    (после strip) тоже считается плохим.
    """
    if not isinstance(text, str):
        return True
    if looks_like_thinking(text) or looks_like_refusal(text):
        return True
    return len(text.strip()) < 80


def _task_payload(db: Database, user: str, date: str | None = None) -> dict[str, Any]:
    """JSON для /api/task/today: позиция до хода, решение и объяснение."""
    day = date or _today()
    item = _pick_daily_task(db, user, day)
    game = item["game"]
    move = item["move"]
    ply = item["ply"]
    best = str(move.get("best_move_san"))
    # Конкретная линия для просмотра: UCI первого хода, сыгранный ход и FEN
    # после каждого полухода лучшей линии (фронтенд листает их без шахматной
    # библиотеки на клиенте).
    task_board = chess.Board(item["fen"])
    best_uci = ""
    played_uci = ""
    try:
        best_uci = task_board.parse_san(best).uci()
    except ValueError:
        pass
    played_san = str(move.get("san") or game.moves[ply])
    try:
        played_uci = task_board.parse_san(played_san).uci()
    except ValueError:
        pass
    best_line_fens: list[str] = []
    cursor = task_board.copy()
    for line_san in [best, *[str(s) for s in (move.get("best_line") or [])]]:
        try:
            line_move = cursor.parse_san(line_san)
        except ValueError:
            break
        cursor.push(line_move)
        best_line_fens.append(cursor.fen())
    explanation = db.get_task_explanation(item["task_key"])
    if explanation is not None and _is_bad_explanation(explanation):
        # Отравленный кэш (reasoning-разбор/safety-заглушка/огрызок вместо ответа):
        # игнорируем, кнопка «Объяснить» снова доступна, успешный повтор перезапишет
        # мусор через UPSERT save_task_explanation. Строки из БД не удаляем.
        explanation = None
    return {
        "date": day,
        "task_key": item["task_key"],
        "game_id": game.id,
        "link": game.link,
        "ply": ply,
        "fen": item["fen"],
        "san_played": str(move.get("san") or game.moves[ply]),
        "best_san": best,
        "best_line": [str(s) for s in (move.get("best_line") or [])],
        "best_uci": best_uci,
        "played_uci": played_uci,
        "best_line_fens": best_line_fens,
        "classification": move.get("classification"),
        "drop": move.get("drop"),
        "win_before": move.get("win_before"),
        "win_after": move.get("win_after"),
        "opening": game.opening,
        "eco": game.eco,
        "result_for_user": game.result_for_user,
        "opponent": game.opponent,
        "user_color": game.user_color,
        "moves_before": game.moves[max(0, ply - 6) : ply],
        "solution": {
            "san_played": str(move.get("san") or game.moves[ply]),
            "best_san": best,
            "drop": move.get("drop"),
        },
        "explanation": explanation,
        "explained": explanation is not None,
        "done": db.get_meta(_task_done_key(day, user)) == "1",
    }


# --- План дня, снапшоты рейтинга и админ-когорта ---------------------------

# Снапшоты рейтинга ходят в сеть; тесты API выключают флаг, чтобы не
# обращаться к Lichess и не плодить фоновые потоки.
_RATING_SNAPSHOT_ENABLED = True


def _rating_try_key(day: str, user: str) -> str:
    """Ключ meta: попытка снапшота рейтинга за день уже была."""
    return f"rating_snapshot_try_{day}_{user}"


def _snapshot_rating_worker(db_path: Path, nick: str, day: str) -> None:
    """Фоновый снапшот рейтинга Lichess; любые ошибки молча глотаем."""
    try:
        with Database(db_path) as db:
            db.init_db()
            data = fetch_lichess_rating(_lichess_of(db, nick))
            if isinstance(data, dict):
                rating = data.get("rating")
                if (
                    isinstance(rating, int)
                    and not isinstance(rating, bool)
                    and rating > 0
                ):
                    db.save_rating_snapshot(nick, day, rating)
    except Exception:  # noqa: BLE001 — фоновый путь не должен ронять запрос
        logger.debug("rating snapshot failed for %s/%s", nick, day, exc_info=True)


def _maybe_snapshot_rating(db: Database, nick: str, day: str) -> None:
    """Запускает снапшот не чаще раза в день (успех и ошибка считаются).

    Маркер попытки в meta ставится до потока: если Lichess недоступен,
    каждый ping не будет заново ждать таймаут.
    """
    if not _RATING_SNAPSHOT_ENABLED:
        return
    try:
        if db.get_rating_snapshot(nick, day) is not None:
            return
        marker = _rating_try_key(day, nick)
        if db.get_meta(marker) == "1":
            return
        db.set_meta(marker, "1")
        threading.Thread(
            target=_snapshot_rating_worker,
            args=(db.path, nick, day),
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — трекер времени важнее рейтинга
        logger.debug("cannot start rating snapshot for %s", nick, exc_info=True)


def _parse_day(raw: str | None) -> str:
    """Дата из query/body строго ``YYYY-MM-DD``; пусто — сегодня."""
    text = (raw or "").strip()
    if not text:
        return _today()
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise HTTPException(400, "Дата: формат YYYY-MM-DD") from None


def _dayplan_payload(
    db: Database, nick: str, day: str, data_dir: Path
) -> dict[str, Any]:
    """День плана: строит и сохраняет задачи при первом запросе.

    Повторный вызов читает сохранённое, поэтому ``done`` и порядок задач
    стабильны. Если аналитики ещё нет, план пустой: останутся задача дня
    (если есть ошибки) и разбор.
    """
    tasks = db.get_daily_plan(nick, day)
    if not tasks:
        plan: dict[str, Any] = {}
        try:
            plan = shared_data.plan(db, nick, root=data_dir)
        except (RuntimeError, ValueError):
            plan = {}
        task = None
        try:
            task = _task_payload(db, nick, day)
        except (RuntimeError, ValueError):
            task = None
        built = build_tasks(plan, task, day)
        if built:
            db.save_daily_plan(nick, day, built)
            tasks = db.get_daily_plan(nick, day) or built

    # Задача дня решается на своей странице (/api/task/answer) — meta
    # daily_done становится источником правды, отмечаем её и в плане.
    if db.get_meta(_task_done_key(day, nick)) == "1":
        for item in tasks:
            if item.get("kind") == "task" and not item.get("done"):
                if db.toggle_daily_task(nick, day, str(item.get("id")), True):
                    item["done"] = True

    done = sum(1 for item in tasks if item.get("done"))
    total = len(tasks)
    log = db.get_daily_log(nick, day) or {}
    if (
        int(log.get("tasks_done") or 0) != done
        or int(log.get("tasks_total") or 0) != total
    ):
        db.upsert_daily_log(nick, day, tasks_done=done, tasks_total=total)
        log = db.get_daily_log(nick, day) or log
    return {
        "date": day,
        "tasks": tasks,
        "done": int(log.get("tasks_done") or done),
        "total": int(log.get("tasks_total") or total),
        "minutes": int(log.get("minutes") or 0),
        "streak_days": db.streak_days(nick, day),
    }


def _admin_user(request: Request, db_path: Path) -> dict | None:
    """Текущий пользователь, если он админ; иначе HTTP 403."""
    user = _current_user(db_path, request)
    if not user:
        raise HTTPException(403, "Доступ только для админа")
    with Database(db_path) as db:
        db.init_db()
        if not is_admin(db, user["nick"]):
            raise HTTPException(403, "Доступ только для админа")
    return user


def _last_seen_iso(value: Any) -> str | None:
    """``users.last_seen_at`` (секунды) → ISO с поясом приложения."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            seconds, timeutil.timezone_for()
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def _cohort_payload(db: Database, days: int) -> dict[str, Any]:
    """Когорта за окно ``days``: рейтинг, активность, задачи, анализы."""
    end_day = timeutil.today()
    count = min(3650, max(1, int(days)))
    rows: list[dict[str, Any]] = []
    for record in db.list_users():
        nick = str(record.get("nick") or "").strip()
        if not nick:
            continue
        snapshots = [
            item
            for item in db.list_rating_snapshots(nick, count, end_day)
            if item.get("rating") is not None
        ]
        rating_start = snapshots[0]["rating"] if snapshots else None
        rating_end = snapshots[-1]["rating"] if snapshots else None
        rating_delta = (
            int(rating_end) - int(rating_start)
            if rating_start is not None and rating_end is not None
            else None
        )

        logs = db.list_daily_logs(nick, count, end_day)
        active = [
            item
            for item in logs
            if int(item.get("tasks_done") or 0) > 0 or int(item.get("minutes") or 0) > 0
        ]
        days_active = len(active)
        minutes_total = sum(int(item.get("minutes") or 0) for item in logs)
        tasks_done = sum(int(item.get("tasks_done") or 0) for item in logs)
        tasks_total = sum(int(item.get("tasks_total") or 0) for item in logs)
        completion_pct = (
            round(tasks_done / tasks_total * 100.0, 1) if tasks_total else 0.0
        )
        improved_rating = rating_delta is not None and rating_delta >= 30
        improved_activity = completion_pct >= 60.0 and days_active >= 10
        rows.append(
            {
                "nick": nick,
                "rating_start": rating_start,
                "rating_end": rating_end,
                "rating_delta": rating_delta,
                "days_active": days_active,
                "minutes_total": minutes_total,
                "avg_minutes_per_day": (
                    round(minutes_total / days_active, 1) if days_active else 0.0
                ),
                "tasks_done": tasks_done,
                "tasks_total": tasks_total,
                "completion_pct": completion_pct,
                "games_analyzed": sum(
                    int(item.get("games_analyzed") or 0) for item in logs
                ),
                "analyses": sum(int(item.get("analyses") or 0) for item in logs),
                "last_seen": _last_seen_iso(record.get("last_seen_at")),
                "improved_rating": improved_rating,
                "improved_activity": improved_activity,
                "improved": improved_rating or improved_activity,
            }
        )

    return {
        "days": count,
        "users": rows,
        "summary": {
            "users": len(rows),
            "active_users": sum(1 for row in rows if row["days_active"] > 0),
            "improved": sum(1 for row in rows if row["improved"]),
            "improved_by_rating": sum(
                1 for row in rows if row["improved_rating"]
            ),
            "improved_by_activity": sum(
                1 for row in rows if row["improved_activity"]
            ),
            "avg_rating_delta": _mean(
                [
                    float(row["rating_delta"])
                    for row in rows
                    if row["rating_delta"] is not None
                ]
            ),
            "avg_completion_pct": _mean(
                [float(row["completion_pct"]) for row in rows]
            ),
            "avg_minutes_per_day": _mean(
                [float(row["avg_minutes_per_day"]) for row in rows]
            ),
            "avg_days_active": _mean([float(row["days_active"]) for row in rows]),
        },
    }


_COHORT_CSV_COLUMNS = (
    "nick",
    "rating_start",
    "rating_end",
    "rating_delta",
    "days_active",
    "minutes_total",
    "avg_minutes_per_day",
    "tasks_done",
    "tasks_total",
    "completion_pct",
    "games_analyzed",
    "analyses",
    "improved",
    "improved_rating",
    "improved_activity",
    "last_seen",
)


def _cohort_csv(rows: list[dict[str, Any]]) -> str:
    """CSV когорты (RFC 4180, UTF-8, перевод строки ``\\n``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_COHORT_CSV_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                int(row[column])
                if isinstance(row.get(column), bool)
                else ("" if row.get(column) is None else row.get(column))
                for column in _COHORT_CSV_COLUMNS
            ]
        )
    return buffer.getvalue()


def _fmt_win(value: Any) -> str:
    """Оформление win% для промпта (один знак, '—' для None)."""
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.1f}"


def _task_explain_messages(payload: dict[str, Any]) -> list[ChatMessage]:
    """Промпт LLM-объяснения задачи (по-русски, только факты из данных)."""
    color = "белые" if payload["user_color"] == "white" else "чёрные"
    context = " ".join(payload["moves_before"]) or "—"
    line = payload.get("best_line") or []
    lines = [
        f"Позиция (FEN): {payload['fen']}",
        f"Партия: ты играл за {color}; дебют: {payload['opening'] or '—'} "
        f"({payload['eco'] or '—'}); соперник: {payload['opponent'] or '—'}; "
        f"результат: {payload['result_for_user']}.",
        f"Контекст — ходы до позиции: {context}",
        f"Твой ход: {payload['san_played']} — классификация {payload['classification']}, "
        f"win% {_fmt_win(payload['win_before'])} → {_fmt_win(payload['win_after'])}, "
        f"потеря {_fmt_win(payload['drop'])}.",
        f"Лучший ход движка: {payload['best_san']}"
        + (f"; его продолжение: {' '.join(line)}" if line else "")
        + ".",
        "Объясни по-русски в 3-5 предложений: в чём ошибка моего хода, какую идею "
        "или угрозу я не увидел и о чём думать в похожих позициях. Только факты из данных выше. "
        "Ответ — только объяснение, без разбора по шагам.",
    ]
    return [
        ChatMessage("system", _TASK_EXPLAIN_SYSTEM),
        ChatMessage("user", "\n".join(lines)),
    ]


def _task_explain_llm(payload: dict[str, Any]) -> str:
    """Один вызов LLM для объяснения задачи (с повтором при плохом ответе).

    thinking-блоки вырезаются, thinking-разбор/safety-заглушка/огрызок
    вместо ответа — это ошибка (job уйдёт в error, а не сохранит мусор в БД).
    """
    llm = LLMClient()
    messages = _task_explain_messages(payload)
    text = clean_llm_text(llm.chat(messages, temperature=0.4))
    if text and not _is_bad_explanation(text):
        return text
    retry = [
        messages[0],
        ChatMessage(
            "user",
            messages[1].content + "\nБез разбора. Сразу объяснение по-русски.",
        ),
    ]
    second = clean_llm_text(llm.chat(retry, temperature=0.4))
    if not second or _is_bad_explanation(second):
        raise RuntimeError("Модель не дала объяснение задачи (повтор не помог)")
    return second

# --- Цель и недельный план ------------------------------------------------

_PLAN_GOAL_FOCUSES = ("auto", "opening", "middlegame", "endgame")
_PLAN_GOAL_TYPES = ("rating", "tournament", "fix_weak")
_PLAN_GOAL_TYPE_RU = {
    "rating": "поднять рейтинг",
    "tournament": "подготовка к турниру",
    "fix_weak": "исправить слабые места",
}
_PLAN_GOAL_DEFAULT_TYPE = "fix_weak"
_PLAN_FOCUS_RU = {
    "opening": "дебют",
    "middlegame": "миттельшпиль",
    "endgame": "эндшпиль",
}
# Фазы в build_plan/build_report: opening | midgame | endgame.
_INTERNAL_TO_PUBLIC_PHASE = {
    "opening": "opening",
    "midgame": "middlegame",
    "endgame": "endgame",
}
_PLAN_GOAL_DEFAULT_MINUTES = 30


def _plan_goal_key(nick: str) -> str:
    """Ключ meta с целью игрока (JSON)."""
    return f"plan_goal_{nick}"


def _parse_plan_date(raw: Any) -> datetime.date | None:
    """Строгий разбор YYYY-MM-DD; мусор (включая не-строки) → None.

    datetime.date.fromisoformat в новых Python lenient (ест и "20260101"),
    поэтому формат проверяем вручную: ровно 10 символов и дефисы на местах.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if (
        len(text) != 10
        or text[4] != "-"
        or text[7] != "-"
        or not (text[:4] + text[5:7] + text[8:]).isdigit()
    ):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _plan_weeks_left(
    tournament_date: str | None, today: str | None = None
) -> int | None:
    """Недель до турнира от _today(): ceil((date-today)/7), минимум 0.

    Без даты или с битой датой → None. Прошедшая дата → 0.
    """
    if not tournament_date:
        return None
    day = _parse_plan_date(tournament_date)
    if day is None:
        return None
    try:
        now = datetime.date.fromisoformat((today or _today()).strip())
    except (ValueError, AttributeError):
        return None
    diff = (day - now).days
    if diff <= 0:
        return 0
    return (diff + 6) // 7


def _load_plan_goal(db: Database, nick: str) -> dict[str, Any]:
    """Цель из meta: goal/minutes/focus + goal_type/tournament_date/target_rating.

    Битый/частичный JSON чинится дефолтами, исключения не выбрасываются.
    Старые записи без новых ключей читаются с дефолтами (dict.get).
    """
    result: dict[str, Any] = {
        "goal": None,
        "minutes": _PLAN_GOAL_DEFAULT_MINUTES,
        "focus": "auto",
        "goal_type": _PLAN_GOAL_DEFAULT_TYPE,
        "tournament_date": None,
        "target_rating": None,
    }
    raw = db.get_meta(_plan_goal_key(nick))
    if not raw:
        return result
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    goal = data.get("goal")
    if isinstance(goal, str) and goal.strip():
        result["goal"] = goal.strip()[:200]
    minutes = data.get("minutes")
    if isinstance(minutes, bool):
        pass
    elif isinstance(minutes, (int, float)) and 5 <= int(minutes) <= 480:
        result["minutes"] = int(minutes)
    focus = data.get("focus")
    if isinstance(focus, str) and focus in _PLAN_GOAL_FOCUSES:
        result["focus"] = focus
    goal_type = data.get("goal_type")
    if isinstance(goal_type, str) and goal_type.strip() in _PLAN_GOAL_TYPES:
        result["goal_type"] = goal_type.strip()
    tournament_date = data.get("tournament_date")
    if isinstance(tournament_date, str) and _parse_plan_date(tournament_date) is not None:
        result["tournament_date"] = tournament_date.strip()
    target_rating = data.get("target_rating")
    if isinstance(target_rating, bool):
        pass
    elif isinstance(target_rating, (int, float)) and 800 <= int(target_rating) <= 3200:
        result["target_rating"] = int(target_rating)
    elif (
        isinstance(target_rating, str)
        and target_rating.strip().isdigit()
        and 800 <= int(target_rating.strip()) <= 3200
    ):
        result["target_rating"] = int(target_rating.strip())
    return result


def _week_phases(plan: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Фазы ошибок из плана как [(публичная фаза, статистика)].

    Принимает и формат build_plan (список пар), и build_report (словарь).
    Внутренний 'midgame' отдаётся наружу как 'middlegame'.
    """
    if not isinstance(plan, dict):
        return []
    raw = (plan.get("patterns") or {}).get("phases")
    if isinstance(raw, dict):
        raw = list(raw.items())
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        name, stats = entry
        public = _INTERNAL_TO_PUBLIC_PHASE.get(name)
        if public is None or not isinstance(stats, dict):
            continue
        out.append((public, stats))
    return out


def _resolve_week_focus(
    goal: dict[str, Any], plan: dict[str, Any] | None
) -> tuple[str, str]:
    """Фаза фокуса недели + человеческое обоснование с цифрами.

    Явный фокус цели побеждает; иначе — слабейшая фаза по avg_drop;
    без данных — 'middlegame' по умолчанию.
    """
    phases = _week_phases(plan)
    by_phase = {name: stats for name, stats in phases}
    wanted = goal.get("focus") or "auto"
    if wanted != "auto":
        stats = by_phase.get(wanted)
        if stats:
            count = int(stats.get("count") or 0)
            avg = float(stats.get("avg_drop") or 0.0)
            return wanted, f"твой выбор; {count} ошибок, средняя потеря {avg:.1f}%"
        return wanted, "выбран тобой в цели"
    if phases:
        name, stats = max(
            phases, key=lambda item: float((item[1] or {}).get("avg_drop") or 0.0)
        )
        count = int(stats.get("count") or 0)
        avg = float(stats.get("avg_drop") or 0.0)
        return name, f"{count} ошибок, средняя потеря {avg:.1f}%"
    return "middlegame", "нет данных анализа — взят миттельшпиль по умолчанию"


def _week_drills_portion(unnatural: int) -> int:
    """Дневная порция дрелей: min(20, max(5, ceil(unnatural/7)))."""
    portion = (max(0, int(unnatural)) + 6) // 7
    return min(20, max(5, portion))


def _build_week_tasks(
    minutes: int,
    unnatural: int,
    weak_opening: dict[str, Any] | None,
    focus_phase: str,
    focus_reason: str,
) -> list[dict[str, str]]:
    """Детерминированный список задач на неделю (ключи key/text/detail)."""
    portion = _week_drills_portion(unnatural)
    phase_ru = _PLAN_FOCUS_RU.get(focus_phase, focus_phase)
    tasks = [
        {
            "key": "daily-task",
            "text": "Решай задачу дня каждый день (7 из 7)",
            "detail": "задача дня ×7: ни дня без тактики",
        },
        {
            "key": "drills",
            "text": f"Реши {portion} дрелей в день",
            "detail": f"{unnatural} неестественных ошибок за неделю — по {portion} в день",
        },
    ]
    if weak_opening:
        name = str(weak_opening.get("opening") or "—")
        epg = weak_opening.get("errors_per_game")
        epg_txt = f"{float(epg):.2f}" if isinstance(epg, (int, float)) else "—"
        tasks.append(
            {
                "key": "opening",
                "text": f"Разобрать 2 партии в дебюте «{name}»",
                "detail": f"худший дебют: {epg_txt} ошибок на партию",
            }
        )
    else:
        tasks.append(
            {
                "key": "opening",
                "text": "Разобрать 2 партии в слабейшем дебюте",
                "detail": "нет данных по дебютам — возьми любые 2 проигранные",
            }
        )
    tasks.append(
        {
            "key": "phase",
            "text": f"Разобрать 3 партии с ошибками в {phase_ru}",
            "detail": f"фокус недели: {phase_ru} ({focus_reason})",
        }
    )
    tasks.append(
        {
            "key": "time",
            "text": f"Занимайся {minutes} мин в день ({minutes * 7} мин за неделю)",
            "detail": "время из твоей цели: минута в день лучше часа раз в неделю",
        }
    )
    return tasks


def _build_human_summary(
    focus_phase: str,
    phases: list[tuple[str, dict[str, Any]]],
    weak_opening: dict[str, Any] | None,
    humanity: dict[str, Any] | None,
    drills_today: int,
) -> str:
    """Человеческий итог недели шаблонами, без LLM (3–5 предложений)."""
    phase_ru = _PLAN_FOCUS_RU.get(focus_phase, focus_phase)
    parts: list[str] = []
    stats = dict(phases).get(focus_phase) if phases else None
    if stats:
        count = int(stats.get("count") or 0)
        avg = float(stats.get("avg_drop") or 0.0)
        parts.append(
            f"Главная дыра — {phase_ru}: {count} ошибок, средняя потеря {avg:.1f}%."
        )
    else:
        parts.append(f"На неделе качаем {phase_ru}: данных пока мало, собираем базу.")
    if weak_opening:
        name = str(weak_opening.get("opening") or "—")
        epg = weak_opening.get("errors_per_game")
        epg_txt = f"{float(epg):.2f}" if isinstance(epg, (int, float)) else "—"
        parts.append(f"Худший дебют — {name}: {epg_txt} ошибок на партию.")
    total = 0
    unnatural = 0
    if isinstance(humanity, dict):
        total = int(humanity.get("total") or 0)
        unnatural = int(humanity.get("unnatural") or 0)
    share = round(unnatural / total * 100, 1) if total else 0.0
    if share >= 40.0:
        parts.append(
            f"Много случайных ходов — {share}% неестественных из {total}: "
            "играй медленнее, считай до конца."
        )
    else:
        parts.append(
            f"Ошибки в основном естественные ({share}% неестественных из {total}) — "
            "работаем над счётом."
        )
    parts.append(
        f"Вывод на неделю: качаем {phase_ru} — {drills_today} дрелей в день "
        "плюс разбор партий из списка выше."
    )
    return " ".join(parts)


def _plan_week_payload(db: Database, nick: str, data_dir: Any) -> dict[str, Any]:
    """JSON для /api/plan/week: цель + детерминированный план из живых данных.

    Каждый источник обёрнут в try/except и деградирует в null/дефолт,
    а не в 404 — неделя строится из того, что есть.
    """
    goal = _load_plan_goal(db, nick)
    minutes = int(goal["minutes"])

    plan: dict[str, Any] | None = None
    try:
        plan = shared_data.plan(db, nick, root=data_dir)
    except RuntimeError:
        plan = None
    report: dict[str, Any] | None = None
    try:
        report = shared_data.report(db, nick)
    except RuntimeError:
        report = None
    _ = report  # тот же билдер, что /api/report: держим вызов ради живых данных

    phases = _week_phases(plan)
    focus_phase, focus_reason = _resolve_week_focus(goal, plan)

    weak_openings = []
    if isinstance(plan, dict):
        weak_openings = (plan.get("repertoire") or {}).get("weak_openings") or []
    weak_opening = weak_openings[0] if weak_openings else None

    drills_total = 0
    unnatural = 0
    humanity = None
    if isinstance(plan, dict):
        drills = plan.get("drills") or {}
        try:
            drills_total = int(drills.get("total") or 0)
        except (TypeError, ValueError):
            drills_total = 0
        try:
            unnatural = int(drills.get("unnatural") or 0)
        except (TypeError, ValueError):
            unnatural = 0
        humanity = plan.get("humanity")
    drills_today = _week_drills_portion(unnatural)

    try:
        task = _task_payload(db, nick)
        task_done: bool | None = bool(task.get("done"))
    except RuntimeError:
        task_done = None

    day = _today()
    now = _now()
    try:
        track_state = _track_state(db, _track_key(day, nick))
        manual_sec = _manual_seconds(db, _lichess_manual_key(day, nick))
        games = db.get_games(nick, limit=100_000)
        site_sec = 0.0
        if track_state is not None:
            site_sec = track_state["total"] + max(
                0.0, min(now - track_state["last"], 60.0)
            )
        lichess_sec = 0.0
        for game in games:
            if timeutil.date_of_ms(game.created_at) != day:
                continue
            lichess_sec += _clock_seconds(game.clocks)
        time_sec = int(site_sec + lichess_sec + manual_sec)
    except (ValueError, TypeError, OSError):
        time_sec = 0

    return {
        "goal": goal["goal"],
        "minutes_per_day": minutes,
        "focus": {"phase": focus_phase, "reason": focus_reason},
        "week_tasks": _build_week_tasks(
            minutes, unnatural, weak_opening, focus_phase, focus_reason
        ),
        "today": {
            "task_done": task_done,
            "time_sec": time_sec,
            "drills_today": drills_today,
            "drills_total": drills_total,
        },
        "human_summary": _build_human_summary(
            focus_phase, phases, weak_opening, humanity, drills_today
        ),
    }

# --- Индивидуальный LLM-план недели ---------------------------------------

_PLAN_LLM_SYSTEM = """Ты — шахматный тренер сильного любителя. Отвечай СРАЗУ готовым планом, без разбора шагов. Только русский язык.
Структура ответа строго такая:
"Неделя {n}: {заголовок}"
"Режим: обычный|предтурнирный"
"Норма дня ({M} мин):" — далее 3-5 строк формата "- {Блок} — {X} мин ({конкретика: N качественных задач / какие дебюты / сколько партий})" — сумма минут всех строк равна {M}.
"Итог недели: ..." (1-2 предложения).
Опирайся ТОЛЬКО на переданные данные: называй ИХ фазу и ИХ дебюты. Если до турнира 14 дней или меньше — предтурнирный режим: решаем задачи и повторяем дебюты, без нового материала. Никакого английского, никаких общих фраз вида "учи эндшпили".
Калибруй сложность под уровень: при рейтинге ниже 1400 допустима база; 1400–1800 — без детской базы; 1800 и выше — ТОЛЬКО продвинутый уровень: сложные реализации, техника, типовые планы из ЕГО дебютов; ЗАПРЕЩЕНЫ темы одиночного мата, оппозиции, "король+пешка против короля" и любая элементарная база. Примеры позиций и дебютов бери ТОЛЬКО из переданных данных, не выдумывай детские."""


def _plan_llm_key(nick: str) -> str:
    """Ключ meta с индивидуальным планом недели (JSON)."""
    return f"plan_llm_{nick}"


def _plan_unnatural_verdict(humanity: dict[str, Any] | None) -> str:
    """Вердикт человечности одной фразой (для входа LLM-плана)."""
    if not isinstance(humanity, dict):
        return "нет данных человечности"
    try:
        total = int(humanity.get("total") or 0)
        unnatural = int(humanity.get("unnatural") or 0)
    except (TypeError, ValueError):
        return "нет данных человечности"
    if total <= 0:
        return "нет данных человечности"
    share = round(unnatural / total * 100, 1)
    if share >= 40.0:
        return f"{share}% неестественных из {total} — много случайных ходов"
    return f"{share}% неестественных из {total} — ошибки в основном естественные"


_PLAN_SPEED_RU = {
    "bullet": "пуля",
    "blitz": "блиц",
    "rapid": "рапид",
    "classical": "классика",
    "correspondence": "переписка",
}


def _plan_player_level(db: Database, nick: str) -> dict[str, Any]:
    """Уровень игрока для калибровки плана: рейтинг и контроль времени.

    Приоритет источников: живой рейтинг Lichess (fetch_lichess_rating по
    личесс-нику пользователя) → последняя по created_at партия в БД (поля Game
    user_rating/speed) → рейтинг null (план тогда калибруется по глубине
    ошибок). Сетевые исключения наружу не выпускаются: любой сбой live-пути
    молча откатывается на БД. Партий нет, рейтинг 0/None/мусор или ошибка
    чтения — рейтинг null.
    """
    try:
        live = fetch_lichess_rating(_lichess_of(db, nick))
    except Exception:
        live = None
    if isinstance(live, dict):
        rating = live.get("rating")
        speed = live.get("speed")
        if (
            isinstance(rating, int)
            and not isinstance(rating, bool)
            and rating > 0
            and isinstance(speed, str)
            and speed.strip()
        ):
            return {"rating": rating, "speed": speed.strip()}
    try:
        games = db.get_games(nick, limit=100_000)
    except (ValueError, TypeError, OSError):
        return {"rating": None, "speed": None}
    if not games:
        return {"rating": None, "speed": None}
    last = max(games, key=lambda g: (g.created_at, g.id))
    rating = last.user_rating
    if isinstance(rating, bool) or not isinstance(rating, int) or rating <= 0:
        rating = None
    speed = last.speed
    if not isinstance(speed, str) or not speed.strip():
        speed = None
    else:
        speed = speed.strip()
    return {"rating": rating, "speed": speed}


def _plan_llm_input(db: Database, nick: str, data_dir: Any) -> dict[str, Any]:
    """Компактный вход LLM-плана: анкета + цифры из тех же источников,
    что /api/plan/week (слабая фаза, топ-3 дебюта, вердикт, дрели, счёт),
    плюс уровень игрока (живой рейтинг Lichess, fallback — последняя партия;
    входит в хэш input_hash, чтобы план пересоздавался при смене рейтинга)."""
    goal = _load_plan_goal(db, nick)
    minutes = int(goal["minutes"])
    try:
        plan = shared_data.plan(db, nick, root=data_dir)
    except RuntimeError:
        plan = None
    if not isinstance(plan, dict):
        plan = None

    focus_phase, focus_reason = _resolve_week_focus(goal, plan)
    weak_openings: list[dict[str, Any]] = []
    drills_total = 0
    drills_unnatural = 0
    humanity = None
    score_pct = None
    avg_win_loss = None
    if plan is not None:
        raw_weak = (plan.get("repertoire") or {}).get("weak_openings") or []
        for item in raw_weak[:3]:
            if not isinstance(item, dict):
                continue
            epg = item.get("errors_per_game")
            weak_openings.append(
                {
                    "opening": str(item.get("opening") or "—"),
                    "errors_per_game": round(float(epg), 2)
                    if isinstance(epg, (int, float))
                    else None,
                }
            )
        drills = plan.get("drills") or {}
        try:
            drills_total = int(drills.get("total") or 0)
        except (TypeError, ValueError):
            drills_total = 0
        try:
            drills_unnatural = int(drills.get("unnatural") or 0)
        except (TypeError, ValueError):
            drills_unnatural = 0
        humanity = plan.get("humanity")
        if isinstance(plan.get("score_pct"), (int, float)):
            score_pct = round(float(plan["score_pct"]), 1)
        if isinstance(plan.get("avg_win_loss"), (int, float)):
            avg_win_loss = round(float(plan["avg_win_loss"]), 2)

    level = _plan_player_level(db, nick)

    return {
        "goal_type": goal.get("goal_type") or _PLAN_GOAL_DEFAULT_TYPE,
        "goal": goal.get("goal"),
        "weeks_left": _plan_weeks_left(goal.get("tournament_date")),
        "target_rating": goal.get("target_rating"),
        "rating": level["rating"],
        "speed": level["speed"],
        "minutes": minutes,
        "focus": goal.get("focus") or "auto",
        "focus_phase": focus_phase,
        "focus_reason": focus_reason,
        "weak_openings": weak_openings,
        "unnatural_verdict": _plan_unnatural_verdict(humanity),
        "drills_total": drills_total,
        "drills_unnatural": drills_unnatural,
        "score_pct": score_pct,
        "avg_win_loss": avg_win_loss,
    }


def _plan_llm_messages(entry: dict[str, Any]) -> list[ChatMessage]:
    """User-промпт плана: факты построчно + требование только плана."""
    type_ru = _PLAN_GOAL_TYPE_RU.get(
        str(entry.get("goal_type")), str(entry.get("goal_type"))
    )
    weeks = entry.get("weeks_left")
    weeks_txt = f"{weeks} нед." if isinstance(weeks, int) else "без даты турнира"
    rating = entry.get("target_rating")
    rating_txt = str(rating) if isinstance(rating, int) else "—"
    weak = entry.get("weak_openings") or []
    if weak:
        weak_txt = "; ".join(
            f"{w.get('opening')} ({w.get('errors_per_game') if w.get('errors_per_game') is not None else '—'} ошибок/партию)"
            for w in weak
            if isinstance(w, dict)
        )
    else:
        weak_txt = "нет данных по дебютам"
    score = entry.get("score_pct")
    loss = entry.get("avg_win_loss")
    rating = entry.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or rating <= 0:
        rating = None
    if rating is not None:
        speed = entry.get("speed")
        speed_ru = _PLAN_SPEED_RU.get(speed, speed) if isinstance(speed, str) else None
        level_txt = f"{speed_ru} {rating}" if speed_ru else str(rating)
        level_line = f"Уровень: {level_txt}."
    else:
        loss_txt = loss if loss is not None else "—"
        level_line = (
            "Уровень: неизвестен, калибруй по глубине ошибок: "
            f"средняя потеря {loss_txt}%."
        )
    lines = [
        level_line,
        f"Тип цели: {type_ru}.",
        f"Цель своими словами: {entry.get('goal') or '—'}.",
        f"До турнира: {weeks_txt}; целевой рейтинг: {rating_txt}.",
        f"Минут в день: {entry.get('minutes')}.",
        f"Слабая фаза: {entry.get('focus_phase')} ({entry.get('focus_reason')}).",
        f"Топ слабых дебютов: {weak_txt}.",
        f"Человечность: {entry.get('unnatural_verdict')}.",
        f"Дрели: всего {entry.get('drills_total')}, неестественных {entry.get('drills_unnatural')}.",
        f"Счёт: {score if score is not None else '—'}%; средняя потеря: {loss if loss is not None else '—'}%.",
        "Ответ — только план по структуре выше.",
    ]
    return [
        ChatMessage("system", _PLAN_LLM_SYSTEM),
        ChatMessage("user", "\n".join(lines)),
    ]


# Леммы детсадовских тем: для игрока 1800+ их наличие в плане — брак.
_PLAN_BASIC_LEMMAS = (
    "оппозиц",
    "пешка против короля",
    "пешкой против короля",
    "пешек против короля",
    "король против короля",
    "мат одинок",
    "одиноким корол",
    "одинокого короля",
    "правило квадрата",
    "как ходят фигуры",
    "как ходит конь",
)


def _plan_has_basic_stuff(text: str) -> bool:
    """True, если в тексте плана есть детсадовские темы."""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(lemma in lowered for lemma in _PLAN_BASIC_LEMMAS)


def _plan_chat_ok(text: str, advanced: bool) -> bool:
    """Годен ли текст плана: не пуст, не разбор, без детсада для 1800+."""
    if not text or looks_like_thinking(text):
        return False
    if advanced and _plan_has_basic_stuff(text):
        return False
    return True


def _plan_llm_chat(messages: list[ChatMessage], advanced: bool = False) -> str:
    """Один вызов LLM для плана (с повтором при thinking-мусоре).

    thinking-блоки вырезаются, служебный разбор вместо плана — это ошибка
    (job уйдёт в error, мусор в meta не пишется).
    """
    llm = LLMClient()
    text = clean_llm_text(llm.chat(messages, temperature=0.4))
    if _plan_chat_ok(text, advanced):
        return text
    retry = [
        messages[0],
        ChatMessage(
            "user",
            messages[1].content + "\nБез разбора. Сразу готовый план по-русски.",
        ),
    ]
    second = clean_llm_text(llm.chat(retry, temperature=0.4))
    if _plan_chat_ok(second, advanced):
        return second
    if advanced and second and _plan_has_basic_stuff(second):
        raise RuntimeError("LLM вернул базовые темы вместо продвинутого плана")
    raise RuntimeError("LLM вернул служебный разбор вместо плана")


def _run_plan_llm_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: индивидуальный LLM-план недели → meta plan_llm_*."""
    try:
        messages = _plan_llm_messages(params["input"])
        if params["dry_run"]:
            job.result = {
                "dry_run": True,
                "input_hash": params["input_hash"],
                "prompts": [
                    {"role": m.role, "content": m.content} for m in messages
                ],
            }
            job.summary = "План недели: собраны промпты без вызова LLM"
        else:
            job.phase = "План недели"
            _ensure_llm_key()
            rating = params["input"].get("rating")
            advanced = (
                isinstance(rating, int)
                and not isinstance(rating, bool)
                and rating >= 1800
            )
            text = _plan_llm_chat(messages, advanced=advanced)
            if not text:
                raise RuntimeError("LLM вернул пустой план")
            created = _today()
            record = {
                "text": text,
                "created_at": created,
                "input_hash": params["input_hash"],
                "weeks_left": params["input"].get("weeks_left"),
            }
            with Database(app.state.db_path) as db:
                db.init_db()
                db.set_meta(
                    _plan_llm_key(job.user),
                    json.dumps(record, ensure_ascii=False),
                )
            job.result = {
                "plan": {
                    "text": text,
                    "created_at": created,
                    "weeks_left": record["weeks_left"],
                }
            }
            job.summary = "Индивидуальный план недели готов"
        job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()


def _load_plan_llm(db: Database, nick: str) -> dict[str, Any] | None:
    """Сохранённый LLM-план из meta; нет/битый/пустой текст → None."""
    raw = db.get_meta(_plan_llm_key(nick))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "text": text,
        "created_at": data.get("created_at"),
        "weeks_left": data.get("weeks_left"),
    }

# --- Мини-трекер времени ---------------------------------------------------

def _now() -> float:
    """Текущий unix-время (сек) — отдельная функция ради тестов (патчат)."""
    return time.time()


def _track_key(date: str, user: str) -> str:
    """Ключ meta с сессией на сайте за дату (на пользователя)."""
    return f"site_time_{date}_{user}"


def _track_state(db: Database, key: str) -> dict[str, float] | None:
    """Читает сессию трекера {"last", "total"}; нет записи/битый JSON → None."""
    raw = db.get_meta(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {"last": float(data["last"]), "total": float(data["total"])}
    except (TypeError, ValueError, KeyError):
        return None


def _lichess_manual_key(date: str, user: str) -> str:
    """Ключ meta с ручными минутами на Lichess за дату (на пользователя)."""
    return f"lichess_manual_{date}_{user}"


def _manual_seconds(db: Database, key: str) -> int:
    """Накопленные ручные секунды Lichess по ключу; нет записи/мусор → 0."""
    raw = db.get_meta(key)
    if not raw:
        return 0
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _parse_minutes(raw: Any) -> int:
    """Строковые минуты из формы → целое 1..600; иначе HTTP 400."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise HTTPException(400, "Минуты: целое число от 1 до 600")
    if not 1 <= value <= 600:
        raise HTTPException(400, "Минуты: целое число от 1 до 600")
    return value


def _clock_seconds(clocks: list[float]) -> float:
    """«Часы в партии»: сумма убываний остатков clocks (без инкрементов)."""
    return sum(max(0.0, prev - cur) for prev, cur in zip(clocks, clocks[1:]))


# Заметки к ходам: максимальная длина идеи хода и классы ошибок, для которых
# /api/games/{id}/causes считает причину (см. app.mistakes).
_MOVE_NOTE_MAX_LEN = 500
_MOVE_ERROR_CLASSES = frozenset({"blunder", "mistake"})


# --- API factory -----------------------------------------------------------

def create_api(
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Chess Trainer API", version="0.4.8", docs_url="/api/docs", redoc_url=None)
    app.state.db_path = Path(db_path) if db_path else settings.db_path
    app.state.data_dir = Path(data_dir) if data_dir else settings.data_dir

    # --- User / Auth ---
    @app.get("/api/user/current")
    def api_current_user(request: Request):
        user = _current_user(app.state.db_path, request)
        if user:
            with Database(app.state.db_path) as db:
                db.init_db()
                admin = is_admin(db, user["nick"])
            # Только публичные поля: хеш пароля и служебные колонки наружу не идут.
            return {
                "user": {k: user.get(k) for k in ("nick", "lichess", "fide_id")},
                "is_admin": admin,
            }
        raise HTTPException(status_code=404, detail="No current user")

    @app.get("/api/users")
    def api_list_users(request: Request):
        with Database(app.state.db_path) as db:
            db.init_db()
            users = db.list_users()
        return {"users": users}

    @app.post("/api/user/add")
    def api_user_add(request: Request, nick: str = Form(...), fide: str = Form("")):
        from .identity import validate_nick
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        if not validate_nick(clean):
            raise HTTPException(400, f"Невалидный ник: {clean}")
        db = Database(app.state.db_path)
        db.init_db()
        if not _session_nick(request, db):
            raise HTTPException(401, "Нужен вход")
        db.upsert_user(clean, fide_id=fide.strip() or None)
        db.set_current_user(clean)
        db.touch_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/switch")
    def api_user_switch(request: Request, nick: str = Form(...)):
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        db = Database(app.state.db_path)
        db.init_db()
        if not _session_nick(request, db):
            raise HTTPException(401, "Нужен вход")
        db.set_current_user(clean)
        db.touch_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/delete")
    def api_user_delete(request: Request, nick: str = Form(...)):
        """Удаляет профиль и его партии/анализ из БД (файлы data/<ник>/ остаются)."""
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        db = Database(app.state.db_path)
        db.init_db()
        if not _session_nick(request, db):
            raise HTTPException(401, "Нужен вход")
        db.delete_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/fide")
    def api_user_fide(nick: str = Form(...), fide_id: str = Form("")):
        """Задаёт (или пустым значением очищает) FIDE ID профиля."""
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        value = fide_id.strip()
        with Database(app.state.db_path) as db:
            db.init_db()
            db.set_user_fide(clean, value or None)
        return {"ok": True, "nick": clean, "fide_id": value or None}

    # --- Data endpoints ---
    @app.get("/api/overview")
    def api_overview(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: shared_data.overview(db, user["nick"], root=app.state.data_dir),
            app.state.db_path,
        )

    @app.get("/api/plan")
    def api_plan(request: Request, max_depth: int = 16):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: shared_data.plan(
                db, user["nick"], root=app.state.data_dir, max_depth=max_depth
            ),
            app.state.db_path,
        )

    @app.get("/api/progress")
    def api_progress(request: Request, windows: int = 5):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: shared_data.progress(
                db, user["nick"], root=app.state.data_dir, windows=windows
            ),
            app.state.db_path,
        )

    @app.get("/api/progress/daily")
    def api_progress_daily(request: Request, days: int = 30):
        """История активности по дням (плотная, пустые дни — нули)."""
        if not 1 <= days <= 3650:
            raise HTTPException(400, "days: целое число от 1 до 3650")
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            logs = db.list_daily_logs(user["nick"], days)
        return [
            {
                "date": item["date"],
                "tasks_done": item["tasks_done"],
                "tasks_total": item["tasks_total"],
                "minutes": item["minutes"],
            }
            for item in logs
        ]

    # --- План дня ---
    @app.get("/api/dayplan")
    def api_dayplan(request: Request, date: str = ""):
        day = _parse_day(date)
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            return _dayplan_payload(db, user["nick"], day, app.state.data_dir)

    @app.post("/api/dayplan/toggle")
    def api_dayplan_toggle(request: Request, body: DayPlanToggleBody):
        day = _parse_day(body.date)
        task_id = body.task_id.strip()
        if not task_id:
            raise HTTPException(400, "task_id не может быть пустым")
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            tasks = db.get_daily_plan(user["nick"], day)
            kind = next(
                (task.get("kind") for task in tasks if task.get("id") == task_id),
                None,
            )
            if not db.toggle_daily_task(user["nick"], day, task_id, body.done):
                raise HTTPException(404, "Задача дня не найдена")
            # Задача дня имеет второй счётчик в meta (/api/task/answer);
            # ручная отметка в плане должна совпадать с ним.
            if kind == "task":
                db.set_meta(
                    _task_done_key(day, user["nick"]),
                    "1" if body.done else "0",
                )
            return _dayplan_payload(
                db, user["nick"], day, app.state.data_dir
            )

    # --- Админка демо: когорта игроков ---
    @app.get("/api/admin/cohort")
    def api_admin_cohort(request: Request, days: int = 30):
        if not 1 <= days <= 3650:
            raise HTTPException(400, "days: целое число от 1 до 3650")
        _admin_user(request, app.state.db_path)
        with Database(app.state.db_path) as db:
            db.init_db()
            return _cohort_payload(db, days)

    @app.get("/api/admin/cohort.csv")
    def api_admin_cohort_csv(request: Request, days: int = 30):
        if not 1 <= days <= 3650:
            raise HTTPException(400, "days: целое число от 1 до 3650")
        _admin_user(request, app.state.db_path)
        with Database(app.state.db_path) as db:
            db.init_db()
            payload = _cohort_payload(db, days)
        return Response(
            _cohort_csv(payload["users"]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="cohort_{days}d.csv"'
                )
            },
        )

    def _drills_payload(
        user: dict,
        min_drop: float,
        min_win: float,
        verdict: str,
        limit: int,
    ) -> dict:
        verdicts = tuple(verdict.split(",")) if verdict else None
        return _call_data(
            lambda db: shared_data.drills(
                db,
                user["nick"],
                root=app.state.data_dir,
                min_drop=min_drop,
                min_win=min_win,
                verdicts=verdicts,
                limit=limit,
            ),
            app.state.db_path,
        )

    @app.get("/api/drills")
    def api_drills(
        request: Request,
        min_drop: float = 15.0,
        min_win: float = 50.0,
        verdict: str = "",
        limit: int = 0,
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        dr = _drills_payload(user, min_drop, min_win, verdict, limit)
        dr["items"] = [asdict(d) for d in dr["items"]]
        return dr

    @app.get("/api/drills.pgn")
    def api_drills_pgn(
        request: Request,
        min_drop: float = 15.0,
        min_win: float = 50.0,
        verdict: str = "",
        limit: int = 0,
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        dr = _drills_payload(user, min_drop, min_win, verdict, limit)
        pgn = drills_to_pgn(dr["items"])
        return StreamingResponse(
            iter([pgn]),
            media_type="application/x-chess-pgn",
            headers={"Content-Disposition": f'attachment; filename="drills_{user["nick"]}.pgn"'},
        )

    @app.get("/api/drills.json")
    def api_drills_json(
        request: Request,
        min_drop: float = 15.0,
        min_win: float = 50.0,
        verdict: str = "",
        limit: int = 0,
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        dr = _drills_payload(user, min_drop, min_win, verdict, limit)
        return Response(
            drills_to_json(dr["items"]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="drills_{user["nick"]}.json"'},
        )

    @app.get("/api/report")
    def api_report(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            rep = shared_data.report(db, user["nick"])
        if not rep:
            raise HTTPException(404, "No report")
        return rep

    @app.get("/api/plan/goal")
    def api_plan_goal_get(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            goal = _load_plan_goal(db, user["nick"])
        return {**goal, "weeks_left": _plan_weeks_left(goal.get("tournament_date"))}

    @app.post("/api/plan/goal")
    def api_plan_goal_set(
        request: Request,
        goal: str = Form(""),
        minutes: str = Form(""),
        focus: str = Form(""),
        goal_type: str = Form(""),
        tournament_date: str = Form(""),
        target_rating: str = Form(""),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        text = goal.strip()
        if not 1 <= len(text) <= 200:
            raise HTTPException(400, "Цель: от 1 до 200 символов")
        try:
            mins = int(str(minutes).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, "Минуты: целое число от 5 до 480") from None
        if not 5 <= mins <= 480:
            raise HTTPException(400, "Минуты: целое число от 5 до 480")
        focus_clean = focus.strip()
        if focus_clean not in _PLAN_GOAL_FOCUSES:
            raise HTTPException(
                400, f"Фокус: один из {', '.join(_PLAN_GOAL_FOCUSES)}"
            )
        type_clean = goal_type.strip() or _PLAN_GOAL_DEFAULT_TYPE
        if type_clean not in _PLAN_GOAL_TYPES:
            raise HTTPException(
                400, f"Тип цели: один из {', '.join(_PLAN_GOAL_TYPES)}"
            )
        date_clean = tournament_date.strip()
        if date_clean and _parse_plan_date(date_clean) is None:
            raise HTTPException(400, "Дата турнира: формат YYYY-MM-DD")
        rating_clean = target_rating.strip()
        rating_value: int | None = None
        if rating_clean:
            try:
                rating_value = int(rating_clean)
            except (TypeError, ValueError):
                raise HTTPException(
                    400, "Целевой рейтинг: целое число от 800 до 3200"
                ) from None
            if not 800 <= rating_value <= 3200:
                raise HTTPException(
                    400, "Целевой рейтинг: целое число от 800 до 3200"
                )
        saved = {
            "goal": text,
            "minutes": mins,
            "focus": focus_clean,
            "goal_type": type_clean,
            "tournament_date": date_clean or None,
            "target_rating": rating_value,
        }
        with Database(app.state.db_path) as db:
            db.set_meta(
                _plan_goal_key(user["nick"]),
                json.dumps(saved, ensure_ascii=False),
            )
        return {"ok": True, **saved}

    @app.get("/api/plan/week")
    def api_plan_week(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            return _plan_week_payload(db, user["nick"], app.state.data_dir)

    @app.get("/api/plan/llm")
    def api_plan_llm_get(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            saved = _load_plan_llm(db, user["nick"])
        if saved is None:
            return {"text": None}
        return saved

    @app.post("/api/plan/generate")
    def api_plan_generate(
        request: Request,
        dry_run: str | None = Form(None),
        regenerate: str | None = Form(None),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            entry = _plan_llm_input(db, user["nick"], app.state.data_dir)
            raw = db.get_meta(_plan_llm_key(user["nick"]))
        digest = hashlib.sha256(
            json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        saved = None
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    saved = data
            except (TypeError, ValueError):
                saved = None
        if (
            saved
            and saved.get("input_hash") == digest
            and regenerate != "on"
            and isinstance(saved.get("text"), str)
            and saved["text"].strip()
        ):
            return {
                "cached": True,
                "plan": {
                    "text": saved["text"],
                    "created_at": saved.get("created_at"),
                    "weeks_left": saved.get("weeks_left"),
                },
            }
        _check_job_limits(user["nick"])
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
            phase="План недели",
        )
        _register_job(job)
        params = {
            "input": entry,
            "input_hash": digest,
            "dry_run": dry_run == "on",
        }
        threading.Thread(
            target=_run_plan_llm_job, args=(app, job, params), daemon=True
        ).start()
        return {"cached": False, "job_id": job.id, "status": "started"}

    # --- Coach / Analysis jobs ---
    @app.post("/api/coach/run")
    def api_coach_run(
        request: Request,
        max: int = Form(50),
        perf: str = Form("rapid"),
        depth: int = Form(14),
        multipv: int = Form(3),
        profile: str = Form(""),
        cached_only: str | None = Form(None),
        refresh: str | None = Form(None),
        since: int | None = Form(None),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        profile = (profile or "").strip().lower()
        if profile:
            preset = _COACH_PROFILES.get(profile)
            if preset is None:
                raise HTTPException(
                    400,
                    "profile должен быть quick, standard или deep "
                    f"(получено {profile!r})",
                )
            depth, multipv = preset
        if not 1 <= max <= 200:
            raise HTTPException(400, "max должен быть от 1 до 200")
        if not 1 <= depth <= 30:
            raise HTTPException(400, "depth должен быть от 1 до 30")
        if not 1 <= multipv <= 10:
            raise HTTPException(400, "multipv должен быть от 1 до 10")
        if not _engine_ready():
            raise HTTPException(
                503,
                f"Stockfish не найден по пути {settings.stockfish_path!r}. "
                "Задай STOCKFISH_PATH (например, в .env или переменной окружении).",
            )
        _check_job_limits(user["nick"])

        job = CoachJob(id=uuid.uuid4().hex, user=user["nick"])
        _register_job(job)

        params = {
            "max": max,
            "perf": perf,
            "depth": depth,
            "multipv": multipv,
            "cached_only": cached_only == "on",
            "refresh": refresh == "on",
            "since": since,
        }

        t = threading.Thread(target=_run_coach_job, args=(app, job, params), daemon=True)
        t.start()

        return {"job_id": job.id, "status": "started"}

    @app.get("/api/jobs/active")
    def api_active_job(request: Request):
        """Активная задача текущего пользователя (для блокировки кнопки)."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with _JOBS_LOCK:
            for job in _JOBS.values():
                if job.status == "running" and job.user == user["nick"]:
                    return job.as_dict()
        raise HTTPException(404, "No active job")

    @app.get("/api/jobs/{job_id}")
    def api_job_status(job_id: str, request: Request):
        user = _current_user(app.state.db_path, request)
        job = _get_job(job_id)
        if job is not None:
            # Чужую задачу (результаты LLM, данные соперника) не отдаём:
            # с сессией — только свои job'ы; без сессии — meta-fallback как раньше.
            if user is not None and job.user != user["nick"]:
                raise HTTPException(404, "Job not found")
            return job.as_dict()

        # Job'а нет в памяти: возможно, процесс перезапустился. Пробуем снапшот из meta.
        meta = _load_job_meta(app.state.db_path, job_id)
        if meta is None:
            raise HTTPException(404, "Job not found")
        if user is not None and meta.get("user") != user["nick"]:
            raise HTTPException(404, "Job not found")
        if meta.get("status") == "running":
            # Снапшот «running», но процесса-владельца в памяти уже нет — значит
            # сервер упал/перезапустился посреди анализа. Отдаём копию как error.
            stale = dict(meta)
            stale["status"] = "error"
            stale["error"] = "Анализ прерван: сервер перезапустился"
            return stale
        return meta

    @app.post("/api/jobs/{job_id}/cancel")
    def api_job_cancel(job_id: str, request: Request):
        """Отмена фоновой задачи: только владелец, идемпотентно.

        Помечает job как ``cancelled``; рабочий поток проверяет статус перед
        каждой партией и выходит из цикла. Уже завершённая задача возвращает
        текущий статус без ошибки.
        """
        user = _current_user(app.state.db_path, request)
        job = _get_job(job_id)
        if job is None or user is None or job.user != user["nick"]:
            raise HTTPException(404, "Job not found")
        with _JOBS_LOCK:
            was_running = job.status == "running"
            if was_running:
                job.status = "cancelled"
                job.finished = time.time()
                if not job.summary:
                    job.summary = "Анализ отменён"
            current = job.status
        if was_running:
            _persist_job(app.state.db_path, job)
            return {"ok": True}
        return {"ok": True, "status": current}

    # --- Games (просмотр партий) ---
    def _game_summary(g) -> dict:
        return {
            "id": g.id,
            "link": g.link,
            "created_at": g.created_at,
            "opponent": g.opponent,
            "user_color": g.user_color,
            "result_for_user": g.result_for_user,
            "opening": g.opening,
            "eco": g.eco,
            "speed": g.speed,
            "rated": g.rated,
            "moves": len(g.moves),
        }

    def _games_payload(db, user: str, limit: int) -> dict:
        games = db.get_games(user, limit)
        ids = [g.id for g in games]
        analyzed = set(ids) - set(db.get_unanalyzed(ids))
        items = []
        for g in games:
            item = _game_summary(g)
            item["analyzed"] = g.id in analyzed
            items.append(item)
        return {"games": items}

    def _game_dict(g) -> dict:
        return {
            "id": g.id,
            "link": g.link,
            "rated": g.rated,
            "speed": g.speed,
            "created_at": g.created_at,
            "status": g.status,
            "winner": g.winner,
            "white": g.white,
            "black": g.black,
            "opening": g.opening,
            "eco": g.eco,
            "moves": g.moves,
            "user_color": g.user_color,
            "opponent": g.opponent,
            "user_rating": g.user_rating,
            "user_rating_diff": g.user_rating_diff,
            "result_for_user": g.result_for_user,
        }

    def _fens_for(moves: list[str]) -> list[str]:
        """FEN после каждого хода; на невалидном ходе останавливается."""
        board = chess.Board()
        fens = [board.fen()]
        for san in moves:
            try:
                board.push_san(san)
            except ValueError:
                break
            fens.append(board.fen())
        return fens

    def _game_detail_payload(db, user: str, game_id: str) -> dict:
        game = db.get_game(game_id, user)
        if not game:
            raise RuntimeError("Game not found")
        return {
            "game": _game_dict(game),
            "analysis": db.get_analysis(game_id),
            "fens": _fens_for(game.moves),
        }

    def _causes_payload(game, analysis: dict) -> dict[str, dict]:
        """Причины ошибок по полуходам: {"<ply>": {cause, label, explanation}}.

        Доска восстанавливается по ``game.moves``; фаза берётся по позиции
        ПЕРЕД ходом (``phase(ply, board)``), как в app.patterns. ``repeated``
        пока всегда False — повторы считает отдельный эндпоинт /repeats.
        Невалидный ход обрывает реплей: дальше позицию не восстановить.
        """
        moves = analysis.get("moves") or []
        board = chess.Board()
        causes: dict[str, dict] = {}
        for ply, san in enumerate(game.moves):
            entry = moves[ply] if ply < len(moves) else {}
            if not isinstance(entry, dict):
                entry = {}
            if entry.get("classification") in _MOVE_ERROR_CLASSES:
                current_phase = phase(ply, board)
                cause = classify_cause(entry, current_phase, repeated=False)
                causes[str(ply)] = {
                    "cause": cause,
                    "label": cause_label(cause),
                    "explanation": explain_cause(
                        cause, entry, repeated=False, phase=current_phase
                    ),
                }
            try:
                board.push_san(san)
            except ValueError:
                break
        return causes

    def _notes_payload(db, user: str, game_id: str) -> dict[str, str]:
        """Заметки игрока к ходам: {"<ply>": intent}."""
        return {str(ply): text for ply, text in db.get_move_notes(game_id, user).items()}

    # --- Шахматные утилиты: свободный разбор позиции (stateless) ------------

    @app.post("/api/chess/legal")
    def api_chess_legal(body: ChessPositionBody, request: Request):
        """Легальные ходы позиции: UCI-координаты + SAN (для доски разбора)."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        board = _fen_board(body.fen)
        return {
            "moves": [
                {
                    "from": chess.square_name(move.from_square),
                    "to": chess.square_name(move.to_square),
                    "promotion": (
                        chess.piece_symbol(move.promotion) if move.promotion else None
                    ),
                    "san": board.san(move),
                }
                for move in board.legal_moves
            ]
        }

    @app.post("/api/chess/move")
    def api_chess_move(body: ChessMoveBody, request: Request):
        """Применяет ход к FEN и возвращает новую позицию без сохранения."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        board = _fen_board(body.fen)
        move = _chess_move_from_body(board, body)
        san = board.san(move)
        board.push(move)
        return {
            "fen": board.fen(),
            "san": san,
            "check": board.is_check(),
            "game_over": board.is_game_over(),
        }

    @app.post("/api/chess/eval")
    def api_chess_eval(body: ChessEvalBody, request: Request):
        """Быстрая оценка позиции Stockfish (win% — для белых)."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        board = _fen_board(body.fen)
        depth = max(CHESS_EVAL_DEPTH_MIN, min(CHESS_EVAL_DEPTH_MAX, body.depth))
        try:
            with Engine(depth=depth, multipv=1, time_ms=CHESS_EVAL_TIME_MS) as engine:
                lines = engine.analyze_position(board)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, "Stockfish недоступен") from exc
        if not lines:
            raise HTTPException(503, "Stockfish не вернул оценку")
        top = lines[0]
        eval_white = top.get("eval") or {}
        best_line = [str(move) for move in (top.get("pv") or [])]
        try:
            win_value = float(top["win"]) if top.get("win") is not None else None
        except (TypeError, ValueError):
            win_value = None
        if win_value is None:
            win_value = win_percent(eval_white.get("cp"), eval_white.get("mate"))
        return {
            "depth": depth,
            "cp": eval_white.get("cp"),
            "mate": eval_white.get("mate"),
            "win": win_value,
            "best_san": best_line[0] if best_line else None,
            "best_line": best_line,
        }

    # --- Теория дебютов -----------------------------------------------------

    @app.get("/api/theory")
    def api_theory(request: Request, fen: str = "", plies: int = 0, moves: str = ""):
        """Главная линия позиции по выбранному провайдеру теории.

        Провайдер недоступен → 200 с line от ECO-fallback или ``source="none"``:
        экран дебюта не должен падать из-за внешнего сервиса.
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        _fen_board(fen)
        depth = plies if plies > 0 else settings.theory_max_plies
        try:
            history = [part.strip() for part in moves.split(",") if part.strip()]
            return theory.main_line(
                fen, depth, db_path=app.state.db_path, moves=history or None
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/theory/ideas")
    def api_theory_ideas(body: TheoryIdeasBody, request: Request):
        """LLM-объяснение идеи линии: факты Explorer/ECO, без выдумок.

        Если идеи выключены или LLM недоступен — ``ideas: null`` и 200.
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        _fen_board(body.fen)
        if not body.line:
            return {"ideas": None, "cached": False}
        try:
            text, cached = theory.ideas_with_cache(
                body.fen, body.line, db_path=app.state.db_path
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ideas": text, "cached": cached}

    @app.get("/api/games")
    def api_games(request: Request, limit: int = 100):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _games_payload(db, user["nick"], limit),
            app.state.db_path,
        )

    @app.get("/api/games/{game_id}")
    def api_game_detail(game_id: str, request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _game_detail_payload(db, user["nick"], game_id),
            app.state.db_path,
        )

    @app.post("/api/games/import")
    async def api_games_import(
        request: Request,
        pgn: str = Form(""),
        file: UploadFile | None = File(None),
        user_color: str = Form(""),
        analyze: str | None = Form(None),
    ):
        """Импорт одной или нескольких партий из PGN (текст или файл).

        Цвет пользователя можно задать явно; иначе он определяется по нику
        из заголовков White/Black. ``analyze=on`` сразу считает Stockfish
        (depth 10) для импортированных партий.
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        text = (pgn or "").strip()
        if file is not None:
            raw = await file.read()
            if len(raw) > MAX_PGN_BYTES:
                raise HTTPException(400, "PGN больше 2 МБ")
            text = raw.decode("utf-8", errors="replace")
        if not text:
            raise HTTPException(400, "Пустой PGN")
        if len(text.encode("utf-8", errors="replace")) > MAX_PGN_BYTES:
            raise HTTPException(400, "PGN больше 2 МБ")
        try:
            games = parse_pgn_games(text, user["nick"], user_color=user_color or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not games:
            raise HTTPException(400, "Не удалось разобрать PGN")
        with Database(app.state.db_path) as db:
            db.init_db()
            db.save_games(games, user["nick"])
        analyzed = 0
        if (analyze or "").strip().lower() in ("on", "1", "true", "yes"):
            # Сразу анализируем только небольшие импорты: иначе это уже
            # фоновая задача, а не быстрый ответ на форму.
            for game in games[:3]:
                try:
                    with Engine(depth=10, multipv=1, time_ms=800) as engine:
                        summary = engine.analyze_game(game).summary()
                    with Database(app.state.db_path) as db:
                        db.init_db()
                        db.save_analysis(game.id, summary, depth=10)
                    analyzed += 1
                except Exception:  # noqa: BLE001 - импорт важнее анализа
                    logger.warning("Импорт: не удалось проанализировать %s", game.id)
        return {
            "imported": len(games),
            "analyzed": analyzed,
            "ids": [game.id for game in games],
        }

    @app.post("/api/games/{game_id}/analyze")
    def api_game_analyze(game_id: str, request: Request, refresh: bool = False):
        """Быстрый анализ одной партии по кнопке (depth 10, без фоновой задачи)."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            if not game.moves:
                raise HTTPException(400, "В партии нет ходов")
            existing = db.get_analysis(game_id)
            if isinstance(existing, dict) and not refresh:
                return {"analysis": existing, "cached": True}
        if _running_jobs_count() > 0:
            raise HTTPException(409, "Идёт фоновый анализ — дождись его окончания")
        try:
            with Engine(depth=10, multipv=1, time_ms=800) as engine:
                summary = engine.analyze_game(game).summary()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Не удалось проанализировать партию: {exc}") from exc
        with Database(app.state.db_path) as db:
            db.init_db()
            db.save_analysis(game_id, summary, depth=10)
        return {"analysis": summary, "cached": False}

    @app.get("/api/games/{game_id}/repeats")
    def api_game_repeats(game_id: str, request: Request):
        """Повторы ошибок: тот же полуход+SAN уже был в прошлых партиях."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            analysis = db.get_analysis(game_id)
            if not isinstance(analysis, dict):
                return {"repeats": []}
            history = db.get_analyzed_games(user["nick"], limit=500)
        return {"repeats": repeated_mistakes(game, analysis, history)}

    def _review_payload(game, analysis: dict) -> dict:
        """Полный разбор партии: бейджи, точность, фазы, категории, комментарии."""
        moves_san = list(game.moves or [])
        records = analysis.get("moves") or []
        user_is_white = game.user_color == "white"

        def _norm(san: str) -> str:
            return str(san or "").strip().rstrip("+#")

        def _avg_drop(for_user: bool) -> float:
            values: list[float] = []
            for ply, entry in enumerate(records):
                if not isinstance(entry, dict):
                    continue
                is_user = (ply % 2 == 0) == user_is_white
                if is_user != for_user:
                    continue
                raw = entry.get("drop")
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                values.append(float(raw))
            return sum(values) / len(values) if values else 0.0

        board = chess.Board()
        items: list[dict] = []
        for ply, san in enumerate(moves_san):
            entry = records[ply] if ply < len(records) and isinstance(records[ply], dict) else {}
            theory = {_norm(t_san) for t_san, _ in theory_moves(moves_san[:ply])}
            in_book = bool(theory) and _norm(san) in theory
            played_line = entry.get("played_line") or []
            try:
                sacrifice = detect_sacrifice(board, san, played_line)
            except Exception:  # noqa: BLE001
                sacrifice = False
            current_phase = phase(ply, board)
            phase_key = "middlegame" if current_phase in ("midgame", "middlegame") else current_phase
            cause = None
            if entry.get("classification") in ("blunder", "mistake"):
                cause = classify_cause(entry, phase_key, repeated=False)
            badge = badge_for(entry, in_book=in_book, sacrifice=sacrifice)
            items.append(
                {
                    "ply": ply,
                    "san": san,
                    "color": "w" if ply % 2 == 0 else "b",
                    "is_user": (ply % 2 == 0) == user_is_white,
                    "badge": badge,
                    "label": badge_label(badge),
                    "comment": move_comment(
                        entry,
                        badge,
                        best_move_san=entry.get("best_move_san"),
                        phase=phase_key,
                        cause=cause,
                    ),
                    "drop": entry.get("drop"),
                    "win_before": entry.get("win_before"),
                    "win_after": entry.get("win_after"),
                    "best_move_san": entry.get("best_move_san"),
                }
            )
            try:
                board.push_san(san)
            except ValueError:
                break

        phases = phase_stats(moves_san, analysis, game.user_color)
        user_acc = acc_accuracy(_avg_drop(for_user=True))
        opponent_acc = acc_accuracy(_avg_drop(for_user=False))
        return {
            "accuracy": {"user": user_acc, "opponent": opponent_acc},
            "rating_estimate": estimate_rating(user_acc, game.user_rating),
            "phases": phases,
            "categories": category_stats(phases),
            "summary": game_summary(
                phases,
                user_acc,
                user_color=game.user_color,
                result=game.result_for_user,
            ),
            "moves": items,
        }

    @app.get("/api/games/{game_id}/review")
    def api_game_review(game_id: str, request: Request):
        """Разбор партии в стиле chess.com: бейджи, точность, фазы, комментарии."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            analysis = db.get_analysis(game_id)
        if not isinstance(analysis, dict):
            return {"review": None}
        return {"review": _review_payload(game, analysis)}

    @app.get("/api/games/{game_id}/causes")
    def api_game_causes(game_id: str, request: Request):
        """Причины ошибок партии: только ходы с classification blunder/mistake."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            analysis = db.get_analysis(game_id)
        if not isinstance(analysis, dict):
            return {"causes": {}}
        return {"causes": _causes_payload(game, analysis)}

    @app.get("/api/games/{game_id}/notes")
    def api_game_notes(game_id: str, request: Request):
        """Заметки (идеи ходов) текущего игрока по партии."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            notes = _notes_payload(db, user["nick"], game_id)
        return {"notes": notes}

    @app.post("/api/games/{game_id}/intent-review")
    def api_game_intent_review(game_id: str, request: Request):
        """LLM-разбор замыслов игрока к ошибочным ходам (по заметкам)."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            analysis = db.get_analysis(game_id)
            if not isinstance(analysis, dict):
                raise HTTPException(400, "Сначала проанализируй партию")
            notes = db.get_move_notes(game_id, user["nick"])
            if not notes:
                raise HTTPException(400, "Сначала сохрани хотя бы одну идею хода")
            key = "intent_review:" + user["nick"] + ":" + intent_cache_key(
                game_id, notes, analysis
            )
            cached = db.get_meta(key)
        if cached:
            return {"review": cached, "cached": True}
        try:
            text = review_intents(game, analysis, notes)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"LLM недоступна: {exc}") from exc
        with Database(app.state.db_path) as db:
            db.init_db()
            db.set_meta(key, text)
        return {"review": text, "cached": False}

    @app.post("/api/games/{game_id}/notes")
    def api_game_note_save(game_id: str, body: MoveNoteBody, request: Request):
        """Сохраняет идею хода; пустой intent удаляет заметку."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        if body.ply < 0:
            raise HTTPException(400, "ply должен быть >= 0")
        intent = (body.intent or "").strip()
        if len(intent) > _MOVE_NOTE_MAX_LEN:
            raise HTTPException(
                400, f"intent не должен быть длиннее {_MOVE_NOTE_MAX_LEN} символов"
            )
        with Database(app.state.db_path) as db:
            db.init_db()
            game = db.get_game(game_id, user["nick"])
            if game is None:
                raise HTTPException(404, "Партия не найдена")
            db.save_move_note(game_id, user["nick"], body.ply, intent)
        return {"ok": True}

    # --- Tools: репертуар / соперник / человечность / турнир / FIDE ---
    def _repertoire_payload(db, user: str, color: str, max_depth: int) -> dict:
        pairs = shared_data.repertoire_pairs(db, user)
        rep = Repertoire()
        for game, analysis in pairs:
            rep.add_game(game, analysis, max_depth=max_depth)
        colors = ("white", "black") if color == "both" else (color,)
        lines = []
        weak: dict[str, int] = {}
        for c in colors:
            found = sorted(rep.lines(c, min_count=1), key=lambda line: -line.count)[:60]
            lines.extend({"color": c, **shared_data.line_to_dict(line)} for line in found)
            weak[c] = len(rep.weak_lines(c, min_count=1))
        return {
            "color": color,
            "games": len(pairs),
            "max_depth": max_depth,
            "lines": lines,
            "weak": weak,
            "openings": opening_stats(pairs, user=user),
        }

    @app.get("/api/repertoire")
    def api_repertoire(request: Request, color: str = "both", max_depth: int = 16):
        if color not in ("white", "black", "both"):
            raise HTTPException(400, "color должен быть white, black или both")
        if not 1 <= max_depth <= 64:
            raise HTTPException(400, "max_depth должен быть от 1 до 64")
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _repertoire_payload(db, user["nick"], color, max_depth),
            app.state.db_path,
        )

    @app.post("/api/prepare/run")
    def api_prepare_run(
        request: Request,
        opponent: str = Form(""),
        max: int = Form(30),
        perf: str = Form("rapid"),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        opp = opponent.strip()
        if not opp:
            raise HTTPException(400, "Укажи ник соперника")
        if not 1 <= max <= 200:
            raise HTTPException(400, "max должен быть от 1 до 200")
        _check_job_limits(user["nick"])
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
            phase="Загрузка партий соперника",
        )
        _register_job(job)
        params = {"opponent": opp, "max": max, "perf": perf}
        threading.Thread(target=_run_prepare_job, args=(app, job, params), daemon=True).start()
        return {"job_id": job.id, "status": "started"}

    @app.get("/api/humanize")
    def api_humanize(request: Request, refresh: str = ""):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        if refresh in ("1", "true", "on", "yes"):
            job = CoachJob(
                id=uuid.uuid4().hex,
                user=user["nick"],
                phase="Оценка человечности",
            )
            _register_job(job)
            threading.Thread(
                target=_run_humanize_job, args=(app, job, {}), daemon=True
            ).start()
            return {"job_id": job.id, "status": "started"}
        path = shared_data.humanity_path(user["nick"], root=app.state.data_dir)
        raw = shared_data.load_humanity_raw(path)
        if not raw:
            raise HTTPException(404, "Нет данных humanize — нажми «Посчитать»")
        return _humanity_payload(raw)

    @app.post("/api/tournament/run")
    def api_tournament_run(
        request: Request,
        games: str = Form(""),
        initial: str = Form(""),
    ):
        parsed: list[TournamentGame] = []
        for spec in (line.strip() for line in games.splitlines()):
            if not spec:
                continue
            parts = [part.strip() for part in spec.split(":")]
            if len(parts) < 3 or not parts[0]:
                raise HTTPException(
                    400,
                    f"Неверный формат партии: {spec!r} "
                    "(ожидается ОППОНЕНТ:ЦВЕТ:РЕЗУЛЬТАТ[:РЕЙТИНГ_СОПЕРНИКА])",
                )
            opponent, color, result = parts[0], parts[1], parts[2]
            opponent_rating = None
            if len(parts) > 3 and parts[3]:
                try:
                    opponent_rating = int(parts[3])
                except ValueError:
                    raise HTTPException(400, f"Рейтинг соперника не число: {parts[3]!r}")
            if color not in ("white", "black"):
                raise HTTPException(400, f"Цвет должен быть white или black, получено: {color!r}")
            if result not in ("win", "draw", "loss"):
                raise HTTPException(400, f"Результат должен быть win/draw/loss, получено: {result!r}")
            parsed.append(
                TournamentGame(
                    opponent=opponent,
                    color=color,
                    result=result,
                    opponent_rating=opponent_rating,
                )
            )
        if not parsed:
            raise HTTPException(
                400,
                "Нет партий — построчно «Имя:white:win:2100», по одной партии на строку.",
            )
        initial_rating = None
        if initial.strip():
            try:
                initial_rating = int(initial.strip())
            except ValueError:
                raise HTTPException(400, f"Инициал рейтинга — число: {initial!r}")
        return build_tournament_report(parsed, initial_rating=initial_rating)

    @app.post("/api/fide/run")
    def api_fide_run(request: Request, id: str = Form(""), windows: int = Form(4)):
        fide_id = id.strip()
        if not fide_id:
            raise HTTPException(400, "Укажи FIDE ID")
        if not 1 <= windows <= 52:
            raise HTTPException(400, "windows должен быть от 1 до 52")
        user = _current_user(app.state.db_path, request)
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"] if user else "",
            phase="Загрузка профиля FIDE",
        )
        _register_job(job)
        params = {"id": fide_id, "windows": windows}
        threading.Thread(target=_run_fide_job, args=(app, job, params), daemon=True).start()
        return {"job_id": job.id, "status": "started"}

    @app.post("/api/review/run")
    def api_review_run(
        request: Request,
        max: int = Form(3),
        game_id: str = Form(""),
        moments: int = Form(6),
        dry_run: str | None = Form(None),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        if not 1 <= max <= 50:
            raise HTTPException(400, "max должен быть от 1 до 50")
        if not 1 <= moments <= 20:
            raise HTTPException(400, "moments должен быть от 1 до 20")
        gid = game_id.strip()
        try:
            with Database(app.state.db_path) as db:
                db.init_db()
                _select_analyzed_pairs(db, user["nick"], gid, max if gid else 1)
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc
        _check_job_limits(user["nick"])

        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
            phase="Разбор партий",
        )
        _register_job(job)
        params = {
            "max": max,
            "game_id": gid or None,
            "moments": moments,
            "dry_run": dry_run == "on",
        }
        threading.Thread(
            target=_run_review_job, args=(app, job, params), daemon=True
        ).start()
        return {"job_id": job.id, "status": "started"}

    @app.post("/api/mentor/run")
    def api_mentor_run(
        request: Request,
        notes: str = Form(""),
        game_id: str = Form(""),
        max_depth: int = Form(16),
        dry_run: str | None = Form(None),
        no_progress: str | None = Form(None),
    ):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        if not 1 <= max_depth <= 64:
            raise HTTPException(400, "max_depth должен быть от 1 до 64")
        gid = game_id.strip()
        try:
            with Database(app.state.db_path) as db:
                db.init_db()
                _select_analyzed_pairs(db, user["nick"], gid, 1 if gid else None)
        except RuntimeError as exc:
            raise HTTPException(404, str(exc)) from exc
        _check_job_limits(user["nick"])

        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
            phase="Сборка плана",
        )
        _register_job(job)
        params = {
            "notes": notes,
            "game_id": gid or None,
            "max_depth": max_depth,
            "dry_run": dry_run == "on",
            "no_progress": no_progress == "on",
        }
        threading.Thread(
            target=_run_mentor_job, args=(app, job, params), daemon=True
        ).start()
        return {"job_id": job.id, "status": "started"}

    # --- Задача дня ---
    @app.get("/api/task/today")
    def api_task_today(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _task_payload(db, user["nick"]), app.state.db_path
        )

    @app.post("/api/task/answer")
    def api_task_answer(request: Request, answer: str = Form(...)):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")

        def _check(db: Database) -> dict[str, Any]:
            payload = _task_payload(db, user["nick"])
            board = chess.Board(payload["fen"])
            try:
                given = board.parse_san(answer.strip())
            except ValueError as exc:
                raise HTTPException(400, "нет такого хода") from exc
            try:
                best = board.parse_san(payload["best_san"])
            except ValueError:
                best = None
            correct = best is not None and given == best
            db.set_meta(_task_done_key(payload["date"], user["nick"]), "1")
            return {
                "correct": correct,
                "expected": payload["best_san"],
                "drop": payload["drop"],
                "win_before": payload["win_before"],
                "win_after": payload["win_after"],
                "san_played": payload["san_played"],
            }

        return _call_data(_check, app.state.db_path)

    @app.post("/api/task/explain")
    def api_task_explain(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        payload = _call_data(
            lambda db: _task_payload(db, user["nick"]), app.state.db_path
        )
        cached = payload.get("explanation")
        if cached and not _is_bad_explanation(cached):
            return {"cached": True, "explanation": cached}
        _check_job_limits(user["nick"])
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
            phase="Объяснение задачи",
        )
        _register_job(job)
        threading.Thread(
            target=_run_task_explain_job,
            args=(app, job, {"payload": payload}),
            daemon=True,
        ).start()
        return {"cached": False, "job_id": job.id, "status": "started"}

    @app.get("/api/task/legal")
    def api_task_legal(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")

        def _legal(db: Database) -> dict[str, Any]:
            payload = _task_payload(db, user["nick"])
            board = chess.Board(payload["fen"])
            moves = [
                {
                    "from": chess.square_name(m.from_square),
                    "to": chess.square_name(m.to_square),
                    "san": board.san(m),
                }
                for m in board.legal_moves
            ]
            return {
                "task_key": payload["task_key"],
                "fen": payload["fen"],
                "moves": moves,
            }

        return _call_data(_legal, app.state.db_path)

    # --- Мини-трекер времени ---
    @app.post("/api/track/ping")
    def api_track_ping(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        now = _now()
        day = _today()
        nick = user["nick"]
        key = _track_key(day, nick)
        with Database(app.state.db_path) as db:
            state = _track_state(db, key)
            if state is None:
                total = 0.0
            else:
                gap = now - state["last"]
                total = state["total"] + gap if 0.0 <= gap <= 300.0 else state["total"]
            db.set_meta(key, json.dumps({"last": now, "total": total}))
            # Дневной лог: минуты сайта (total/60) и активность по партиям/анализам.
            activity = db.daily_activity_counts(nick, day)
            db.upsert_daily_log(
                nick,
                day,
                minutes=int(round(total / 60.0)),
                games_analyzed=activity["games_analyzed"],
                analyses=activity["analyses"],
            )
        _maybe_snapshot_rating(db, nick, day)
        return {"ok": True, "total": int(total)}

    @app.post("/api/track/lichess_manual")
    def api_track_lichess_manual(request: Request, minutes: str = Form("")):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        value = _parse_minutes(minutes)
        day = _today()
        key = _lichess_manual_key(day, user["nick"])
        with Database(app.state.db_path) as db:
            total = _manual_seconds(db, key) + value * 60
            db.set_meta(key, str(total))
        return {"manual_sec": total, "date": day}

    @app.get("/api/track/today")
    def api_track_today(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        day = _today()
        now = _now()
        with Database(app.state.db_path) as db:
            state = _track_state(db, _track_key(day, user["nick"]))
            manual_sec = _manual_seconds(db, _lichess_manual_key(day, user["nick"]))
            games = db.get_games(user["nick"], limit=100_000)
        site_sec = 0.0
        if state is not None:
            site_sec = state["total"] + max(0.0, min(now - state["last"], 60.0))
        lichess_sec = 0.0
        games_today = 0
        for game in games:
            if timeutil.date_of_ms(game.created_at) != day:
                continue
            games_today += 1
            lichess_sec += _clock_seconds(game.clocks)
        return {
            "date": day,
            "site_sec": int(site_sec),
            "lichess_sec": int(lichess_sec),
            "manual_sec": int(manual_sec),
            "total_sec": int(site_sec + lichess_sec + manual_sec),
            "games_today": games_today,
        }

    # --- Тренер дебютов: сессии и фоновая аналитика ---
    def _opening_owned(db: Database, nick: str, session_id: str) -> dict | None:
        """Сессия по id, только если она принадлежит ``nick`` (иначе None)."""
        session = db.get_opening_session(session_id)
        if session is None:
            return None
        if (session.get("player") or "").lower() != nick.lower():
            return None
        return session

    @app.get("/api/opening/styles")
    def api_opening_styles(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return {
            "styles": [
                {
                    "key": key,
                    "label": STYLE_LABELS_RU.get(key, key),
                    "description": STYLE_DESCRIPTIONS_RU.get(key, ""),
                }
                for key in STYLES
            ]
        }

    @app.post("/api/opening/start")
    def api_opening_start(request: Request, body: OpeningStartBody):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        style = (body.style or "").strip()
        if style not in STYLES:
            raise HTTPException(400, f"Стиль: один из {', '.join(STYLES)}")
        color = (body.color or "").strip()
        if color not in ("white", "black"):
            raise HTTPException(400, "Цвет: white или black")
        for value in (body.opp_elo, body.my_elo):
            if value is not None and value <= 0:
                raise HTTPException(400, "Рейтинг: положительное число")

        nick = user["nick"]
        session_id = uuid.uuid4().hex
        with Database(app.state.db_path) as db:
            db.init_db()
            my_elo, opp_elo = _opening_resolve_elos(db, nick, body)
            game = new_game(my_elo, opp_elo, style, color)
            requested = [str(x).strip() for x in (body.start_moves or []) if str(x).strip()]
            if requested:
                board = chess.Board()
                normalized: list[str] = []
                for san in requested[:24]:
                    try:
                        move = board.parse_san(san)
                    except ValueError as exc:
                        raise HTTPException(
                            400, f"Нелегальный ход в стартовой линии: {san}"
                        ) from exc
                    normalized.append(board.san(move))
                    board.push(move)
                game.moves = normalized
            db.save_opening_session(
                session_id,
                nick,
                my_elo=my_elo,
                opp_elo=opp_elo,
                style=style,
                color=color,
                moves=list(game.moves),
                status=_OPENING_IN_PROGRESS,
            )

        engine_name: str | None = None
        last_reply: dict[str, Any] | None = None
        if _opening_need_reply(game):
            # Соперник ходит первым (чёрный старт или линия закончилась на его ход).
            if body.human:
                time.sleep(random.uniform(1.0, 3.0))
            policy, engine_name = _opening_policy(opp_elo, my_elo)
            try:
                san, reason = opponent_move(game, policy, rng=random.Random())
            finally:
                _close_opening_policy(policy, engine_name)
            last_reply = {"san": san, "reason": reason}
            with Database(app.state.db_path) as db:
                db.init_db()
                db.update_opening_session(session_id, moves=game.moves)

        with Database(app.state.db_path) as db:
            db.init_db()
            session = db.get_opening_session(session_id)
        return _opening_state(session, engine=engine_name, last_reply=last_reply)

    @app.post("/api/opening/move")
    def api_opening_move(request: Request, body: OpeningMoveBody):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, nick, body.session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if session.get("status") != _OPENING_IN_PROGRESS:
            raise HTTPException(400, "Партия уже завершена")

        game = _opening_game_from_session(session)
        try:
            user_move(game, (body.san or "").strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # Только ход игрока: ответ соперника считает отдельный /reply,
        # чтобы фронтенд показал ход сразу, не дожидаясь соперника.
        finished = is_finished(game)
        with Database(app.state.db_path) as db:
            db.init_db()
            kwargs: dict[str, Any] = {
                "moves": game.moves,
                "status": _OPENING_DONE if finished else _OPENING_IN_PROGRESS,
            }
            if finished:
                kwargs["result"] = result(game)
            db.update_opening_session(body.session_id, **kwargs)
            session = db.get_opening_session(body.session_id)
        return _opening_state(session)

    @app.post("/api/opening/reply")
    def api_opening_reply(request: Request, body: OpeningReplyBody):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, nick, body.session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")

        # Идемпотентно: не очередь соперника или партия завершена — без хода.
        if session.get("status") != _OPENING_IN_PROGRESS:
            return _opening_state(session)
        game = _opening_game_from_session(session)
        if not _opening_need_reply(game):
            return _opening_state(session)

        if body.human:
            time.sleep(random.uniform(1.0, 3.0))
        policy, engine_name = _opening_policy(game.opp_elo, game.my_elo)
        try:
            san, reason = opponent_move(game, policy, rng=random.Random())
        finally:
            _close_opening_policy(policy, engine_name)
        last_reply = {"san": san, "reason": reason}

        finished = is_finished(game)
        with Database(app.state.db_path) as db:
            db.init_db()
            kwargs: dict[str, Any] = {
                "moves": game.moves,
                "status": _OPENING_DONE if finished else _OPENING_IN_PROGRESS,
            }
            if finished:
                kwargs["result"] = result(game)
            db.update_opening_session(body.session_id, **kwargs)
            session = db.get_opening_session(body.session_id)
        return _opening_state(session, engine=engine_name, last_reply=last_reply)

    @app.post("/api/opening/analyze")
    def api_opening_analyze(request: Request, body: OpeningAnalyzeBody):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, nick, body.session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if session.get("status") not in (_OPENING_DONE, _OPENING_ANALYZED):
            raise HTTPException(400, "Сначала доиграй партию")
        _check_job_limits(nick)
        job = CoachJob(id=uuid.uuid4().hex, user=nick, phase="Анализ дебюта")
        _register_job(job)
        _persist_job(app.state.db_path, job, session_id=body.session_id)
        threading.Thread(
            target=_run_opening_analyze_job,
            args=(app, job, body.session_id),
            daemon=True,
        ).start()
        return {"job_id": job.id, "status": "started"}

    @app.get("/api/opening")
    def api_opening_list(request: Request, limit: int = 20):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            sessions = db.get_opening_sessions(user["nick"], limit)
        return {"sessions": [_opening_state(s) for s in sessions]}

    @app.get("/api/opening/{session_id}")
    def api_opening_get(session_id: str, request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, user["nick"], session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        return _opening_state(session)

    @app.get("/api/opening/{session_id}/legal")
    def api_opening_legal(session_id: str, request: Request):
        """Легальные ходы текущей позиции — для click-to-move на доске.

        Ходы отдаются, только когда сейчас ход игрока и партия не завершена;
        иначе список пуст (ходит соперник или всё закончилось).
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, user["nick"], session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")

        game = _opening_game_from_session(session)
        board = board_from_moves(game.moves)
        user_is_white = game.color == "white"
        your_turn = (
            not is_finished(game)
            and board.turn == (chess.WHITE if user_is_white else chess.BLACK)
        )
        moves = (
            [
                {
                    "from": chess.square_name(m.from_square),
                    "to": chess.square_name(m.to_square),
                    "san": board.san(m),
                }
                for m in board.legal_moves
            ]
            if your_turn
            else []
        )
        return {
            "session_id": session_id,
            "fen": board.fen(),
            "turn": "white" if board.turn == chess.WHITE else "black",
            "your_turn": your_turn,
            "moves": moves,
        }

    # --- Тренер миттельшпиля: сессии, Maia и фоновый разбор ---
    def _require_user(request: Request) -> dict[str, Any]:
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(401, "Нужен вход")
        return user

    def _middlegame_owned(
        db: Database, nick: str, session_id: str
    ) -> mg.MiddlegameSession | None:
        session = db.get_middlegame_session(session_id)
        if session is None:
            return None
        if session.user.lower() != nick.lower():
            return None
        return session

    @app.get("/api/middlegame/meta")
    def api_middlegame_meta(request: Request):
        user = _require_user(request)
        with Database(app.state.db_path) as db:
            db.init_db()
            played = len(
                db.recent_middlegame_keys(user["nick"], limit=1_000_000)
            )
            base_elo = _middlegame_base_elo(db, user["nick"])
        return {
            "base_elo": base_elo,
            "total": len(mg.POSITIONS),
            "sources": [
                {
                    "id": source["id"],
                    "title": source["title"],
                    "count": source["count"],
                }
                for source in mg.SOURCES
            ],
            "played": played,
        }

    @app.post("/api/middlegame/session")
    def api_middlegame_create(request: Request):
        user = _require_user(request)
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            avoid = db.recent_middlegame_keys(nick, limit=16)
            position = mg.pick_position(nick, avoid_keys=avoid)
            session = mg.MiddlegameSession(
                user=nick,
                position_key=position.key,
                fen=position.fen,
            )
            db.create_middlegame_session(session)
        return {"session": _middlegame_payload(session)}

    @app.post("/api/middlegame/session/{session_id}/plan")
    def api_middlegame_plan(
        session_id: str, request: Request, body: MiddlegamePlanBody
    ):
        user = _require_user(request)
        nick = user["nick"]
        strength = (body.strength or "equal").strip()
        if strength not in _MIDDLEGAME_STRENGTHS and strength != "hybrid":
            raise HTTPException(
                400,
                "Сила соперника: equal, plus100, plus200 или hybrid",
            )
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _middlegame_owned(db, nick, session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if session.status != "plan":
            raise HTTPException(409, "План уже отправлен")
        try:
            plan = mg.validate_plan(body.plan)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        with Database(app.state.db_path) as db:
            db.init_db()
            base = _middlegame_base_elo(db, nick)
            session.plan = plan
            session.opp_elo = _middlegame_opp_elo(base, strength)
            session.status = "playing"
            db.update_middlegame_session(session)
        return {"session": _middlegame_payload(session)}

    @app.post("/api/middlegame/session/{session_id}/move")
    def api_middlegame_move(
        session_id: str, request: Request, body: MiddlegameMoveBody
    ):
        user = _require_user(request)
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _middlegame_owned(db, nick, session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if session.status != "playing":
            detail = (
                "Сначала сформулируйте план"
                if session.status == "plan"
                else "Партия уже разобрана"
            )
            raise HTTPException(409, detail)
        try:
            mg.apply_user_move(session, (body.move or "").strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with Database(app.state.db_path) as db:
            db.init_db()
            db.update_middlegame_session(session)
        return {
            "session": _middlegame_payload(session),
            "need_reply": mg.need_reply(session),
        }

    @app.post("/api/middlegame/session/{session_id}/reply")
    def api_middlegame_reply(session_id: str, request: Request, human: bool = False):
        user = _require_user(request)
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _middlegame_owned(db, nick, session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if not mg.need_reply(session):
            return {"session": _middlegame_payload(session)}

        # Имитация «человеческого» обдумывания: в режиме fast ход мгновенный,
        # в режиме human — пауза 1..4 с, чтобы соперник не играл «пулемётом».
        if human:
            time.sleep(random.uniform(1.0, 4.0))

        with Database(app.state.db_path) as db:
            db.init_db()
            base = _middlegame_base_elo(db, nick)
        opp_elo = int(session.opp_elo if session.opp_elo is not None else settings.maia_default_elo)
        if human:
            time.sleep(random.uniform(1.0, 3.0))
        policy, engine_name = _opening_policy(opp_elo, base)
        try:
            board = mg.board_of(session)
            try:
                candidates = policy.probs(board, k=8)
            except Exception as exc:
                raise HTTPException(
                    503, "Не удалось получить ход соперника"
                ) from exc
            san = _middlegame_pick_san(candidates)
            if not san:
                raise HTTPException(503, "Не удалось получить ход соперника")
            try:
                uci = board.parse_san(san).uci()
            except ValueError:
                try:
                    uci = chess.Move.from_uci(san).uci()
                except ValueError as exc:
                    raise HTTPException(
                        503, "Соперник предложил недопустимый ход"
                    ) from exc
        finally:
            _close_opening_policy(policy, engine_name)

        try:
            mg.apply_opponent_move(session, uci)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from exc
        with Database(app.state.db_path) as db:
            db.init_db()
            db.update_middlegame_session(session)
        return {"session": _middlegame_payload(session)}

    @app.post("/api/middlegame/session/{session_id}/analyze")
    def api_middlegame_analyze(session_id: str, request: Request):
        user = _require_user(request)
        nick = user["nick"]
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _middlegame_owned(db, nick, session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        if session.status == "analyzed":
            raise HTTPException(409, "Эта партия уже разобрана")
        if not session.moves:
            raise HTTPException(409, "В партии ещё нет ходов")

        with _MIDDLEGAME_JOBS_LOCK:
            running_id = _MIDDLEGAME_JOBS.get(session_id)
        if running_id:
            existing = _get_job(running_id)
            if existing is not None and existing.status == "running":
                raise HTTPException(409, "Разбор этой партии уже выполняется")
            with _MIDDLEGAME_JOBS_LOCK:
                if _MIDDLEGAME_JOBS.get(session_id) == running_id:
                    _MIDDLEGAME_JOBS.pop(session_id, None)

        _check_job_limits(nick)
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=nick,
            phase="Анализ миттельшпиля",
        )
        _register_job(job)
        with _MIDDLEGAME_JOBS_LOCK:
            _MIDDLEGAME_JOBS[session_id] = job.id
        _persist_job(app.state.db_path, job, session_id=session_id)
        threading.Thread(
            target=_run_middlegame_analyze_job,
            args=(app, job, session_id),
            daemon=True,
        ).start()
        return {"job_id": job.id}

    @app.get("/api/middlegame/session/{session_id}")
    def api_middlegame_get(session_id: str, request: Request):
        user = _require_user(request)
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _middlegame_owned(db, user["nick"], session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        return {"session": _middlegame_payload(session)}

    # --- Auth: регистрация и вход (логин+пароль+lichess) ---
    @app.post("/api/auth/register")
    def api_auth_register(
        response: Response,
        login: str = Form(""),
        password: str = Form(""),
        lichess: str = Form(""),
        fide: str = Form(""),
    ):
        clean = (login or "").strip()
        if not 1 <= len(clean) <= 64:
            raise HTTPException(400, "Логин: от 1 до 64 символов")
        if len(password or "") < 4:
            raise HTTPException(400, "Пароль: минимум 4 символа")
        lich = (lichess or "").strip()
        if not 1 <= len(lich) <= 64:
            raise HTTPException(400, "Укажи Lichess-ник")
        fide_clean = (fide or "").strip() or None
        db = Database(app.state.db_path)
        db.init_db()
        rec = db.get_user_record(clean)
        if rec and rec.get("password_hash"):
            raise HTTPException(409, "Логин занят")
        if rec:
            # CLAIM: ник уже есть без пароля (старые данные) — дописываем
            # пароль+lichess(+fide), партии и аналитика сохраняются.
            # Неактивному пользователю доступ закрыт (как при логине).
            if not _is_active_record(rec):
                raise HTTPException(403, "Доступ ограничен")
            db.set_password_hash(rec["nick"], hash_password(password))
            db.set_lichess(rec["nick"], lich)
            if fide_clean:
                db.set_user_fide(rec["nick"], fide_clean)
            nick = rec["nick"]
        else:
            now = int(time.time())
            with db._connection() as conn:
                conn.execute(
                    "INSERT INTO users(nick_lower, nick, fide_id, bound_at, "
                    "last_seen_at, password_hash, lichess, role, is_active, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        clean.lower(),
                        clean,
                        fide_clean,
                        now,
                        now,
                        hash_password(password),
                        lich,
                        "user",
                        1,
                        now,
                    ),
                )
            nick = clean
        token = secrets.token_hex(32)
        db.create_session(nick, token, days=_SESSION_DAYS)
        _set_session_cookie(response, token)
        return {"ok": True, "nick": nick}

    @app.post("/api/auth/login")
    def api_auth_login(
        response: Response,
        login: str = Form(""),
        password: str = Form(""),
    ):
        clean = (login or "").strip()
        db = Database(app.state.db_path)
        db.init_db()
        rec = db.get_user_record(clean)
        if not rec or not rec.get("password_hash"):
            raise HTTPException(401, "Неверный логин или пароль")
        if not verify_password(password or "", rec["password_hash"]):
            raise HTTPException(401, "Неверный логин или пароль")
        if not _is_active_record(rec):
            raise HTTPException(403, "Доступ ограничен")
        token = secrets.token_hex(32)
        db.create_session(rec["nick"], token, days=_SESSION_DAYS)
        _set_session_cookie(response, token)
        return {"ok": True, "nick": rec["nick"]}

    @app.post("/api/auth/logout")
    def api_auth_logout(request: Request, response: Response):
        token = request.cookies.get(_SESSION_COOKIE)
        if token:
            db = Database(app.state.db_path)
            db.init_db()
            db.delete_session(token)
        response.delete_cookie(_SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/status")
    def api_auth_status(request: Request):
        # Гейт фронта: только сессия, meta сюда не подмешивается.
        db = Database(app.state.db_path)
        db.init_db()
        nick = _session_nick(request, db)
        if nick:
            return {"logged_in": True, "nick": nick}
        return {"logged_in": False, "nick": None}

    @app.post("/api/auth/password")
    def api_auth_password(
        request: Request,
        old: str = Form(""),
        new: str = Form(""),
    ):
        db = Database(app.state.db_path)
        db.init_db()
        nick = _session_nick(request, db)
        if not nick:
            raise HTTPException(401, "Нужен вход")
        rec = db.get_user_record(nick)
        if (
            not rec
            or not rec.get("password_hash")
            or not verify_password(old or "", rec["password_hash"])
        ):
            raise HTTPException(401, "Неверный старый пароль")
        if len(new or "") < 4:
            raise HTTPException(400, "Новый пароль: минимум 4 символа")
        db.set_password_hash(nick, hash_password(new))
        # Все живые сессии (включая текущую) больше не действуют:
        # с новым паролем нужно войти заново.
        db.delete_user_sessions(nick)
        return {"ok": True}

    @app.post("/api/auth/lichess")
    def api_auth_lichess(request: Request, lichess: str = Form("")):
        lich = (lichess or "").strip()
        if not 1 <= len(lich) <= 64:
            raise HTTPException(400, "Lichess-ник: от 1 до 64 символов")
        db = Database(app.state.db_path)
        db.init_db()
        nick = _session_nick(request, db)
        if not nick:
            raise HTTPException(401, "Нужен вход")
        db.set_lichess(nick, lich)
        return {"ok": True, "lichess": lich}

    # Frontend static files — mounted LAST so /api/* routes take precedence.
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app

# --- CLI entrypoint --------------------------------------------------------

def run_web(host: str = "127.0.0.1", port: int = 8000, no_browser: bool = False) -> int:
    """Запускает uvicorn с API + фронтендом. Возвращает код выхода."""
    try:
        import uvicorn
    except ImportError:
        print("Ошибка: uvicorn не установлен - pip install -r requirements.txt")
        return 1

    from .api import create_api
    app = create_api()
    url = f"http://{host}:{port}"
    print(f"Chess Trainer - API + frontend -> {url}  (Ctrl+C для остановки)")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0