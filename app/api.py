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
import shutil
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
from . import endgame as eg
from . import middlegame as mg
from . import reading
from . import theory
from . import timeutil
from .analyzer import Engine, analysis_needs_refresh, win_percent
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
from . import facttext
from .humanize import humanize_report
from .intent_review import intent_cache_key, review_intents
from .review import repeated_mistakes
from .llm import ChatMessage, LLMClient, _is_local_host, clean_llm_text, looks_like_refusal, looks_like_thinking
from .maia import MaiaLitePolicy
from .mentor import (
    build_mentor_facts,
    build_mentor_request,
    format_mentor_reply,
    mentor_facts_brief,
    run_mentor,
)
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
from .patterns import analyze_user_moves, phase
from .review_moves import badge_for, badge_label, detect_sacrifice
from .pgn_import import MAX_PGN_BYTES, parse_pgn_games
from .plan import build_plan
from .progress import (
    build_progress,
    monday_of_week,
    snapshot_delta,
    summarize_recent,
    MIN_NEW_GAMES,
    MIN_SNAPSHOT_AGE_DAYS,
    SNAPSHOT_CAVEAT,
    SNAPSHOT_VERSION,
)
from .opening_book import build_opening_book
from .repertoire import Repertoire, is_weak_opening, opening_stats
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


def _purge_user_jobs(nick: str) -> None:
    """Убирает in-memory задачи удалённого профиля.

    Иначе новый владелец того же ника увидит старые ``result`` через
    /api/jobs/active. Persisted ``job:*`` meta чистит Database.delete_user.
    """
    key = (nick or "").strip().lower()
    if not key:
        return
    removed: set[str] = set()
    with _JOBS_LOCK:
        for job_id, job in list(_JOBS.items()):
            if str(job.user or "").strip().lower() == key:
                _JOBS.pop(job_id, None)
                removed.add(job_id)
    if removed:
        with _MIDDLEGAME_JOBS_LOCK:
            for session_id, job_id in list(_MIDDLEGAME_JOBS.items()):
                if job_id in removed:
                    _MIDDLEGAME_JOBS.pop(session_id, None)


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


#: Лёгкие задачи (FIDE/человечность) не занимают движок, поэтому не должны
#: упираться в глобальный лимит. Но один пользователь не должен плодить их
#: без ограничений.
LIGHT_JOBS_PER_USER_LIMIT = 5


def _check_light_job_limits(nick: str) -> None:
    if _user_running_jobs_count(nick) >= LIGHT_JOBS_PER_USER_LIMIT:
        raise HTTPException(
            503, "Слишком много задач — дождись завершения"
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
                # Не сохраняем копию чужой партии: analyses общий по game_id.
                games = [
                    g for g in games
                    if not _analysis_has_foreign_copy(db, g.id, job.user)
                ]
                db.save_games(games, job.user)

            qualified = [
                g for g in games
                if g.is_finished() and g.moves
                and not _analysis_has_foreign_copy(db, g.id, job.user)
            ]
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


def _analysis_has_foreign_copy(db: Database, game_id: str, user: str) -> bool:
    """У партии с этим id есть копия у другого игрока.

    analyses общий по game_id; без этой проверки импорт PGN с чужим Site id
    позволял бы прочитать/перезаписать чужой анализ.
    """
    return any(
        p.strip().lower() != (user or "").strip().lower()
        for p in db.game_players(game_id)
    )


def _analysis_for_user(db: Database, game_id: str, user: str) -> dict | None:
    if _analysis_has_foreign_copy(db, game_id, user):
        return None
    return db.get_analysis(game_id)


def _select_analyzed_pairs(
    db: Database,
    user: str,
    game_id: str | None = None,
    max_games: int | None = None,
) -> list:
    """Отбирает пары (игра, анализ) для LLM-инструментов: конкретная
    партия либо топ-партии по blunders+mistakes. Ошибки — RuntimeError."""
    if game_id:
        game = db.get_game(game_id, user)
        if game is None:
            raise RuntimeError(f"Партия {game_id} не найдена в кеше")
        analysis = _analysis_for_user(db, game_id, user)
        if analysis is None:
            if _analysis_has_foreign_copy(db, game_id, user):
                raise RuntimeError("Партия уже есть в кеше у другого игрока")
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


def _review_entry(
    game,
    analysis,
    idx,
    moments: dict,
    summary: str | None,
    meta: dict[str, Any] | None = None,
) -> dict:
    """Формирует запись «Одна партия» для разбора (поля как в CLI-выводе).

    ``meta`` — ``meta`` из :func:`app.coach.run_coach`: по нему моментам
    и summary проставляется ``text_source``/``summary_source`` (``"llm"`` |
    ``"facts"``), чтобы UI различал проверенную прозу модели и факты.
    """
    moves = analysis.get("moves") or []
    sources = (meta or {}).get("moments_source") or {}
    entry_moments = []
    for ply in idx:
        move = moves[ply] if ply < len(moves) else {}
        drop = move.get("drop")
        text = moments.get(ply)
        entry_moments.append(
            {
                "ply": ply + 1,
                "san": move.get("san"),
                "classification": move.get("classification"),
                "drop": drop if isinstance(drop, (int, float)) else None,
                "text": text,
                "text_source": sources.get(ply, "llm") if text else None,
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
        "summary_source": (meta or {}).get("summary_source"),
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
        llm_error: str | None = None
        level = None
        try:
            if not params["dry_run"]:
                _ensure_llm_key()
                llm = LLMClient()
        except Exception as exc:  # LLM недоступен — facts-only разбор
            logger.info("review: LLM недоступен, показываю факты: %s", exc)
            llm_error = str(exc)
        with Database(app.state.db_path) as db:
            db.init_db()
            level = _plan_player_level(db, job.user)

        games = []
        prompts = []
        for game, analysis in pairs:
            messages, idx = build_request(
                game, analysis, params["moments"], level=level
            )
            if not messages:
                continue
            if params["dry_run"]:
                prompts.extend(
                    {"game_id": game.id, "role": m.role, "content": m.content}
                    for m in messages
                )
                games.append(_review_entry(game, analysis, idx, {}, None))
                continue
            moments, summary, meta = run_coach(
                game, analysis, llm, max_moments=params["moments"], level=level
            )
            moments = {
                ply: clean_llm_text(text) for ply, text in moments.items()
            }
            if summary:
                summary = clean_llm_text(summary)
            games.append(_review_entry(game, analysis, idx, moments, summary, meta))

        job.result = {"games": games}
        if params["dry_run"]:
            job.result["dry_run"] = True
            job.result["prompts"] = prompts
            job.summary = f"Промптов собрано: {len(prompts)} для {len(games)} партий"
        else:
            if llm_error:
                job.result["llm_error"] = llm_error
                job.summary = (
                    f"Разобрано партий: {len(games)} "
                    "(LLM недоступен — показаны факты движка)"
                )
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
        with Database(app.state.db_path) as db:
            db.init_db()
            level = _plan_player_level(db, nick)
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
            level=level,
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
            facts = build_mentor_facts(plan_report, progress_report)
            brief = mentor_facts_brief(plan_report, progress_report)
            facts_only = False
            llm_error: str | None = None
            model: str | None = None
            try:
                _ensure_llm_key()
                llm = LLMClient()
                model = llm.model
                reply, meta = run_mentor(llm, request, facts=facts, brief=brief)
                if meta.get("source") != "llm":
                    facts_only = True
                    model = None
                    llm_error = meta.get("error") or "ответ модели не прошёл проверку фактов"
            except Exception as exc:  # LLM недоступен — recoverable facts-only
                logger.info("mentor: LLM недоступен, показываю факты: %s", exc)
                facts_only = True
                reply = brief
                model = None
                llm_error = str(exc)
            job.result = {
                "user": plan_report.get("user"),
                "model": model,
                "notes": params.get("notes") or "",
                "reply": format_mentor_reply(clean_llm_text(reply)),
                "facts_only": facts_only,
                "llm_error": llm_error,
            }
            job.summary = (
                f"План тренера по {plan_report.get('games', 0)} партиям (по фактам движка)"
                if facts_only
                else f"План тренера по {plan_report.get('games', 0)} партиям"
            )
        job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished = time.time()


def _run_task_explain_job(app: FastAPI, job: CoachJob, params: dict[str, Any]) -> None:
    """Фоновый поток: LLM-объяснение «Задачи дня» → таблица task_explanations.

    LLM опционален: при недоступности/невалидном ответе задача не падает —
    job завершается ``done`` с ``facts_only: true`` и ``explanation: null``,
    а UI показывает facts-only разбор задачи (``explanation_facts``).
    """
    try:
        payload = params["payload"]
        job.phase = "Объяснение задачи"
        try:
            _ensure_llm_key()
            text = _task_explain_llm(payload)
            if not text:
                raise RuntimeError("LLM вернул пустое объяснение")
        except Exception as exc:  # LLM недоступен — recoverable facts-only
            logger.info("task explain: LLM недоступен, показываю факты: %s", exc)
            job.result = {"explanation": None, "facts_only": True, "reason": str(exc)}
            job.summary = "LLM недоступен — показан разбор по фактам движка"
            job.status = "done"
            job.finished = time.time()
            return
        with Database(app.state.db_path) as db:
            db.init_db()
            db.save_task_explanation(
                payload["task_key"],
                job.user,
                payload["game_id"],
                payload["ply"],
                text,
            )
        job.result = {"explanation": text, "facts_only": False}
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


class EndgameAnswerBody(BaseModel):
    """Тело POST /api/endgame/answer."""

    source: str = ""
    task_key: str = ""
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


class ReportRefreshBody(BaseModel):
    """Тело POST /api/report/refresh: явное подтверждение и отбор партий.

    ``confirm`` обязателен: обновление пересобирает анализы заново
    (Stockfish, время пропорционально числу партий). ``game_ids`` — точный
    список партий (только свои проанализированные); отсутствие списка —
    все устаревшие анализы. ``limit`` — потолок партий за один вызов.
    """

    confirm: bool = False
    game_ids: list[str] | None = None
    limit: int = 10


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


def _opening_move_fens(moves: list[str]) -> list[str]:
    """FEN стартовой позиции и после каждого полухода (для навигации)."""
    cursor = chess.Board()
    fens = [cursor.fen()]
    for san in moves:
        try:
            move = cursor.parse_san(san)
        except ValueError:
            break
        cursor.push(move)
        fens.append(cursor.fen())
    return fens


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
        "fens": _opening_move_fens(list(game.moves)),
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
    if body.opp_elo is not None:
        opp_elo = body.opp_elo
    elif int(my_elo) >= 2000:
        # Maia не различает силу выше ~2000 Elo, поэтому для сильных игроков
        # по умолчанию включаем гибрид Maia+Stockfish (sentinel -1), иначе
        # соперник выглядит заметно слабее заявленного рейтинга.
        opp_elo = -1
    else:
        opp_elo = base + 100
    return int(my_elo), int(opp_elo)


def _theory_ideas_facts(line: list[Any]) -> dict[str, Any]:
    """Facts-объект для идей теории: ходы линии и статистика Explorer."""
    sans: list[str] = []
    numbers: list[float] = []
    for item in line or []:
        if isinstance(item, str):
            item = {"san": item}
        if not isinstance(item, dict):
            continue
        san = item.get("san")
        if san:
            sans.append(str(san))
        for key in ("games", "white", "draws", "black"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers.append(float(value))
    name, eco = classify_opening(sans)
    return facttext.new_facts(
        sans=sans,
        numbers=numbers,
        openings=[name] if name else (),
        ecos=[eco] if eco else (),
        strings=[s for s in (name, eco) if s],
    )


def _opening_user_entries(
    analysis: dict[str, Any],
    user_color: str,
    plies: int,
    *,
    moves: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Ходы пользователя из анализа движка (в пределах дебюта ``plies``).

    Дополнительно сохраняет FEN перед ходом и линию движка: это нужно для
    детерминированного текста без выдуманных полей и фигур.
    """
    entries = (analysis.get("moves") or [])[:plies]
    user_is_white = user_color == "white"
    out: list[dict[str, Any]] = []
    board = chess.Board()
    for ply, entry in enumerate(entries):
        fen_before = board.fen()
        if (ply % 2 == 0) == user_is_white and isinstance(entry, dict):
            out.append(
                {
                    "ply": ply + 1,
                    "san": entry.get("san"),
                    "classification": entry.get("classification"),
                    "drop": entry.get("drop"),
                    "win_before": entry.get("win_before"),
                    "win_after": entry.get("win_after"),
                    "best": entry.get("best_move_san"),
                    "best_line": entry.get("best_line") or [],
                    "alternatives": entry.get("alternatives") or [],
                    "fen": fen_before,
                }
            )
        if moves and ply < len(moves):
            try:
                board.push_san(str(moves[ply]))
            except ValueError:
                break
    return out


def _deepen_opening_errors(
    analysis: dict[str, Any],
    moves: list[str],
    user_color: str,
    *,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Перепроверяет лучший ход в позициях ошибок на большей глубине.

    Основной проход быстрый (глубина 14, multipv 3). Для 2600-игрока best move
    в неточности должен быть устойчивым, поэтому ошибочные позиции считаем
    ещё раз depth+4 и сохраняем 1-2 близкие альтернативы.
    """
    entries = analysis.get("moves") or []
    if not isinstance(entries, list):
        return analysis
    user_is_white = user_color == "white"
    board = chess.Board()
    deep_depth = max(int(settings.stockfish_depth) + 4, 18)
    owned = engine is None
    if owned:
        engine = Engine(depth=deep_depth, multipv=3)
        engine.__enter__()
    old_depth = getattr(engine, "depth", None)
    old_multipv = getattr(engine, "multipv", None)
    try:
        if hasattr(engine, "depth"):
            engine.depth = deep_depth
        if hasattr(engine, "multipv"):
            engine.multipv = 3
        for ply, raw_san in enumerate(moves):
            entry = entries[ply] if ply < len(entries) else None
            is_user = (ply % 2 == 0) == user_is_white
            if (
                is_user
                and isinstance(entry, dict)
                and entry.get("classification") in ("inaccuracy", "mistake", "blunder")
            ):
                try:
                    lines = engine.analyze_position(board)
                except AttributeError:
                    # Тестовый/упрощённый движок может не уметь deep-pass.
                    return analysis
                if lines:
                    top = lines[0]
                    pv = [str(move) for move in (top.get("pv") or [])]
                    if pv:
                        entry["best_move_san"] = pv[0]
                        entry["best_line"] = pv[1:7]
                        entry["best_eval"] = dict(top.get("eval") or {})
                        if top.get("win") is not None:
                            entry["best_win"] = top["win"]
                        top_win = top.get("win")
                        alternatives: list[str] = []
                        for alt_line in lines[1:3]:
                            alt_pv = [str(move) for move in (alt_line.get("pv") or [])]
                            if not alt_pv or alt_pv[0] == pv[0]:
                                continue
                            if str(alt_pv[0]).rstrip("+#") == str(entry.get("san") or "").rstrip("+#"):
                                continue
                            alt_win = alt_line.get("win")
                            if (
                                isinstance(top_win, (int, float))
                                and isinstance(alt_win, (int, float))
                                and abs(float(top_win) - float(alt_win)) > 3.0
                            ):
                                continue
                            alternatives.append(alt_pv[0])
                        entry["alternatives"] = alternatives
            try:
                board.push_san(str(raw_san))
            except ValueError:
                break
        return analysis
    finally:
        if old_depth is not None and hasattr(engine, "depth"):
            engine.depth = old_depth
        if old_multipv is not None and hasattr(engine, "multipv"):
            engine.multipv = old_multipv
        if owned:
            engine.__exit__(None, None, None)


def _normalize_opening_moments(
    raw: Any, fallback_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Оставляет только реальные ошибки игрока; «лучше» — строго из движка.

    LLM любит добавлять «хорошие» ходы и придумывать альтернативы. Здесь мы
    фильтруем моменты по классификации из анализа и подменяем san/better
    фактами движка, чтобы в отчёте не было галлюцинаций.
    """
    errors = {
        int(item["ply"]): item
        for item in fallback_entries
        if item.get("ply") is not None
        and item.get("classification") in ("blunder", "mistake", "inaccuracy")
    }
    moments: list[dict[str, Any]] = []
    seen: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                ply = int(item.get("ply"))
            except (TypeError, ValueError):
                continue
            entry = errors.get(ply)
            if entry is None or ply in seen:
                continue
            seen.add(ply)
            moments.append(
                {
                    "ply": ply,
                    "san": entry.get("san"),
                    "kind": "bad" if entry.get("classification") == "blunder" else "better",
                    "text": item.get("text"),
                    "better": entry.get("best"),
                    "classification": entry.get("classification"),
                    "drop": entry.get("drop"),
                }
            )
    # LLM может пропустить реальную ошибку (особенно зевок) — добираем
    # оставшиеся ошибки из движка, чтобы отчёт не терял критичные ходы.
    severity = {"blunder": 0, "mistake": 1, "inaccuracy": 2}

    def _rank(item: tuple[int, dict[str, Any]]) -> tuple[int, float, int]:
        ply, entry = item
        drop = entry.get("drop")
        loss = float(drop) if isinstance(drop, (int, float)) and not isinstance(drop, bool) else 0.0
        return (
            severity.get(str(entry.get("classification")), 9),
            -loss,
            ply,
        )

    if not moments:
        # Показываем в первую очередь худшие ошибки, а не первые по ходу.
        for ply, entry in sorted(sorted(errors.items(), key=_rank)[:4], key=lambda item: item[0]):
            moments.append(
                {
                    "ply": ply,
                    "san": entry.get("san"),
                    "kind": "bad" if entry.get("classification") == "blunder" else "better",
                    "text": None,
                    "better": entry.get("best"),
                    "classification": entry.get("classification"),
                    "drop": entry.get("drop"),
                }
            )
            seen.add(ply)
    missing = [
        (ply, entry)
        for ply, entry in errors.items()
        if ply not in seen
    ]
    missing.sort(key=_rank)
    for ply, entry in missing:
        if len(moments) >= 4:
            break
        moments.append(
            {
                "ply": ply,
                "san": entry.get("san"),
                "kind": "bad" if entry.get("classification") == "blunder" else "better",
                "text": None,
                "better": entry.get("best"),
                "classification": entry.get("classification"),
                "drop": entry.get("drop"),
            }
        )
    moments.sort(key=lambda item: item["ply"])
    return moments[:4]


#: Лимит completion для JSON-отчётов: reasoning-модели иначе съедают весь бюджет.
_REPORT_MAX_TOKENS = 7000


def _llm_json_with_retries(
    messages: list[ChatMessage],
    *,
    error_prefix: str,
    temperature: float = 0.4,
    max_tokens: int | None = None,
    validator: Any | None = None,
) -> dict[str, Any]:
    """JSON-ответ LLM с тремя попытками.

    Free-модели в ``json_mode`` иногда истрачивают весь лимит на reasoning и
    возвращают пустоту (``finish_reason=length``), а отключение reasoning у
    провайдера тоже может не сработать. Порядок: обычный запрос → без reasoning
    → снова обычный с жёстким запретом текста вокруг JSON.

    ``validator`` — необязательный callable(dict) -> None; любое исключение
    от него трактуется как неудачная попытка (с подсказкой в следующей),
    после трёх неудач — ``RuntimeError`` с префиксом ``error_prefix``.
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
    violation_hint = ""
    for attempt_messages, kwargs in attempts:
        if violation_hint:
            attempt_messages = attempt_messages[:-1] + [
                ChatMessage("user", attempt_messages[-1].content + violation_hint)
            ]
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
            violation_hint = ""
            continue
        if isinstance(data, dict) and validator is not None:
            try:
                validator(data)
            except ValueError as exc:
                last_error = RuntimeError(f"ответ модели не прошёл проверку фактов: {exc}")
                violation_hint = _violations_hint(
                    [str(exc)] if str(exc) else ["ответ без опоры на факты"]
                )
                continue
        if isinstance(data, dict):
            return data
    detail = f" (последняя ошибка: {last_error})" if last_error else ""
    raise RuntimeError(f"{error_prefix}: 3 попытки не дали JSON{detail}")


def _build_opening_report(
    session: dict[str, Any],
    moves: list[str],
    user_entries: list[dict[str, Any]],
    opening: tuple[str | None, str | None],
    analysis: dict[str, Any],
    *,
    depth: int | None = None,
) -> dict[str, Any]:
    """Stockfish-отчёт в одном JSON: только проверяемые факты.

    LLM здесь не используется: у 2600-игрока выдуманное «лучше e5» вызывает
    больше вопросов, чем приносит пользы. Моменты, итог и рекомендация
    собираются из ходов, оценок и линий Stockfish.
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
    # Моменты собираем детерминированно из фактов движка: никаких
    # придуманных «лучше e5» для хорошего хода или полей, которых нет в FEN.
    moments = _normalize_opening_moments([], user_entries)
    by_ply = {item["ply"]: item for item in user_entries}
    for moment in moments:
        source = by_ply.get(moment["ply"])
        if source is not None:
            moment["text"] = facttext.error_text(source, source.get("fen"))
            # «Почему» — только проверяемые факты движка и FEN перед ходом.
            moment["why"] = facttext.why_text(
                source, source.get("fen"), source.get("san")
            )
            moment["best_line"] = source.get("best_line") or []
            # FEN перед сыгранным ходом — для показа позиции в интерфейсе.
            moment["fen"] = source.get("fen")

    # Итог и рекомендацию не отдаём LLM: только проверяемые факты.
    summary = facttext.opening_summary(user_entries, name)
    recommendation = facttext.opening_recommendation(user_entries)
    # Секцию «как играл соперник» убрали: это стиль из анкеты, а не факт
    # партии. Для сильного игрока она была водой.
    opponent = None

    return {
        "opening": {"name": name, "eco": eco},
        "summary": summary,
        "moments": moments,
        "opponent": opponent,
        "recommendation": recommendation,
        "style": style,
        "my_elo": session.get("my_elo"),
        "opp_elo": session.get("opp_elo"),
        "engine": "stockfish",
        "depth": int(depth) if depth is not None else int(settings.stockfish_depth),
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
        # Один проход на заявленной глубине: best move, линия, win% и
        # классификация должны быть из одного поиска. Threads=1 — чтобы
        # 2600-игрок мог воспроизвести те же ходы и числа.
        opening_depth = max(int(settings.stockfish_depth), 18)
        with Engine(
            depth=opening_depth,
            multipv=3,
            threads=1,
            time_ms=0,
        ) as engine:
            analysis = engine.analyze_game(game).summary()

        user_entries = _opening_user_entries(
            analysis,
            session.get("color") or "white",
            int(settings.opening_plies),
            moves=moves,
        )
        opening = classify_opening(moves)

        job.phase = "Отчёт тренера"
        _persist_job(app.state.db_path, job, session_id=session_id)
        report = _build_opening_report(
            session, moves, user_entries, opening, analysis, depth=opening_depth
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


def _pv_to_san(board: chess.Board, pv: list[Any]) -> list[str]:
    """UCI-линия движка → SAN-линия в текущей позиции (без выдумывания)."""
    result: list[str] = []
    cursor = board.copy()
    for raw in pv or []:
        text = str(raw).strip()
        move = None
        try:
            candidate = chess.Move.from_uci(text)
            if candidate in cursor.legal_moves:
                move = candidate
        except ValueError:
            pass
        if move is None:
            try:
                move = cursor.parse_san(text)
            except ValueError:
                break
        result.append(cursor.san(move))
        cursor.push(move)
    return result


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
            pv_before = _pv_to_san(board, [str(move) for move in (top.get("pv") or [])][:4])
            best_san = pv_before[0] if pv_before else None
            second_san = None
            if len(lines) > 1:
                second_pv = _pv_to_san(board, lines[1].get("pv") or [])
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
                pv_after = _pv_to_san(
                    board,
                    [str(chess_move) for chess_move in (next_lines[0].get("pv") or [])][:4],
                )
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


def _ensure_worst_moment(
    report: dict[str, Any],
    session: mg.MiddlegameSession,
    engine_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Гарантирует, что худшая ошибка игрока попала в ключевые моменты."""
    moments = report.get("moments") if isinstance(report.get("moments"), list) else []
    user_is_white = session.side == "white"
    candidates = [
        fact
        for fact in engine_facts
        if isinstance(fact, dict)
        and ((int(fact.get("ply", 0)) % 2 == 1) == user_is_white)
        and float(fact.get("delta") or 0) >= 5
    ]
    if not candidates:
        return report
    worst = max(candidates, key=lambda fact: float(fact.get("delta") or 0))
    worst_ply = worst.get("ply")
    if any(moment.get("ply") == worst_ply for moment in moments if isinstance(moment, dict)):
        return report
    moment = mg._fact_moment(
        worst,
        mg._fen_before(session.fen, list(session.moves), int(worst_ply or 0)),
    )
    moments = [m for m in moments if isinstance(m, dict)] + [moment]
    moments.sort(key=lambda item: int(item.get("ply") or 0))
    report["moments"] = moments
    return report


def _middlegame_report_llm(prompt: str, validator: Any | None = None) -> dict[str, Any]:
    """LLM-отчёт по миттельшпилю (3 попытки, см. хелпер).

    ``validator`` — необязательный callable(dict); при неуспехе — повторы
    с подсказкой, после трёх неудач RuntimeError (вызывающий код
    откатится на facts-only отчёт).
    """
    _ensure_llm_key()
    return _llm_json_with_retries(
        [ChatMessage("user", prompt)],
        error_prefix="LLM не вернул JSON-отчёт по миттельшпилю",
        temperature=0.3,
        max_tokens=_REPORT_MAX_TOKENS,
        validator=validator,
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
        position_obj = mg.POSITION_BY_KEY.get(session.position_key)
        user_is_white = session.side == "white"
        facts_for_llm = mg.build_report_facts(session, engine_facts, position_obj)

        def _validate_summary(data: dict[str, Any]) -> None:
            summary = data.get("summary") if isinstance(data, dict) else None
            if isinstance(summary, str) and summary.strip():
                facttext.assert_facts(summary.strip(), facts_for_llm)

        prompt = mg.build_report_prompt(
            session,
            engine_facts=engine_facts,
            position=position_obj,
        )
        plan_comment, lesson = mg.facts_plan_comment(engine_facts, user_is_white)
        facts_only = False
        summary_kind = "llm_opinion"
        llm_error: str | None = None
        try:
            report = mg.ground_report(
                mg.normalize_report(
                    _middlegame_report_llm(prompt, validator=_validate_summary)
                ),
                engine_facts,
                fen=session.fen,
                moves=list(session.moves),
            )
            report = _ensure_worst_moment(report, session, engine_facts)
        except Exception as exc:  # LLM недоступен — recoverable facts-only
            logger.info("middlegame: LLM недоступен, показываю факты: %s", exc)
            report = mg.build_facts_report(session, engine_facts, position_obj)
            facts_only = True
            summary_kind = "engine_facts"
            llm_error = str(exc)
        if not (isinstance(report.get("summary"), str) and report["summary"].strip()) \
                or report.get("summary") == "Итог не сформирован.":
            # Пустой/дефолтный summary модели — заменяем фактами движка.
            facts_report = mg.build_facts_report(session, engine_facts, position_obj)
            report["summary"] = facts_report["summary"]
            summary_kind = "engine_facts"
        # Вердикт — только от движка: LLM в этом поле не участвует.
        report["verdict"] = mg.engine_verdict(engine_facts, user_is_white)
        report["verdict_source"] = "engine"
        report["facts_only"] = facts_only
        report["summary_kind"] = summary_kind
        if llm_error:
            report["llm_error"] = llm_error
        # Комментарий к плану и урок — только факты, без «активизируй фигуры».
        report["plan_comment"] = plan_comment
        report["lesson"] = lesson

        with Database(app.state.db_path) as db:
            db.init_db()
            fresh = db.get_middlegame_session(session_id) or session
            fresh.report = report
            fresh.status = "analyzed"
            db.update_middlegame_session(fresh)
        job.result = {"report": report}
        job.summary = (
            f"Отчёт по миттельшпилю готов ({len(moves)} полуходов, факты движка)"
            if facts_only
            else f"Отчёт по миттельшпилю готов ({len(moves)} полуходов)"
        )
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
# Минимальная длина НОВОГО пароля (регистрация и смена пароля).
PASSWORD_MIN_LENGTH = 8


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
    """Текущий пользователь.

    Для HTTP-запроса — **только** сессия из cookie. Глобальный ``meta``
    current_user оставлен только для CLI/внутренних вызовов без Request:
    иначе запрос без cookie мог действовать от имени последнего вошедшего.
    Неактивный пользователь (is_active=0) — везде None (HTTP 404).
    """
    with Database(db_path) as db:
        db.init_db()
        if request is not None:
            nick = _session_nick(request, db)
            if not nick:
                return None
            rec = db.get_user_record(nick)
            if rec is None or not _is_active_record(rec):
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


def _request_is_https(request: Request) -> bool:
    """HTTPS с учётом reverse proxy (Heroku/Caddy/Fly)."""
    if request.url.scheme == "https":
        return True
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return proto.strip().lower() == "https"


def _set_session_cookie(
    response: Response, token: str, *, secure: bool = False
) -> None:
    """Выставляет cookie сессии (httponly, samesite=lax, 30 дней)."""
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


# --- Rate-limit логина ------------------------------------------------------
# Счётчики неудачных попыток хранятся в meta-таблице приложения (та же БД,
# что и всё состояние: SQLite data/trainer.db или PostgreSQL по
# DATABASE_URL), а не в памяти процесса — лимит переживает рестарт и общий
# для всех процессов, работающих с одной базой. Если несколько процессов
# работают с РАЗНЫМИ SQLite-файлами, лимит действует в пределах каждого
# файла — запускайте один процесс на базу или вынесите БД в PostgreSQL.
# IP берём из соединения (request.client.host); за reverse proxy он будет
# IP прокси, поэтому пороги (LOGIN_RATE_MAX_ATTEMPTS/LOGIN_RATE_WINDOW_SEC)
# надо выбирать с учётом этого.


def _login_rate_keys(request: Request, login: str) -> tuple[str, str]:
    """Ключи meta: по IP соединения и по sha256(логина)."""
    client = getattr(request, "client", None)
    ip = (client.host if client and client.host else "unknown").strip() or "unknown"
    ip_key = f"login_fail_ip:{ip}"
    digest = hashlib.sha256((login or "").strip().lower().encode("utf-8")).hexdigest()
    id_key = f"login_fail_id:{digest[:32]}"
    return ip_key, id_key


def _login_rate_state(db: Database, key: str, now: float) -> dict[str, float] | None:
    """Состояние счётчика {count, window_start}; протухшее окно → None."""
    raw = db.get_meta(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        count = float(data["count"])
        window_start = float(data["window_start"])
    except (TypeError, ValueError, KeyError):
        return None
    if window_start <= 0 or now - window_start >= settings.login_rate_window_sec:
        return None
    return {"count": count, "window_start": window_start}


def _login_rate_blocked(db: Database, keys: tuple[str, str], now: float) -> bool:
    """True, если по любому из ключей исчерпан лимит за текущее окно."""
    limit = max(1, int(settings.login_rate_max_attempts))
    for key in keys:
        state = _login_rate_state(db, key, now)
        if state is not None and state["count"] >= limit:
            return True
    return False


def _login_rate_record_failure(db: Database, keys: tuple[str, str], now: float) -> None:
    """Регистрирует неудачную попытку: новое окно или инкремент текущего."""
    for key in keys:
        state = _login_rate_state(db, key, now)
        if state is None:
            payload = {"count": 1, "window_start": now}
        else:
            payload = {
                "count": state["count"] + 1,
                "window_start": state["window_start"],
            }
        db.set_meta(key, json.dumps(payload))


def _login_rate_clear(db: Database, keys: tuple[str, str]) -> None:
    """Успешный вход: счётчики по IP и логину удаляются."""
    with db._connection() as conn:
        for key in keys:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))


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


_TASK_REPEAT_WINDOW_DAYS_DEFAULT = 7
_TASK_HISTORY_CAP = 500
_TASK_RECENCY_HALF_LIFE_DAYS = 30.0


def _task_history_key(user: str) -> str:
    """Ключ meta: история попыток задач игрока (JSON-список, новые в конце)."""
    return f"task_history_{(user or '').strip()}"


def _task_repeat_window_days() -> int:
    """K-дневное окно подавления решённых позиций (0 — отключить).

    Задаётся переменной окружения ``TASK_REPEAT_WINDOW_DAYS``; по умолчанию
    7 дней. Значения вне 0..365 зажимаются.
    """
    raw = os.environ.get("TASK_REPEAT_WINDOW_DAYS", "").strip()
    if not raw:
        return _TASK_REPEAT_WINDOW_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _TASK_REPEAT_WINDOW_DAYS_DEFAULT
    return max(0, min(365, value))


def _load_task_history(db: Database, user: str) -> list[dict[str, Any]]:
    """История попыток из meta; битые данные → пустая история, не падаем."""
    raw = db.get_meta(_task_history_key(user))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    history: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        task_key = item.get("task_key")
        date = item.get("date")
        if not isinstance(task_key, str) or not task_key:
            continue
        if not isinstance(date, str) or not date:
            continue
        try:
            attempt = int(item.get("attempt") or 0)
            ts = int(item.get("ts") or 0)
        except (TypeError, ValueError):
            attempt, ts = 0, 0
        history.append(
            {
                "task_key": task_key,
                "date": date,
                "correct": bool(item.get("correct")),
                "attempt": attempt,
                "ts": ts,
            }
        )
    return history


def _record_task_attempt(
    db: Database, user: str, task_key: str, date: str, correct: bool
) -> int:
    """Записывает попытку по задаче; возвращает её номер (1 — первая).

    История per-position: номер попытки копится по ``task_key`` и не
    сбрасывается ежедневно. Список в meta ограничен последними
    ``_TASK_HISTORY_CAP`` записями — старые попытки очищаются, чтобы
    конфиг не разрастался.
    """
    history = _load_task_history(db, user)
    prior = [
        int(item.get("attempt") or 0)
        for item in history
        if item["task_key"] == task_key
    ]
    attempt = 1 + (max(prior) if prior else 0)
    history.append(
        {
            "task_key": task_key,
            "date": date,
            "correct": bool(correct),
            "attempt": attempt,
            "ts": int(time.time() * 1000),
        }
    )
    if len(history) > _TASK_HISTORY_CAP:
        history = history[-_TASK_HISTORY_CAP:]
    db.set_meta(_task_history_key(user), json.dumps(history, ensure_ascii=False))
    return attempt


def _solve_suppress_cutoff(today: str, window_days: int) -> str | None:
    """ISO-дата, строго старше которой решения ещё подавляются; None — выкл."""
    if window_days <= 0:
        return None
    try:
        day = datetime.date.fromisoformat(today) - datetime.timedelta(days=window_days)
    except ValueError:
        return None
    return day.isoformat()


def _suppressed_task_keys(
    history: list[dict[str, Any]], today: str, window_days: int
) -> set[str]:
    """Ключи задач, решённых (correct) внутри окна последних K дней.

    Подавление касается только верно решённых позиций: неверные ответы
    не прячут позицию. ``date`` — ISO ``YYYY-MM-DD``, строки сравнимы лексикографически.
    """
    cutoff = _solve_suppress_cutoff(today, window_days)
    if cutoff is None:
        return set()
    suppressed: set[str] = set()
    for item in history:
        if not item.get("correct"):
            continue
        if str(item.get("date") or "") > cutoff:
            suppressed.add(item["task_key"])
    return suppressed


def _task_weight(item: dict[str, Any], now_ms: float | None = None) -> float:
    """Вес кандидата в задачу дня: чем свежее партия и грубее ошибка, тем выше.

    Recency задаётся half-life 30 дней; ``drop`` — множителем 1..100.
    Старые/незначительные ошибки получают низкий, но не нулевой вес.
    """
    if now_ms is None:
        now_ms = time.time() * 1000
    move = item.get("move") if isinstance(item.get("move"), dict) else {}
    drop_raw = move.get("drop")
    try:
        drop = float(drop_raw)
    except (TypeError, ValueError):
        drop = 5.0
    drop = min(max(drop, 1.0), 100.0)
    created = item.get("created_at") or 0
    try:
        created = float(created)
    except (TypeError, ValueError):
        created = 0.0
    if created <= 0:
        age_days = 3650.0
    else:
        age_days = max(0.0, (now_ms - created) / 86_400_000.0)
    recency = 0.5 ** (age_days / _TASK_RECENCY_HALF_LIFE_DAYS)
    return drop * recency


def _seeded_weighted_choice(
    fresh: list[dict[str, Any]], date: str, user: str, now_ms: float
) -> dict[str, Any]:
    """Детерминированный взвешенный выбор по seed (дата+user).

    Не криптографическое решение, а стабильный способ показать свежую
    грубую ошибку чаще, чем старую мелкую, не меняясь между запросами дня.
    """
    if len(fresh) == 1:
        return fresh[0]
    weights = [_task_weight(item, now_ms) for item in fresh]
    seed = int(hashlib.sha256(f"{date}{user}".encode("utf-8")).hexdigest(), 16)
    return random.Random(seed).choices(fresh, weights=weights, k=1)[0]


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


def _pick_daily_task(
    db: Database, user: str, date: str, repeat: bool = False
) -> dict[str, Any]:
    """Одна задача на день: взвешенный выбор, фиксируется в meta.

    Кандидаты взвешиваются по свежести партии (half-life 30 дней) и размеру
    ошибки (``drop``), выбор детерминирован seed'ом дата+user и записывается
    в meta; повторные вызовы дня читают записанный. Решённые в течение
    последних K дней (``TASK_REPEAT_WINDOW_DAYS``, по умолчанию 7) позиции
    подавляются и не предлагаются снова, пока не истечёт окно; ``repeat=True``
    — явный запрос повтора (окно игнорируется). Если подавлены все позиции,
    возбуждается RuntimeError с подсказкой про ``repeat=1``: повторный показ
    решённой позиции возможен только по явному запросу пользователя.
    Если записанная задача больше не найдена в пуле (удалили партию) —
    выбирается новая.
    """
    pool = _task_pool(db, user)
    if not pool:
        raise RuntimeError(
            f"Для {user} нет проанализированных ошибок — сначала запусти анализ "
            "(страница «Анализ»), тогда появится задача дня."
        )
    now_ms = time.time() * 1000
    history = _load_task_history(db, user)
    window = _task_repeat_window_days()
    suppressed = set() if repeat else _suppressed_task_keys(history, date, window)
    suppressed_count = sum(1 for item in pool if item["task_key"] in suppressed)
    key = _task_choice_key(date, user)
    stored = db.get_meta(key)
    chosen: dict[str, Any] | None = None
    if stored:
        # Стабильность дня важнее подавления: если сегодняшнюю задачу уже
        # выдавали (в том числе по repeat=1) и она всё ещё в пуле — возвращаем её
        # даже если игрок только что её решил (иначе страница задачи «сломается»
        # сразу после ответа).
        for item in pool:
            if item["task_key"] == stored:
                chosen = item
                break
    if chosen is None:
        fresh = [item for item in pool if item["task_key"] not in suppressed]
        if not fresh:
            # Все позиции решались недавно: повтор НЕ показываем без явного
            # запроса (repeat=1), чтобы не маскировать решённую позицию под
            # новую тему. Пустой день честнее «ещё одного повтора».
            raise RuntimeError(
                f"Все позиции в пуле ({suppressed_count} шт.) уже решались за "
                f"последние {window} дн. — новых задач нет. "
                "Явный повтор решённой позиции: /api/task/today?repeat=1 "
                "(кнопка «Повторить решённую ранее» на странице задачи)."
            )
        chosen = _seeded_weighted_choice(fresh, date, user, now_ms)
        db.set_meta(key, chosen["task_key"])
    task_history = [item for item in history if item["task_key"] == chosen["task_key"]]
    attempts = len(task_history)
    chosen = dict(chosen)
    chosen["attempt"] = attempts + 1
    chosen["attempts"] = attempts
    chosen["solved_before"] = any(item.get("correct") for item in task_history)
    dates = [str(item.get("date") or "") for item in task_history]
    chosen["last_attempt"] = max(dates) if dates else None
    chosen["suppressed_count"] = suppressed_count
    chosen["repeat_requested"] = bool(repeat)
    return chosen


def _task_explanation_ok(payload: dict[str, Any], text: Any) -> bool:
    """True, если закешированное LLM-объяснение проходит проверку фактов."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        facttext.assert_facts(text, _task_explain_facts(payload))
    except facttext.FactCheckError:
        return False
    return True


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


def _task_payload(
    db: Database, user: str, date: str | None = None, repeat: bool = False
) -> dict[str, Any]:
    """JSON для /api/task/today: позиция до хода, решение и объяснение.

    ``repeat=True`` — явный запрос повтора: окно подавления решённых позиций
    игнорируется. Попытки отслеживаются per-position (``task_key``):
    ``attempt`` — номер предстоящей попытки (1 = позиция видна впервые),
    ``attempts`` — сколько попыток уже было, ``suppressed_count`` — сколько
    позиций пула скрыто окном K дней.
    """
    day = date or _today()
    item = _pick_daily_task(db, user, day, repeat=repeat)
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
    if explanation is not None and (
        _is_bad_explanation(explanation)
        or not _task_explanation_ok(
            {
                "san_played": played_san,
                "best_san": best,
                "best_line": [str(s) for s in (move.get("best_line") or [])],
                "win_before": move.get("win_before"),
                "win_after": move.get("win_after"),
                "drop": move.get("drop"),
                "ply": ply,
                "opening": game.opening,
                "eco": game.eco,
                "opponent": game.opponent,
            },
            explanation,
        )
    ):
        # Отравленный кэш (мусор или текст без опоры на факты):
        # игнорируем, кнопка «Объяснить» снова доступна, успешный повтор
        # перезапишет мусор через UPSERT save_task_explanation.
        # Строки из БД не удаляем.
        explanation = None
    result = {
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
        "attempt": item.get("attempt", 1),
        "attempts": item.get("attempts", 0),
        "solved_before": bool(item.get("solved_before")),
        "last_attempt": item.get("last_attempt"),
        "suppressed_count": int(item.get("suppressed_count") or 0),
        "repeat_requested": bool(item.get("repeat_requested")),
        "repeat_window_days": _task_repeat_window_days(),
    }
    # Детерминированная версия для UI: не зависит от качества и наличия LLM.
    result["explanation_facts"] = facttext.task_text(result)
    return result


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
    каждый вход не будет заново ждать таймаут.
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
        # Аналитика активности считается напрямую из таблиц за окно, а не из
        # кеша daily_log (его минуты/анализы раньше обновлял скрытый ping,
        # который удалён). Окно: последние ``count`` дней до end_day включительно.
        try:
            start_day = (
                datetime.date.fromisoformat(str(end_day))
                - datetime.timedelta(days=count - 1)
            ).isoformat()
            start_ms = timeutil.day_bounds_ms(start_day)[0]
            end_ms = timeutil.day_bounds_ms(str(end_day))[1]
        except (TypeError, ValueError):
            start_ms, end_ms = None, None
        window_games = 0
        window_analyses = 0
        if start_ms is not None:
            with db._connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM games "
                    "WHERE LOWER(player) = LOWER(?) "
                    "AND created_at >= ? AND created_at < ?",
                    (nick, start_ms, end_ms),
                ).fetchone()
                window_games = int(row["n"] or 0) if row else 0
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM analyses a "
                    "JOIN games g ON g.id = a.game_id "
                    "WHERE LOWER(g.player) = LOWER(?) "
                    "AND a.analyzed_at >= ? AND a.analyzed_at < ?",
                    (nick, start_ms, end_ms),
                ).fetchone()
                window_analyses = int(row["n"] or 0) if row else 0
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
        # Активность ≠ обучение: это сырые сигналы вовлечённости. Старые
        # ключи improved_* сохранены как deprecated-алиасы тех же значений —
        # но больше не называются «улучшением» и не читаются как рост игрока.
        active_rating = rating_delta is not None and rating_delta >= 30
        active_completion = completion_pct >= 60.0 and days_active >= 10
        active = active_rating or active_completion
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
                "games_analyzed": window_games,
                "analyses": window_analyses,
                "last_seen": _last_seen_iso(record.get("last_seen_at")),
                # Новые имена: активность/вовлечённость, не «улучшение».
                "active": active,
                "active_rating": active_rating,
                "active_completion": active_completion,
                # Deprecated aliases (deprecated, kept for compatibility):
                # те же значения, но это НЕ измерение обучения игрока.
                "improved": active,
                "improved_rating": active_rating,
                "improved_activity": active_completion,
            }
        )

    return {
        "days": count,
        "users": rows,
        # Оговорка: когорта показывает вовлечённость и сырой Δ рейтинга,
        # а не эффективность плана и не «обученность» игроков.
        "note": (
            "active_* — сигналы активности (Δ рейтинга >= 30 или >= 60% "
            "плана за >= 10 дней), не доказательство обучения/роста. "
            "improved_* — устаревшие алиасы active_*, не измерение прогресса."
        ),
        "deprecated_keys": ["improved", "improved_rating", "improved_activity"],
        "summary": {
            "users": len(rows),
            "active_users": sum(1 for row in rows if row["days_active"] > 0),
            "active": sum(1 for row in rows if row["active"]),
            "active_by_rating": sum(
                1 for row in rows if row["active_rating"]
            ),
            "active_by_completion": sum(
                1 for row in rows if row["active_completion"]
            ),
            # Deprecated aliases (см. note): не использовать в новых отчётах.
            "improved": sum(1 for row in rows if row["active"]),
            "improved_by_rating": sum(
                1 for row in rows if row["active_rating"]
            ),
            "improved_by_activity": sum(
                1 for row in rows if row["active_completion"]
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
    "active",
    "active_rating",
    "active_completion",
    # Deprecated aliases (см. note в /api/admin/cohort): не обучение.
    "improved",
    "improved_rating",
    "improved_activity",
    "last_seen",
)


def _delete_user_artifacts(data_dir: Path, nick: str) -> None:
    """Удаляет data/<ник>/ после удаления профиля.

    Только внутри data_dir: защита от path traversal и от удаления корня.
    """
    target = user_dir(nick, root=data_dir)
    root = data_dir.resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return
    if resolved == root or root not in resolved.parents:
        return
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)


def _csv_safe(value: Any) -> Any:
    """Не даём Excel/LibreOffice исполнить ник как формулу."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _cohort_csv(rows: list[dict[str, Any]]) -> str:
    """CSV когорты (RFC 4180, UTF-8, перевод строки ``\\n``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_COHORT_CSV_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                _csv_safe(
                    int(row[column])
                    if isinstance(row.get(column), bool)
                    else ("" if row.get(column) is None else row.get(column))
                )
                for column in _COHORT_CSV_COLUMNS
            ]
        )
    return buffer.getvalue()


def _fmt_win(value: Any) -> str:
    """Оформление win% для промпта (один знак, '—' для None)."""
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.1f}"


def _task_explain_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """Facts-объект для объяснения задачи (для ``assert_facts``)."""
    sans = [payload.get("san_played"), payload.get("best_san")]
    sans += list(payload.get("best_line") or [])
    win_before = payload.get("win_before")
    win_after = payload.get("win_after")
    drop = payload.get("drop")

    def _num(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    numbers = [v for v in (_num(win_before), _num(win_after), _num(drop)) if v is not None]
    ply = payload.get("ply")
    max_ply = int(ply) if isinstance(ply, int) and ply > 0 else None
    opening = payload.get("opening") or None
    eco = payload.get("eco") or None
    strings = [str(value) for value in (opening, eco, payload.get("opponent")) if value]
    return facttext.new_facts(
        sans=[s for s in sans if s],
        numbers=numbers,
        openings=[opening] if opening else (),
        ecos=[eco] if eco else (),
        strings=strings,
        max_ply=max_ply,
    )


def _violations_hint(violations: list[str]) -> str:
    joined = "; ".join(violations) or "нет опоры на факты"
    return (
        "\nПредыдущий ответ содержал факты без опоры в данных: "
        + joined
        + ". Перепиши так, чтобы остались ТОЛЬКО факты из данных выше."
    )


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
    """LLM-объяснение задачи с проверкой фактов (не более 2 попыток).

    thinking-блоки вырезаются, thinking-разбор/safety-заглушка/огрызок
    вместо ответа — это ошибка. Ответ дополнительно проверяется
    детерминированным ``assert_facts``; при нарушении — одна повторная
    попытка с подсказкой, затем RuntimeError (вызывающий код покажет
    facts-only разбор вместо непроверенного текста модели).
    """
    llm = LLMClient()
    messages = _task_explain_messages(payload)
    facts = _task_explain_facts(payload)
    last_violations: list[str] = []
    for attempt in range(2):
        try:
            text = clean_llm_text(llm.chat(messages, temperature=0.4))
        except RuntimeError as exc:
            if attempt:
                raise
            messages = [
                messages[0],
                ChatMessage(
                    "user",
                    messages[1].content + "\nБез разбора. Сразу объяснение по-русски.",
                ),
            ]
            continue
        if not text or _is_bad_explanation(text):
            messages = [
                messages[0],
                ChatMessage(
                    "user",
                    messages[1].content + "\nБез разбора. Сразу объяснение по-русски.",
                ),
            ]
            continue
        try:
            facttext.assert_facts(text, facts)
        except facttext.FactCheckError as exc:
            last_violations = list(dict.fromkeys(exc.violations))
            messages = [
                messages[0],
                ChatMessage(
                    "user",
                    messages[1].content + _violations_hint(last_violations),
                ),
            ]
            continue
        return text
    detail = f" (нарушения: {'; '.join(last_violations)})" if last_violations else ""
    raise RuntimeError(f"Модель не дала проверяемое объяснение задачи{detail}")

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
_PLAN_FOCUS_PREP = {
    "opening": "дебюте",
    "middlegame": "миттельшпиле",
    "endgame": "эндшпиле",
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


#: Минимум ошибок в фазе, чтобы фаза могла стать фокусом недели
#: (audit M4): меньше — max по avg_drop это шум, а не сигнал.
_MIN_PHASE_ERRORS = 3

#: Окно для подсчёта дрелей (audit M5): последние N дней по created_at.
_DRILLS_WINDOW_DAYS = 14


def _resolve_week_focus(
    goal: dict[str, Any], plan: dict[str, Any] | None
) -> tuple[str, str]:
    """Фаза фокуса недели + человеческое обоснование с цифрами.

    Явный фокус цели побеждает; иначе — слабейшая фаза по avg_drop среди
    фаз, где набралось хотя бы ``_MIN_PHASE_ERRORS`` ошибок. Если таких
    фаз нет — fallback «middlegame» с честным обоснованием «мало данных»,
    а не шумный максимум одной-двух ошибок.
    """
    phases = _week_phases(plan)
    by_phase = {name: stats for name, stats in phases}
    wanted = goal.get("focus") or "auto"
    if wanted != "auto":
        stats = by_phase.get(wanted)
        if stats:
            count = int(stats.get("count") or 0)
            avg = float(stats.get("avg_drop") or 0.0)
            if count < _MIN_PHASE_ERRORS:
                return (
                    wanted,
                    f"твой выбор; всего {count} ошибок за окно — мало данных, "
                    f"вывод осторожный (средняя потеря {avg:.1f}%)",
                )
            return wanted, (
                f"твой выбор; {count} ошибок, средняя потеря {avg:.1f}% "
                "за окно последних проанализированных партий"
            )
        return wanted, "выбран тобой в цели (данных по фазе пока нет)"
    qualified = [
        (name, stats)
        for name, stats in phases
        if int((stats or {}).get("count") or 0) >= _MIN_PHASE_ERRORS
    ]
    if qualified:
        name, stats = max(
            qualified, key=lambda item: float((item[1] or {}).get("avg_drop") or 0.0)
        )
        count = int(stats.get("count") or 0)
        avg = float(stats.get("avg_drop") or 0.0)
        return (
            name,
            f"{count} ошибок (мин. {_MIN_PHASE_ERRORS} для выбора), "
            f"средняя потеря {avg:.1f}% — за окно последних проанализированных партий",
        )
    return (
        "middlegame",
        f"мало данных: ни в одной фазе не набралось {_MIN_PHASE_ERRORS} ошибок — "
        "фокус по умолчанию, собираем базу",
    )


def _week_drills_portion(unnatural: int) -> int:
    """Дневная порция дрелей: ``ceil(unnatural / 7)``, cap 20, без пола.

    ``unnatural`` — неестественные промахи за последние
    ``_DRILLS_WINDOW_DAYS`` дней (не all-time счётчик). При нуле
    доказательств — ноль дрелей, а не искусственная пятёрка (audit M5).
    """
    n = max(0, int(unnatural or 0))
    if n <= 0:
        return 0
    return min(20, (n + 6) // 7)


def _weak_opening_rows(
    plan: dict[str, Any] | None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Слабые дебюты из плана, прошедшие единый предикат (audit M4)."""
    if not isinstance(plan, dict):
        return []
    raw = (plan.get("repertoire") or {}).get("weak_openings")
    rows = [row for row in (raw or []) if is_weak_opening(row)]
    return rows[:limit] if limit is not None else rows


def _build_week_tasks(
    minutes: int,
    unnatural: int,
    weak_opening: dict[str, Any] | None,
    focus_phase: str,
    focus_reason: str,
    drills_window: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Детерминированный список задач на неделю.

    Каждая задача несёт ``est_minutes`` (на день). Сумма оценок по
    активным задачам не превышает ``floor(minutes * 0.8)``: при нехватке
    бюджета оценки урезаются консервативно (never overestimate), начиная
    с дрелей. Если за окно в ``_DRILLS_WINDOW_DAYS`` дней неестественных
    промахов ноль — задача «дрели» не назначается вовсе (audit M5).
    """
    portion = _week_drills_portion(unnatural)
    phase_ru = _PLAN_FOCUS_RU.get(focus_phase, focus_phase)
    phase_prep = _PLAN_FOCUS_PREP.get(focus_phase, phase_ru)

    window = drills_window if isinstance(drills_window, dict) else {}
    window_days = int(window.get("days") or _DRILLS_WINDOW_DAYS)
    window_games = window.get("games")
    window_bad = window.get("total_bad")
    games_txt = (
        f"{int(window_games)} партий в окне" if isinstance(window_games, int) else "окно последних проанализированных партий"
    )

    # Сырые оценки (на день), затем консервативная подгонка под бюджет.
    opening_est = 4   # ~30 мин в неделю: разбор 2 партий
    phase_est = 5     # ~35 мин в неделю: разбор 3 партий
    daily_est = 5     # задача дня
    drills_est = 2 * portion
    budget = max(0, int(minutes) * 8 // 10)

    estimates: dict[str, int] = {}
    remaining = budget
    for key, raw_est in (
        ("daily-task", daily_est),
        ("drills", drills_est),
        ("opening", opening_est),
        ("phase", phase_est),
    ):
        if key == "drills" and portion <= 0:
            continue
        est = min(raw_est, remaining)
        estimates[key] = est
        remaining -= est

    tasks: list[dict[str, Any]] = [
        {
            "key": "daily-task",
            "text": "Решай задачу дня каждый день (7 из 7)",
            "detail": "задача дня ×7: ни дня без тактики",
            "est_minutes": estimates["daily-task"],
        },
    ]
    if portion > 0:
        detail = (
            f"{unnatural} неестественных промахов за последние {window_days} дней "
            f"({games_txt}) — по {portion} в день"
        )
        if isinstance(window_bad, int) and window_bad > 0:
            detail += f" из {window_bad} плохих ходов в окне"
        tasks.append(
            {
                "key": "drills",
                "text": f"Реши {portion} дрелей в день",
                "detail": detail,
                "est_minutes": estimates["drills"],
            }
        )
    if weak_opening:
        name = str(weak_opening.get("opening") or "—")
        epg = weak_opening.get("errors_per_game_shrunk", weak_opening.get("errors_per_game"))
        epg_txt = f"{float(epg):.2f}" if isinstance(epg, (int, float)) else "—"
        opening_detail = (
            f"сжатая оценка: {epg_txt} ошибок на партию "
            f"(приор k=5, {int(weak_opening.get('games') or 0)} партий)"
        )
        tasks.append(
            {
                "key": "opening",
                "text": f"Разобрать 2 партии в дебюте «{name}»",
                "detail": opening_detail,
                "est_minutes": estimates["opening"],
            }
        )
    else:
        tasks.append(
            {
                "key": "opening",
                "text": "Дебютный фокус не выбран",
                "detail": "нет дебюта с достаточной выборкой и плохой формой — данных для рекомендации нет",
                "est_minutes": estimates["opening"],
            }
        )
    tasks.append(
        {
            "key": "phase",
            "text": f"Разобрать 3 партии с ошибками в {phase_prep}",
            "detail": f"фокус недели: {phase_ru} ({focus_reason})",
            "est_minutes": estimates["phase"],
        }
    )
    return tasks


def _build_human_summary(
    focus_phase: str,
    phases: list[tuple[str, dict[str, Any]]],
    weak_opening: dict[str, Any] | None,
    drills_window: dict[str, Any] | None,
    drills_today: int,
) -> str:
    """Человеческий итог недели шаблонами, без LLM (3–5 предложений).

    Про неестественные промахи говорится по окну последних
    ``_DRILLS_WINDOW_DAYS`` дней (не all-time), с числом партий/плохих
    ходов и честным «нет данных» при пустом окне.
    """
    phase_ru = _PLAN_FOCUS_RU.get(focus_phase, focus_phase)
    parts: list[str] = []
    stats = dict(phases).get(focus_phase) if phases else None
    if stats and int(stats.get("count") or 0) >= _MIN_PHASE_ERRORS:
        count = int(stats.get("count") or 0)
        avg = float(stats.get("avg_drop") or 0.0)
        parts.append(
            f"Главная дыра — {phase_ru}: {count} ошибок за окно последних "
            f"проанализированных партий, средняя потеря {avg:.1f}%."
        )
    else:
        parts.append(f"На неделе качаем {phase_ru}: данных за окно мало, собираем базу.")
    if weak_opening:
        name = str(weak_opening.get("opening") or "—")
        epg = weak_opening.get("errors_per_game_shrunk", weak_opening.get("errors_per_game"))
        epg_txt = f"{float(epg):.2f}" if isinstance(epg, (int, float)) else "—"
        games_n = int(weak_opening.get("games") or 0)
        parts.append(
            f"Слабый дебют — {name}: {epg_txt} ошибок на партию "
            f"(сжатая оценка, {games_n} партий)."
        )
    window = drills_window if isinstance(drills_window, dict) else {}
    window_days = int(window.get("days") or _DRILLS_WINDOW_DAYS)
    window_games = int(window.get("games") or 0)
    unnatural = int(window.get("unnatural") or 0)
    total_bad = int(window.get("total_bad") or 0)
    if total_bad <= 0:
        if window.get("source") == "humanity":
            parts.append(
                f"За последние {window_days} дней плохих ходов в окне ({window_games} партий) "
                "не найдено — выводов о человечности не делаем."
            )
        else:
            parts.append(
                f"Нет данных для оценки неестественности за последние {window_days} дней "
                "(нужна оценка ошибок) — выводов не делаем."
            )
    else:
        share = round(unnatural / total_bad * 100, 1)
        if share >= 40.0:
            parts.append(
                f"Много случайных ходов за последние {window_days} дней — "
                f"{share}% неестественных из {total_bad} ({window_games} партий в окне): "
                "играй медленнее, считай до конца."
            )
        else:
            parts.append(
                f"За последние {window_days} дней ошибки в основном естественные "
                f"({share}% неестественных из {total_bad}, {window_games} партий в окне) — "
                "работаем над счётом."
            )
    if drills_today > 0:
        parts.append(
            f"Вывод на неделю: качаем {phase_ru} — {drills_today} дрелей в день "
            f"(по окну {window_days} дней) плюс разбор партий из списка выше."
        )
    else:
        parts.append(
            f"Вывод на неделю: качаем {phase_ru}; дрелей не назначаю — "
            f"за последние {window_days} дней неестественных промахов не найдено."
        )
    return " ".join(parts)


def _move_label(ply: int, san: str) -> str:
    """``0`` + e4 -> ``1.e4``; ``1`` + e5 -> ``1...e5``."""
    try:
        n = int(ply)
    except (TypeError, ValueError):
        n = 0
    return f"{n // 2 + 1}{'.' if n % 2 == 0 else '...'}{san}"


def _week_evidence(
    db: Database, nick: str, focus_phase: str, weak_opening: str | None = None
) -> dict[str, Any]:
    """Evidence for the weekly focus: % of games + the worst concrete move.

    The plan must answer “в 60% партий ошибка в эндшпиле” and point to one
    game + move, instead of only an aggregate. Uses the same cached analyses
    as the plan; no engine/network.
    """
    try:
        pairs = shared_data.repertoire_pairs(db, nick)
    except RuntimeError:
        pairs = []
    internal = {v: k for k, v in _INTERNAL_TO_PUBLIC_PHASE.items()}.get(
        focus_phase, focus_phase
    )
    games_total = len(pairs)
    games_with_error = 0
    worst: dict[str, Any] | None = None
    opening_worst: dict[str, Any] | None = None
    for game, analysis in pairs:
        records = analyze_user_moves(game, analysis)
        phase_records = [item for item in records if item.get("phase") == internal]
        if phase_records:
            games_with_error += 1
        for item in phase_records:
            drop = item.get("drop")
            score = float(drop) if isinstance(drop, (int, float)) else 0.0
            if worst is None or score > float(worst.get("_score", 0.0)):
                worst = {
                    "game_id": game.id,
                    "link": game.link,
                    "ply": int(item.get("ply") or 0),
                    "san": str(item.get("san") or "—"),
                    "move_label": _move_label(
                        int(item.get("ply") or 0), str(item.get("san") or "—")
                    ),
                    "classification": item.get("classification"),
                    "drop": round(score, 1),
                    "phase": focus_phase,
                    "opening": game.opening,
                    "opponent": game.opponent,
                    "_score": score,
                }
        if weak_opening:
            for item in records:
                if str(game.opening or "").strip().lower() != str(weak_opening).strip().lower():
                    continue
                drop = item.get("drop")
                score = float(drop) if isinstance(drop, (int, float)) else 0.0
                if opening_worst is None or score > float(opening_worst.get("_score", 0.0)):
                    opening_worst = {
                        "game_id": game.id,
                        "link": game.link,
                        "ply": int(item.get("ply") or 0),
                        "san": str(item.get("san") or "—"),
                        "move_label": _move_label(
                            int(item.get("ply") or 0), str(item.get("san") or "—")
                        ),
                        "classification": item.get("classification"),
                        "drop": round(score, 1),
                        "opening": game.opening,
                        "opponent": game.opponent,
                        "_score": score,
                    }
    if worst is not None:
        worst.pop("_score", None)
    if opening_worst is not None:
        opening_worst.pop("_score", None)
    return {
        "phase": focus_phase,
        "games_total": games_total,
        "games_with_error": games_with_error,
        "share_pct": round(games_with_error * 100.0 / games_total, 1)
        if games_total
        else None,
        "worst": worst,
        "opening_worst": opening_worst,
    }


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

    weak_opening = None
    weak_openings = _weak_opening_rows(plan, limit=1)
    if weak_openings:
        weak_opening = weak_openings[0]

    drills_total = 0
    if isinstance(plan, dict):
        drills = plan.get("drills") or {}
        try:
            drills_total = int(drills.get("total") or 0)
        except (TypeError, ValueError):
            drills_total = 0
    # Дрели — по окну последних _DRILLS_WINDOW_DAYS дней, не all-time.
    try:
        drills_window = shared_data.recent_drills_unnatural(
            db, nick, days=_DRILLS_WINDOW_DAYS, root=data_dir
        )
    except Exception:
        drills_window = {
            "days": _DRILLS_WINDOW_DAYS,
            "games": 0,
            "unnatural": 0,
            "total_bad": 0,
            "source": "no-humanity-items",
        }
    unnatural = int(drills_window.get("unnatural") or 0)
    drills_today = _week_drills_portion(unnatural)

    try:
        task = _task_payload(db, nick)
        task_done: bool | None = bool(task.get("done"))
    except RuntimeError:
        task_done = None

    day = _today()
    try:
        manual_sec = _manual_seconds(db, _lichess_manual_key(day, nick))
        games = db.get_games(nick, limit=100_000)
        lichess_sec = 0.0
        for game in games:
            if timeutil.date_of_ms(game.created_at) != day:
                continue
            lichess_sec += _clock_seconds(game.clocks)
        time_sec = int(lichess_sec + manual_sec)
    except (ValueError, TypeError, OSError):
        time_sec = 0

    weak_opening_name = (
        str((weak_opening or {}).get("opening") or "").strip() or None
    )
    evidence = _week_evidence(db, nick, focus_phase, weak_opening_name)

    # Планируемый бюджет: 0.8 от минуты пользователя — не больше.
    week_tasks = _build_week_tasks(
        minutes, unnatural, weak_opening, focus_phase, focus_reason, drills_window
    )

    return {
        "goal": goal["goal"],
        "minutes_per_day": minutes,
        "focus": {"phase": focus_phase, "reason": focus_reason},
        "evidence": evidence,
        "week_tasks": week_tasks,
        "week_budget_minutes": int(minutes) * 8 // 10,
        "today": {
            "task_done": task_done,
            "time_sec": time_sec,
            "drills_today": drills_today,
            "drills_total": drills_total,
            # Честная метка окна: откуда взяты дрели (audit M5/M8).
            "drills_window": {
                "days": int(drills_window.get("days") or _DRILLS_WINDOW_DAYS),
                "games": int(drills_window.get("games") or 0),
                "unnatural": unnatural,
                "total_bad": int(drills_window.get("total_bad") or 0),
                "source": str(drills_window.get("source") or "no-humanity-items"),
            },
        },
        "human_summary": _build_human_summary(
            focus_phase, phases, weak_opening, drills_window, drills_today
        ),
    }

# --- Снимок недельного плана (описательная петля, audit C3) --------------

def _week_snapshot_payload(
    db: Database, nick: str, data_dir: Any
) -> dict[str, Any] | None:
    """Агрегаты окна на момент сохранения плана (или None — снимать нечего).

    Источники те же, что у /api/plan/week; наружу уходят только счётчики и
    названия (слабые дебюты, мотивы), никаких ходов, соперников и PII.
    """
    try:
        pairs = shared_data.repertoire_pairs(db, nick)
    except RuntimeError:
        return None
    summary = summarize_recent(pairs)
    plan: dict[str, Any] | None = None
    try:
        plan = shared_data.plan(db, nick, root=data_dir)
    except RuntimeError:
        plan = None
    goal = _load_plan_goal(db, nick)
    focus_phase, _ = _resolve_week_focus(goal, plan)

    try:
        # drills_target — дневная норма из свежего окна, как в /api/plan/week.
        unnatural = int(
            shared_data.recent_drills_unnatural(
                db, nick, days=_DRILLS_WINDOW_DAYS, root=data_dir
            ).get("unnatural")
            or 0
        )
    except Exception:
        unnatural = 0

    weak_openings: list[dict[str, Any]] = []
    motifs: list[dict[str, Any]] = []
    if isinstance(plan, dict):
        # Снимок фиксирует только подтверждённо слабые дебюты (audit M4).
        for item in _weak_opening_rows(plan, limit=3):
            try:
                games_n = int(item.get("games") or 0)
            except (TypeError, ValueError):
                games_n = 0
            weak_openings.append(
                {
                    "opening": str(item.get("opening") or "—"),
                    "eco": item.get("eco"),
                    "games": games_n,
                    "errors_per_game": item.get("errors_per_game"),
                    "points_pct": item.get("points_pct"),
                }
            )
        for entry in (plan.get("patterns") or {}).get("motifs") or []:
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                continue
            name, stats = entry
            stats = stats if isinstance(stats, dict) else {}
            try:
                count_n = int(stats.get("count") or 0)
            except (TypeError, ValueError):
                count_n = 0
            motifs.append(
                {
                    "motif": str(name),
                    "count": count_n,
                    "avg_drop": stats.get("avg_drop"),
                }
            )
            if len(motifs) >= 3:
                break
    return {
        "window_n": summary["window_n"],
        "focus_phase": focus_phase,
        # drills_target — дневная норма из плана недели (как «Реши N дрелей
        # в день»), не недельный итог.
        "drills_target": _week_drills_portion(unnatural),
        "weak_openings": weak_openings,
        "motifs": motifs,
        "avg_drop": summary["avg_drop"],
        "score": summary["score_pct"],
    }


def _save_week_snapshot(db: Database, nick: str, data_dir: Any) -> dict[str, Any] | None:
    """Best-effort снимок при сохранении/генерации плана недели.

    Вызывать только из явных действий пользователя (сохранение цели,
    генерация комментария), НЕ из аналитики. Ошибка сохранения снимка не
    роняет запрос: логируем и продолжаем. Прошлая неделя не перезаписывается
    (см. Database.save_plan_snapshot).
    """
    try:
        payload = _week_snapshot_payload(db, nick, data_dir)
        if payload is None:
            return None
        day = _today()
        try:
            week_start = monday_of_week(datetime.date.fromisoformat(day))
        except ValueError:
            week_start = day
        return db.save_plan_snapshot(
            nick, week_start, version=SNAPSHOT_VERSION, **payload
        )
    except Exception:
        logger.warning("week plan snapshot failed", exc_info=True)
        return None


def _snapshot_public(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """Снимок для ответа API: + читаемая ISO-дата, без лишнего."""
    if not snap:
        return None
    out = dict(snap)
    created_ms = int(out.get("created_at") or 0)
    created_iso: str | None = None
    if created_ms > 0:
        try:
            created_iso = datetime.datetime.fromtimestamp(
                created_ms / 1000, tz=timeutil.timezone_for()
            ).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            created_iso = None
    out["created_iso"] = created_iso
    return out


def _week_snapshot_status(db: Database, nick: str) -> dict[str, Any]:
    """Снимок + описательные дельты «на сейчас» с жёсткими гейтами.

    Честные состояния: ``no_snapshot`` | ``pending`` (<7 дней) |
    ``insufficient_data`` (<10 новых анализов) | ``ok`` (сырые дельты).
    Партии не пересчитываются при каждом чтении аналитики — только здесь.
    """
    snap = db.get_latest_plan_snapshot(nick)
    if not snap:
        return {
            "status": "no_snapshot",
            "snapshot": None,
            "current": None,
            "deltas": None,
            "new_games": 0,
            "age_days": None,
            "days_remaining": None,
            "min_new_games": MIN_NEW_GAMES,
            "min_age_days": MIN_SNAPSHOT_AGE_DAYS,
            "caveat": SNAPSHOT_CAVEAT,
        }
    created_ms = int(snap.get("created_at") or 0)
    now_ms = int(time.time() * 1000)
    age_days = (now_ms - created_ms) / 86_400_000 if created_ms > 0 else None
    new_games = db.count_analyzed_games_since(nick, created_ms)
    window_n = max(1, int(snap.get("window_n") or 1))
    pairs = db.get_analyzed_games(nick, limit=window_n)
    current = summarize_recent(pairs)
    result = snapshot_delta(snap, current, new_games=new_games, age_days=age_days)
    result["snapshot"] = _snapshot_public(result["snapshot"])
    return result


# --- Индивидуальный LLM-план недели ---------------------------------------

_PLAN_LLM_SYSTEM = """Ты — шахматный тренер сильного любителя. Объясни, ПОЧЕМУ такой недельный план. Не составляй новый список задач и не повторяй ежедневную норму минут.
Отвечай 3–5 предложениями по-русски. Обязательно:
1) назови процент партий с ошибками в фокус-фазе и числа (сколько из скольких);
2) назови один конкретный пример: дебют/партия, ход, потеря win%;
3) свяжи это с фокусом и задачами недели.
Опирайся ТОЛЬКО на переданные данные. Если конкретного примера нет — скажи, что данных мало, а не выдумывай. Запрещены заголовки «Неделя 1», «Норма дня», списки минут и общие фразы вида «учи эндшпили».
Калибруй сложность под уровень: ниже 1400 допустима база; 1400–1800 — без детской базы; 1800 и выше — только продвинутый уровень. Для 1800+ запрещены оппозиция, король+пешка против короля и элементарная база."""



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
    # Последняя партия может быть без рейтинга (PGN/тренировка) — ищем
    # ближайшую назад партию с валидным рейтингом, иначе остаёмся без него.
    for game in sorted(games, key=lambda g: (g.created_at, g.id), reverse=True):
        rating = game.user_rating
        if isinstance(rating, bool) or not isinstance(rating, int) or rating <= 0:
            continue
        speed = game.speed
        if not isinstance(speed, str) or not speed.strip():
            speed = None
        else:
            speed = speed.strip()
        return {"rating": rating, "speed": speed}
    return {"rating": None, "speed": None}


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
    humanity = None
    score_pct = None
    avg_win_loss = None
    if plan is not None:
        for item in _weak_opening_rows(plan, limit=3):
            try:
                errors_n = float(
                    item.get(
                        "errors_per_game_shrunk", item.get("errors_per_game") or 0.0
                    )
                )
            except (TypeError, ValueError):
                errors_n = 0.0
            weak_openings.append(
                {
                    "opening": str(item.get("opening") or "—"),
                    "errors_per_game": round(errors_n, 2),
                }
            )
        drills = plan.get("drills") or {}
        try:
            drills_total = int(drills.get("total") or 0)
        except (TypeError, ValueError):
            drills_total = 0
        humanity = plan.get("humanity")
        if isinstance(plan.get("score_pct"), (int, float)):
            score_pct = round(float(plan["score_pct"]), 1)
        if isinstance(plan.get("avg_win_loss"), (int, float)):
            avg_win_loss = round(float(plan["avg_win_loss"]), 2)

    try:
        drills_window = shared_data.recent_drills_unnatural(
            db, nick, days=_DRILLS_WINDOW_DAYS, root=data_dir
        )
    except Exception:
        drills_window = {
            "days": _DRILLS_WINDOW_DAYS,
            "games": 0,
            "unnatural": 0,
            "total_bad": 0,
            "source": "no-humanity-items",
        }
    drills_unnatural = int(drills_window.get("unnatural") or 0)

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
        "evidence": _week_evidence(
            db,
            nick,
            focus_phase,
            str((weak_openings[0] or {}).get("opening") or "").strip()
            if weak_openings
            else None,
        ),
        "weak_openings": weak_openings,
        "unnatural_verdict": _plan_unnatural_verdict(humanity),
        "drills_total": drills_total,
        "drills_unnatural": drills_unnatural,
        "drills_window_days": int(drills_window.get("days") or _DRILLS_WINDOW_DAYS),
        "drills_window_games": int(drills_window.get("games") or 0),
        "score_pct": score_pct,
        "avg_win_loss": avg_win_loss,
        "week_tasks": _build_week_tasks(
            minutes,
            drills_unnatural,
            weak_openings[0] if weak_openings else None,
            focus_phase,
            focus_reason,
            drills_window,
        ),
        "prompt_version": 2,
    }


def _evidence_line(evidence: Any) -> str:
    """Одна строка факта для LLM: % партий + конкретный худший ход."""
    if not isinstance(evidence, dict) or not evidence.get("games_total"):
        return "Пример: нет данных для конкретного примера."
    share = evidence.get("share_pct")
    total = evidence.get("games_total")
    with_err = evidence.get("games_with_error")
    if share is None:
        return "Пример: нет данных для конкретного примера."
    line = (
        f"Фаза {evidence.get('phase')}: ошибки в {share}% партий "
        f"({with_err} из {total})."
    )
    worst = evidence.get("worst")
    if isinstance(worst, dict):
        line += (
            f" Самый явный пример: партия {worst.get('game_id')} "
            f"({worst.get('opening') or 'без дебюта'}), ход "
            f"{worst.get('move_label') or worst.get('san')}, "
            f"потеря {worst.get('drop')}%."
        )
    return line


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
        _evidence_line(entry.get("evidence")),
        "Задачи недели: " + "; ".join(
            str(t.get("text") or "")
            for t in (entry.get("week_tasks") or [])
            if isinstance(t, dict) and t.get("text")
        ) + ".",
        f"Человечность: {entry.get('unnatural_verdict')}.",
        f"Дрели: всего в кеше {entry.get('drills_total')}; неестественных за последние {entry.get('drills_window_days') or _DRILLS_WINDOW_DAYS} дней ({entry.get('drills_window_games') or 0} партий в окне): {entry.get('drills_unnatural')}.",
        f"Счёт: {score if score is not None else '—'}%; средняя потеря: {loss if loss is not None else '—'}%.",
        "Ответ — только объяснение, почему такой план, без нового списка задач.",
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


def _plan_llm_facts(entry: dict[str, Any]) -> dict[str, Any]:
    """Facts-объект для комментария к недельному плану (assert_facts)."""
    numbers: list[float] = []
    for key in (
        "minutes",
        "drills_total",
        "drills_unnatural",
        "drills_window_days",
        "drills_window_games",
        "score_pct",
        "avg_win_loss",
        "rating",
        "target_rating",
        "weeks_left",
    ):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numbers.append(float(value))

    weak = entry.get("weak_openings") or []
    weak_names: list[str] = []
    for item in weak:
        if not isinstance(item, dict):
            continue
        name = item.get("opening")
        if name:
            weak_names.append(str(name))
        errors = item.get("errors_per_game")
        if isinstance(errors, (int, float)) and not isinstance(errors, bool):
            numbers.append(float(errors))

    evidence = entry.get("evidence") or {}
    if isinstance(evidence, dict):
        for key in ("games_total", "games_with_error", "share_pct"):
            value = evidence.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers.append(float(value))
        for key in ("worst", "opening_worst"):
            item = evidence.get(key)
            if not isinstance(item, dict):
                continue
            drop = item.get("drop")
            if isinstance(drop, (int, float)) and not isinstance(drop, bool):
                numbers.append(float(drop))

    sans: list[str] = []
    openings = list(weak_names)
    for key in ("worst", "opening_worst"):
        item = evidence.get(key) if isinstance(evidence, dict) else None
        if isinstance(item, dict):
            for san_key in ("san", "move_label"):
                san = item.get(san_key)
                if san:
                    sans.append(str(san))
            opening = item.get("opening")
            if opening:
                openings.append(str(opening))

    goal = entry.get("goal")
    strings = [str(goal)] if goal else []
    strings += weak_names
    if isinstance(evidence, dict):
        for key in ("worst", "opening_worst"):
            item = evidence.get(key)
            if isinstance(item, dict):
                for text_key in ("game_id", "move_label", "opening"):
                    value = item.get(text_key)
                    if value:
                        strings.append(str(value))

    return facttext.new_facts(
        sans=sans,
        numbers=numbers,
        openings=openings,
        strings=strings,
    )


def _plan_llm_facts_brief(entry: dict[str, Any]) -> str:
    """Факты-only комментарий к плану: те же строки-факты, без LLM."""
    lines = [
        "LLM недоступен или ответ не прошёл проверку фактов — ниже только",
        "проверяемые данные недели (без комментария модели):",
        _evidence_line(entry.get("evidence")),
    ]
    weak = entry.get("weak_openings") or []
    weak_txt = "; ".join(
        f"{w.get('opening')} ({w.get('errors_per_game')} ошибок/партию)"
        for w in weak
        if isinstance(w, dict) and w.get("opening")
    )
    if weak_txt:
        lines.append(f"Топ слабых дебютов: {weak_txt}.")
    loss = entry.get("avg_win_loss")
    score = entry.get("score_pct")
    lines.append(
        f"Счёт: {score if score is not None else '—'}%; "
        f"средняя потеря win%: {loss if loss is not None else '—'}."
    )
    return "\n".join(lines)


def _plan_llm_chat(
    messages: list[ChatMessage],
    advanced: bool = False,
    facts: dict[str, Any] | None = None,
    *,
    brief: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """LLM-план недели с проверкой фактов (не более 2 попыток).

    thinking-блоки вырезаются, служебный разбор и детсад для 1800+ — брак.
    Ответ дополнительно проверяется детерминированным ``assert_facts``; при
    нарушении — одна повторная попытка с подсказкой, затем ``brief``
    (facts-only) вместо непроверенного текста модели. Возвращает
    ``(текст, meta)``; ``meta["source"]`` — ``"llm"`` | ``"facts"``.
    """
    meta: dict[str, Any] = {"source": "llm", "violations": [], "error": None}
    llm = LLMClient()
    messages_now = list(messages)
    text = ""
    try:
        for attempt in range(2):
            text = clean_llm_text(llm.chat(messages_now, temperature=0.4))
            violations: list[str] = []
            if not _plan_chat_ok(text, advanced):
                violations = ["служебный разбор или базовая теория вместо плана"]
            elif facts is not None:
                try:
                    facttext.assert_facts(text, facts)
                except facttext.FactCheckError as exc:
                    violations = list(dict.fromkeys(exc.violations))
            if not violations:
                return text, meta
            meta["violations"] = violations
            if attempt == 0:
                messages_now = [
                    messages_now[0],
                    ChatMessage(
                        "user",
                        messages_now[1].content + _violations_hint(violations),
                    ),
                ]
    except RuntimeError as exc:
        meta["error"] = str(exc)
        if brief:
            meta["source"] = "facts"
            return brief, meta
        raise
    if advanced and text and _plan_has_basic_stuff(text):
        meta["error"] = "LLM вернул базовые темы вместо продвинутого плана"
    elif not meta["error"]:
        meta["error"] = "ответ модели не прошёл проверку фактов"
    if brief:
        meta["source"] = "facts"
        return brief, meta
    raise RuntimeError(meta["error"])


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
            rating = params["input"].get("rating")
            advanced = (
                isinstance(rating, int)
                and not isinstance(rating, bool)
                and rating >= 1800
            )
            facts = _plan_llm_facts(params["input"])
            brief = _plan_llm_facts_brief(params["input"])
            try:
                _ensure_llm_key()
                text, meta = _plan_llm_chat(
                    messages, advanced=advanced, facts=facts, brief=brief
                )
            except Exception as exc:  # LLM недоступен — recoverable facts-only
                logger.info("plan llm: LLM недоступен, показываю факты: %s", exc)
                text, meta = brief, {"source": "facts", "violations": [], "error": str(exc)}
            if not text:
                raise RuntimeError("LLM вернул пустой план")
            facts_only = meta.get("source") != "llm"
            created = _today()
            record = {
                "text": text,
                "created_at": created,
                "input_hash": params["input_hash"],
                "weeks_left": params["input"].get("weeks_left"),
                "prompt_version": params["input"].get("prompt_version"),
                "facts_only": facts_only,
                "llm_error": meta.get("error"),
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
                    "facts_only": facts_only,
                    "llm_error": meta.get("error"),
                }
            }
            job.summary = (
                "Комментарий тренера (факты недели, без LLM)"
                if facts_only
                else "Комментарий тренера готов"
            )
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
        "input_hash": data.get("input_hash"),
        "prompt_version": data.get("prompt_version"),
        "facts_only": bool(data.get("facts_only")),
        "llm_error": data.get("llm_error"),
    }

# --- Мини-трекер времени ---------------------------------------------------

def _now() -> float:
    """Текущий unix-время (сек) — отдельная функция ради тестов (патчат)."""
    return time.time()


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
    app = FastAPI(title="Chess Trainer API", version="0.9.22", docs_url="/api/docs", redoc_url=None)
    app.state.db_path = Path(db_path) if db_path else settings.db_path
    app.state.data_dir = Path(data_dir) if data_dir else settings.data_dir

    # ADMIN_NICKS — seed только для существующих профилей. Проверка прав
    # (is_admin) смотрит исключительно users.role, не env.
    with Database(app.state.db_path) as db:
        db.init_db()
        db.bootstrap_admin_nicks(
            settings.admin_nicks, settings.admin_bootstrap_password
        )

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
        # Список всех игроков — только админу. Свой профиль клиент берёт
        # из /api/user/current.
        _admin_user(request, app.state.db_path)
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
        nick = _session_nick(request, db)
        if not nick:
            raise HTTPException(401, "Нужен вход")
        if not is_admin(db, nick):
            raise HTTPException(403, "Доступ только для админа")
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
        nick = _session_nick(request, db)
        if not nick:
            raise HTTPException(401, "Нужен вход")
        if not is_admin(db, nick):
            raise HTTPException(403, "Доступ только для админа")
        db.set_current_user(clean)
        db.touch_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/delete")
    def api_user_delete(
        request: Request,
        nick: str = Form(...),
        password: str = Form(""),
    ):
        """Удаляет профиль и его партии/анализ из БД и артефакты на диске.

        Свой аккаунт (в т.ч. для админа): если у профиля есть пароль,
        обязателен верный ``password`` — повторная авторизация перед
        необратимым удалением. Паролем не защищённые legacy-профили и
        удаление ЧУЖОГО профиля админом пароль не требуют (админ может
        не знать чужой пароль).
        """
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        db = Database(app.state.db_path)
        db.init_db()
        nick = _session_nick(request, db)
        if not nick:
            raise HTTPException(401, "Нужен вход")
        is_self = clean.lower() == nick.lower()
        if not is_self and not is_admin(db, nick):
            raise HTTPException(403, "Можно удалить только свой профиль")
        target = db.get_user_record(clean)
        if is_self and target and target.get("password_hash"):
            if not (password or ""):
                raise HTTPException(
                    400, "Пароль для подтверждения удаления"
                )
            if not verify_password(password, target["password_hash"]):
                raise HTTPException(401, "Неверный пароль")
        deleted = db.delete_user(clean)
        if deleted:
            _purge_user_jobs(clean)
            _delete_user_artifacts(app.state.data_dir, clean)
            # Чистим rate-limit счётчик логина удаляемого (хеш логина,
            # без самого ника; ключ мог остаться после неудачных входов).
            try:
                digest = hashlib.sha256(clean.lower().encode("utf-8")).hexdigest()
                with db._connection() as conn:
                    conn.execute(
                        "DELETE FROM meta WHERE key = ?",
                        (f"login_fail_id:{digest[:32]}",),
                    )
            except Exception:  # noqa: BLE001 — удаление профиля важнее чистки
                logger.debug("cannot purge login rate key for %s", clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/fide")
    def api_user_fide(
        request: Request,
        nick: str = Form(...),
        fide_id: str = Form(""),
    ):
        """Задаёт (или пустым значением очищает) FIDE ID профиля."""
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        value = fide_id.strip()
        with Database(app.state.db_path) as db:
            db.init_db()
            current = _session_nick(request, db)
            if not current:
                raise HTTPException(401, "Нужен вход")
            if clean.lower() != current.lower() and not is_admin(db, current):
                raise HTTPException(403, "Можно менять только свой профиль")
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

    @app.post("/api/report/refresh")
    def api_report_refresh(request: Request, body: ReportRefreshBody):
        """Переанализирует устаревшие анализы (нет разметки ``themes``).

        Безопасный путь для старых кешей: только явное подтверждение
        (``confirm=true``), партии текущего игрока, потолок ``limit`` за
        вызов. Анализ заменяется только при успешном переанализировании;
        при ошибке движка прежний успешный анализ остаётся нетронутым, а
        ошибка попадает в ответ. Партией, которая есть у другого игрока
        (общий кеш), не трогаем совсем. Старые данные не удаляются.
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        if not body.confirm:
            raise HTTPException(
                400,
                "Обновление нужно подтвердить явно: переанализ стоит Stockfish-времени "
                "(глубина 10, ~несколько секунд на партию). Старый анализ заменится "
                "только после успешного переанализирования. Повтори с confirm=true.",
            )
        try:
            limit = int(body.limit)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(20, limit))
        if _running_jobs_count() > 0:
            raise HTTPException(409, "Идёт фоновый анализ — дождись его окончания")
        with Database(app.state.db_path) as db:
            db.init_db()
            pairs = db.get_analyzed_games(user["nick"], limit=200)
            if body.game_ids is not None:
                wanted = {gid for gid in body.game_ids if isinstance(gid, str)}
                target_all = [(g, a) for g, a in pairs if g.id in wanted]
                found = {g.id for g, _ in target_all}
                not_found = sorted(wanted - found)
            else:
                target_all = [(g, a) for g, a in pairs if analysis_needs_refresh(a)]
                not_found = []
            truncated = max(0, len(target_all) - limit)
            target = target_all[:limit]
            refreshed: list[str] = []
            failed: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            for game, _analysis in target:
                if _analysis_has_foreign_copy(db, game.id, user["nick"]):
                    skipped.append(
                        {
                            "game_id": game.id,
                            "reason": "партия есть у другого игрока — общий кеш не перезаписываем",
                        }
                    )
                    continue
                if not game.moves:
                    skipped.append({"game_id": game.id, "reason": "в партии нет ходов"})
                    continue
                try:
                    with Engine(depth=10, multipv=1, time_ms=800) as engine:
                        summary = engine.analyze_game(game).summary()
                except Exception as exc:  # noqa: BLE001 — старая запись остаётся
                    logger.warning("Refresh analysis failed for %s: %s", game.id, exc)
                    failed.append({"game_id": game.id, "error": str(exc)})
                    continue
                try:
                    db.save_analysis(game.id, summary, depth=10)
                except Exception as exc:  # noqa: BLE001 — старая запись остаётся
                    logger.warning("Refresh save failed for %s: %s", game.id, exc)
                    failed.append(
                        {"game_id": game.id, "error": f"анализ не сохранён: {exc}"}
                    )
                    continue
                refreshed.append(game.id)
            after = db.get_analyzed_games(user["nick"], limit=200)
            stale_after = [
                g.id for g, a in after if analysis_needs_refresh(a)
            ]
        return {
            "requested": len(target),
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped,
            "not_found": not_found,
            "truncated": truncated,
            "needs_refresh": {
                "count": len(stale_after),
                "total": len(after),
                "game_ids": stale_after[:50],
            },
            "cost": (
                "Каждая партия заново проанализирована Stockfish "
                "(глубина 10, 1 линия): время пропорционально числу партий."
            ),
            "note": (
                "Старый анализ заменяется только после успешного переанализирования; "
                "при ошибке движка прежний кеш сохраняется без изменений. "
                "Данные не удаляются."
            ),
        }

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
            _save_week_snapshot(db, user["nick"], app.state.data_dir)
        return {"ok": True, **saved}

    @app.get("/api/plan/week")
    def api_plan_week(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            return _plan_week_payload(db, user["nick"], app.state.data_dir)

    @app.get("/api/plan/week/snapshot")
    def api_plan_week_snapshot(request: Request):
        """Снимок текущей недели + описательные дельты к текущему окну.

        Чистая аналитика чтения: снимок сюда не пишет. Гейты: <7 дней —
        pending, <10 новых анализов — insufficient_data, иначе сырые
        дельты со ссылкой на n и оговоркой про отсутствие причинности.
        """
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            return _week_snapshot_status(db, user["nick"])

    @app.get("/api/plan/llm")
    def api_plan_llm_get(request: Request):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            saved = _load_plan_llm(db, user["nick"])
        if saved is None:
            return {"text": None}
        if saved.get("prompt_version") != 2:
            # Старый комментарий после смены промпта не показываем: иначе UI
            # выдаёт устаревший «план» вместо объяснения.
            return {"text": None, "stale": True}
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
            if dry_run != "on":
                _save_week_snapshot(db, user["nick"], app.state.data_dir)
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
                    "facts_only": bool(saved.get("facts_only")),
                    "llm_error": saved.get("llm_error"),
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
        if not user:
            raise HTTPException(401, "Нужен вход")
        job = _get_job(job_id)
        if job is not None:
            # Чужую задачу (результаты LLM, данные соперника) не отдаём.
            if job.user != user["nick"]:
                raise HTTPException(404, "Job not found")
            return job.as_dict()

        # Job'а нет в памяти: возможно, процесс перезапустился. Пробуем снапшот из meta.
        meta = _load_job_meta(app.state.db_path, job_id)
        if meta is None:
            raise HTTPException(404, "Job not found")
        if meta.get("user") != user["nick"]:
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
            "analysis": _analysis_for_user(db, game_id, user),
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
        Ответ модели дополнительно проверяется ``assert_facts``: при
        нарушениях ``ideas: null`` и ``rejected: true`` (клиент покажет
        данные книги вместо непроверенного текста).
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
        if isinstance(text, str) and text.strip():
            facts = _theory_ideas_facts(body.line)
            try:
                facttext.assert_facts(text.strip(), facts)
            except facttext.FactCheckError as exc:
                logger.info("theory ideas отклонены по фактам: %s", exc)
                return {"ideas": None, "cached": cached, "rejected": True}
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
            for game in games:
                if _analysis_has_foreign_copy(db, game.id, user["nick"]):
                    raise HTTPException(
                        409,
                        "Партия с таким id уже есть в кеше другого игрока",
                    )
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
            if _analysis_has_foreign_copy(db, game_id, user["nick"]):
                raise HTTPException(409, "Партия уже есть в кеше у другого игрока")
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
            analysis = _analysis_for_user(db, game_id, user["nick"])
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
            is_user = (ply % 2 == 0) == user_is_white
            fen_before = board.fen()
            best_line = entry.get("best_line") or []
            item = {
                "ply": ply,
                "san": san,
                "color": "w" if ply % 2 == 0 else "b",
                "is_user": is_user,
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
                "best_line": best_line,
                "fen_before": fen_before,
            }
            if is_user and entry.get("classification") in (
                "blunder",
                "mistake",
                "inaccuracy",
            ):
                why = facttext.why_text(entry, fen_before, san)
                if why:
                    item["why"] = why
            items.append(item)
            try:
                board.push_san(san)
            except ValueError:
                break

        phases = phase_stats(moves_san, analysis, game.user_color)
        user_acc = acc_accuracy(_avg_drop(for_user=True))
        opponent_acc = acc_accuracy(_avg_drop(for_user=False))
        user_errors = [
            item
            for item in items
            if item.get("is_user")
            and item.get("badge") in ("blunder", "mistake", "inaccuracy", "missed_win")
        ]
        worst_error = (
            max(user_errors, key=lambda item: float(item.get("drop") or 0))
            if user_errors
            else None
        )
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
                errors=len(user_errors),
                worst=worst_error,
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
            analysis = _analysis_for_user(db, game_id, user["nick"])
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
            analysis = _analysis_for_user(db, game_id, user["nick"])
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
            analysis = _analysis_for_user(db, game_id, user["nick"])
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
        opening_book = {
            c: build_opening_book(pairs, c, max_depth=max_depth)
            for c in colors
        }
        return {
            "color": color,
            "games": len(pairs),
            "max_depth": max_depth,
            "lines": lines,
            "weak": weak,
            "openings": opening_stats(pairs, user=user),
            "opening_book": opening_book,
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
            # humanize поднимает MaiaLite/Stockfish — это тяжёлая задача.
            _check_job_limits(user["nick"])
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
        if not user:
            raise HTTPException(401, "Нужен вход")
        _check_light_job_limits(user["nick"])
        job = CoachJob(
            id=uuid.uuid4().hex,
            user=user["nick"],
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
    def api_task_today(request: Request, repeat: bool = False):
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _task_payload(db, user["nick"], repeat=repeat),
            app.state.db_path,
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
            # Задача считается решённой только при верном ответе; иначе
            # пользователь может попробовать ещё раз. Попытка пишется вне
            # зависимости от верности; номер попытки копится по позиции.
            if correct:
                db.set_meta(_task_done_key(payload["date"], user["nick"]), "1")
            try:
                attempt = _record_task_attempt(
                    db, user["nick"], payload["task_key"], payload["date"], correct
                )
            except Exception:  # noqa: BLE001 — ответ важнее записи истории
                logger.warning(
                    "Не удалось записать попытку для %s", payload["task_key"],
                    exc_info=True,
                )
                attempt = None
            return {
                "correct": correct,
                "expected": payload["best_san"],
                "drop": payload["drop"],
                "win_before": payload["win_before"],
                "win_after": payload["win_after"],
                "san_played": payload["san_played"],
                "attempt": attempt,
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
        if cached and not _is_bad_explanation(cached) and _task_explanation_ok(payload, cached):
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

    # --- Мини-трекер времени: только данные, введённые пользователем ---
    # Скрытого трекинга сессии нет: время берётся из часов Lichess в
    # проанализированных партиях и из ручного ввода (/api/track/lichess_manual).
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
        with Database(app.state.db_path) as db:
            manual_sec = _manual_seconds(db, _lichess_manual_key(day, user["nick"]))
            games = db.get_games(user["nick"], limit=100_000)
        lichess_sec = 0.0
        games_today = 0
        for game in games:
            if timeutil.date_of_ms(game.created_at) != day:
                continue
            games_today += 1
            lichess_sec += _clock_seconds(game.clocks)
        return {
            "date": day,
            "lichess_sec": int(lichess_sec),
            "manual_sec": int(manual_sec),
            "total_sec": int(lichess_sec + manual_sec),
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
        if body.my_elo is not None and body.my_elo <= 0:
            raise HTTPException(400, "Рейтинг: положительное число")
        # -1 — служебный sentinel гибрида Maia+Stockfish, не рейтинг.
        if body.opp_elo is not None and body.opp_elo != -1 and body.opp_elo <= 0:
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

    @app.post("/api/opening/{session_id}/to-game")
    def api_opening_to_game(session_id: str, request: Request):
        """Сохраняет тренировочную партию как обычную и сразу её анализирует."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, user["nick"], session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        moves = list(session.get("moves") or [])
        if not moves:
            raise HTTPException(400, "В партии нет ходов")
        user_color = session.get("color") or "white"
        board = board_from_moves(moves)
        result = board.result(claim_draw=False) if board.is_game_over(claim_draw=False) else "*"
        if result == "1-0":
            winner = "white"
        elif result == "0-1":
            winner = "black"
        else:
            winner = None
        result_for_user = (
            "win" if winner == user_color else "loss" if winner else "draw"
        )
        name, eco = classify_opening(moves)
        game = Game(
            id=f"opening-{session_id}",
            moves=moves,
            user_color=user_color,
            opponent="Maia",
            result_for_user=result_for_user,
            status="normal",
            opening=name,
            eco=eco,
            created_at=int(_now() * 1000),
        )
        with Database(app.state.db_path) as db:
            db.init_db()
            db.save_games([game], user["nick"])
        try:
            with Engine(depth=10, multipv=1, time_ms=800) as engine:
                summary = engine.analyze_game(game).summary()
            with Database(app.state.db_path) as db:
                db.init_db()
                db.save_analysis(game.id, summary, depth=10)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"Не удалось проанализировать: {exc}") from exc
        return {"game_id": game.id}

    @app.get("/api/opening/{session_id}/pgn")
    def api_opening_pgn(session_id: str, request: Request):
        """PGN тренировочной партии для скачивания."""
        user = _current_user(app.state.db_path, request)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            db.init_db()
            session = _opening_owned(db, user["nick"], session_id)
        if session is None:
            raise HTTPException(404, "Сессия не найдена")
        moves = list(session.get("moves") or [])
        color = session.get("color") or "white"
        import chess.pgn as chess_pgn

        board = board_from_moves(moves)
        result = board.result(claim_draw=False) if board.is_game_over(claim_draw=False) else "*"
        name, eco = classify_opening(moves)
        pgn_game = chess_pgn.Game()
        pgn_game.headers.update(
            {
                "Event": "Chess Trainer opening",
                "Site": "chess-trainer",
                "Date": time.strftime("%Y.%m.%d"),
                "White": user["nick"] if color == "white" else "Maia",
                "Black": "Maia" if color == "white" else user["nick"],
                "Result": result,
                "Opening": name or "",
                "ECO": eco or "",
            }
        )
        node = pgn_game
        cursor = chess.Board()
        for san in moves:
            try:
                move = cursor.parse_san(san)
            except ValueError:
                break
            cursor.push(move)
            node = node.add_main_variation(move)
        return Response(
            str(pgn_game),
            media_type="application/x-chess-pgn; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="opening-{session_id[:8]}.pgn"'
                )
            },
        )

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

    # --- Тренер чтения: «Моя система» (Нимцович) ---

    _READING_GOAL = "Найдите лучший ход и кратко опишите план."
    _READING_WRONG_VERDICTS = ("incorrect", "invalid")

    def _reading_session_rows(
        db: Database, nick: str, session_id: str
    ) -> list[dict[str, Any]]:
        return db.get_reading_attempts(nick, session_id=session_id, limit=100)

    def _reading_session_lesson(
        db: Database, nick: str, session_id: str
    ) -> tuple[reading.ReadingLesson | None, list[dict[str, Any]]]:
        """Урок и все строки сессии; чужой/битый session_id → (None, [])."""
        rows = _reading_session_rows(db, nick, session_id)
        if not rows:
            return None, []
        anchor = rows[0]
        try:
            lesson = reading.find_lesson(
                reading.COURSE,
                str(anchor.get("chapter_id") or ""),
                str(anchor.get("lesson_id") or ""),
            )
        except KeyError:
            return None, rows
        return lesson, rows

    def _reading_answered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows
                if str(r.get("verdict") or "").strip().lower() in reading.VERDICTS]

    def _reading_session_payload(
        lesson: reading.ReadingLesson,
        session_id: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        answered = _reading_answered(rows)
        return {
            "session_id": session_id,
            "course_id": reading.COURSE.id,
            "course_version": reading.COURSE.version,
            "chapter_id": lesson.chapter_id,
            "lesson_id": lesson.id,
            "goal": _READING_GOAL,
            "status": "answered" if answered else "active",
            "attempts": len(answered),
            "lesson": reading.lesson_payload(lesson),
        }

    def _reading_progress(db: Database, nick: str) -> reading.Progress:
        attempts = db.get_reading_attempts(nick, course_id=reading.COURSE.id)
        return reading.build_progress(attempts)

    @app.get("/api/reading/meta")
    def api_reading_meta(request: Request):
        user = _require_user(request)
        with Database(app.state.db_path) as db:
            db.init_db()
            progress = _reading_progress(db, user["nick"])
        meta = reading.get_course_meta(reading.COURSE)
        meta["chapters"] = [
            {
                "id": entry["chapter_id"],
                "title": entry["title"],
                "summary": entry["summary"],
                "order": entry["order"],
                "lessons_total": entry["lessons_total"],
                "completed": entry["completed"],
                "correct_first_attempt": entry["correct_first_attempt"],
                "accuracy": entry["accuracy"],
                "status": entry["status"],
            }
            for entry in reading.list_chapters(reading.COURSE, progress)
        ]
        return meta

    @app.post("/api/reading/session")
    def api_reading_session_create(
        request: Request,
        chapter_id: str = Form(...),
        lesson_id: str = Form(""),
    ):
        user = _require_user(request)
        nick = user["nick"]
        chapter_key = (chapter_id or "").strip()
        if not chapter_key:
            raise HTTPException(400, "Нужен chapter_id")
        lesson_key = (lesson_id or "").strip() or None
        with Database(app.state.db_path) as db:
            db.init_db()
            progress = _reading_progress(db, nick)
            try:
                session = reading.create_session(
                    reading.COURSE, nick, chapter_key, lesson_key, progress
                )
            except KeyError:
                raise HTTPException(404, "Глава или урок не найдены") from None
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            db.create_reading_attempt(
                session_id=session["session_id"],
                player=nick,
                course_id=session["course_id"],
                chapter_id=session["chapter_id"],
                lesson_id=session["lesson_id"],
                verdict="started",
                course_version=session["course_version"],
            )
        lesson = reading.find_lesson(
            reading.COURSE, session["chapter_id"], session["lesson_id"]
        )
        return _reading_session_payload(lesson, session["session_id"], [])

    @app.get("/api/reading/session/{session_id}")
    def api_reading_session_get(session_id: str, request: Request):
        user = _require_user(request)
        with Database(app.state.db_path) as db:
            db.init_db()
            lesson, rows = _reading_session_lesson(db, user["nick"], session_id)
        if not rows:
            raise HTTPException(404, "Сессия не найдена")
        if lesson is None:
            raise HTTPException(404, "Урок не найден в текущей версии курса")
        return _reading_session_payload(lesson, session_id, rows)

    @app.post("/api/reading/session/{session_id}/answer")
    def api_reading_answer(
        session_id: str,
        request: Request,
        move: str = Form(...),
        plan: str = Form(""),
    ):
        user = _require_user(request)
        nick = user["nick"]
        move_clean = (move or "").strip()
        if not move_clean:
            raise HTTPException(400, "Нужен ход (SAN или UCI)")
        try:
            plan_clean = reading.validate_plan(plan)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with Database(app.state.db_path) as db:
            db.init_db()
            lesson, rows = _reading_session_lesson(db, nick, session_id)
            if not rows:
                raise HTTPException(404, "Сессия не найдена")
            if lesson is None:
                raise HTTPException(404, "Урок не найден в текущей версии курса")
            if _reading_answered(rows):
                raise HTTPException(409, "Ответ уже отправлен")
            result = reading.judge_answer(reading.COURSE, lesson, move_clean, plan_clean)
            if result["verdict"] == "invalid":
                # Ход не разобран — 400, попытку не сохраняем.
                raise HTTPException(
                    400, result.get("message") or "Ход не разобран"
                )
            db.create_reading_attempt(
                session_id=session_id,
                player=nick,
                course_id=reading.COURSE.id,
                chapter_id=lesson.chapter_id,
                lesson_id=lesson.id,
                verdict=result["verdict"],
                answer_move=(result.get("move") or {}).get("played_san"),
                plan=plan_clean or None,
                judge_kind=result.get("judge_kind"),
                engine_depth=lesson.engine.depth,
                course_version=reading.COURSE.version,
            )
        return {
            "session_id": session_id,
            "chapter_id": lesson.chapter_id,
            "lesson_id": lesson.id,
            "verdict": result["verdict"],
            "judge_kind": result.get("judge_kind"),
            "move": result.get("move"),
            "plan": result.get("plan"),
            "accepted_moves": result.get("accepted_moves"),
            "revealed": reading.revealed_payload(lesson),
        }

    @app.post("/api/reading/session/{session_id}/skip")
    def api_reading_skip(
        session_id: str,
        request: Request,
        reason: str = Form(""),
    ):
        user = _require_user(request)
        nick = user["nick"]
        reason_clean = (reason or "").strip()[:200]
        with Database(app.state.db_path) as db:
            db.init_db()
            lesson, rows = _reading_session_lesson(db, nick, session_id)
            if not rows:
                raise HTTPException(404, "Сессия не найдена")
            if lesson is None:
                raise HTTPException(404, "Урок не найден в текущей версии курса")
            if _reading_answered(rows):
                raise HTTPException(409, "Ответ уже отправлен")
            already_skipped = any(
                str(r.get("verdict") or "").strip().lower() == "skipped"
                for r in rows
            )
            if not already_skipped:
                db.create_reading_attempt(
                    session_id=session_id,
                    player=nick,
                    course_id=reading.COURSE.id,
                    chapter_id=lesson.chapter_id,
                    lesson_id=lesson.id,
                    verdict="skipped",
                    plan=reason_clean or None,
                    course_version=reading.COURSE.version,
                )
        return {"session_id": session_id, "status": "skipped"}

    @app.get("/api/reading/chapters/{chapter_id}/summary")
    def api_reading_chapter_summary(chapter_id: str, request: Request):
        user = _require_user(request)
        with Database(app.state.db_path) as db:
            db.init_db()
            progress = _reading_progress(db, user["nick"])
        try:
            return reading.get_chapter_meta(reading.COURSE, progress, chapter_id)
        except KeyError:
            raise HTTPException(404, "Глава не найдена") from None

    @app.get("/api/reading/review")
    def api_reading_review(
        request: Request,
        chapter_id: str = "",
        limit: int = 20,
    ):
        user = _require_user(request)
        nick = user["nick"]
        chapter_key = (chapter_id or "").strip()
        if chapter_key:
            try:
                reading.find_chapter(reading.COURSE, chapter_key)
            except KeyError:
                raise HTTPException(404, "Глава не найдена") from None
        cap = max(1, min(limit, 100))
        with Database(app.state.db_path) as db:
            db.init_db()
            rows = db.get_reading_attempts(
                nick,
                course_id=reading.COURSE.id,
                chapter_id=chapter_key or None,
                limit=500,
            )
        wrong = [
            r for r in rows
            if str(r.get("verdict") or "").strip().lower() in _READING_WRONG_VERDICTS
        ][:cap]
        items: list[dict[str, Any]] = []
        for row in wrong:
            lesson_title: str | None = None
            try:
                lesson = reading.find_lesson(
                    reading.COURSE,
                    str(row.get("chapter_id") or ""),
                    str(row.get("lesson_id") or ""),
                )
                lesson_title = lesson.title
            except KeyError:
                pass
            items.append(
                {
                    "attempt_id": row.get("id"),
                    "session_id": row.get("session_id"),
                    "chapter_id": row.get("chapter_id"),
                    "lesson_id": row.get("lesson_id"),
                    "lesson_title": lesson_title,
                    "verdict": row.get("verdict"),
                    "move": row.get("answer_move"),
                    "created_at": row.get("created_at"),
                }
            )
        return {"items": items, "count": len(items)}

    # --- Тренер эндшпиля: задачи из своих партий + эталонные позиции ---

    _ENDGAME_SOLVED_KEY = "endgame_solved"

    def _endgame_solved_state(db: Database, nick: str) -> dict[str, Any]:
        """Список решённых задач по источникам из meta (JSON, по пользователю)."""
        raw = db.get_meta(f"{_ENDGAME_SOLVED_KEY}_{nick}")
        if not raw:
            return {"master": [], "own": []}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {"master": [], "own": []}
        if not isinstance(data, dict):
            return {"master": [], "own": []}
        result: dict[str, Any] = {"master": [], "own": []}
        for source in ("master", "own"):
            keys = data.get(source)
            if isinstance(keys, list):
                result[source] = [str(key) for key in keys if key]
        return result

    def _endgame_progress(
        solved: dict[str, Any], source: str, task_keys: list[str]
    ) -> dict[str, int]:
        """Прогресс источника: всё и решённое (учёт только существующих задач)."""
        done = set(solved.get(source) or [])
        return {
            "total": len(task_keys),
            "solved": sum(1 for key in task_keys if key in done),
        }

    @app.get("/api/endgame/meta")
    def api_endgame_meta(request: Request, source: str = ""):
        user = _require_user(request)
        key = (source or "").strip().lower()
        if key not in ("", "own", "master"):
            raise HTTPException(400, "source: own, master или пусто (оба источника)")
        with Database(app.state.db_path) as db:
            db.init_db()
            rows = eg.own_task_rows(db.get_analyzed_games(user["nick"], limit=500))
            solved = _endgame_solved_state(db, user["nick"])
        master_tasks = [eg.master_payload(task) for task in eg.MASTER_TASKS]
        own_tasks = [eg.own_payload(row) for row in rows]
        progress = {
            "master": _endgame_progress(
                solved, "master", [item["task_key"] for item in master_tasks]
            ),
            "own": _endgame_progress(
                solved, "own", [item["task_key"] for item in own_tasks]
            ),
        }
        return {
            "sources": list(eg.SOURCES),
            "progress": progress,
            "master": master_tasks if key in ("", "master") else [],
            "own": own_tasks if key in ("", "own") else [],
        }

    @app.get("/api/endgame/legal")
    def api_endgame_legal(request: Request, source: str = "", task_key: str = ""):
        """Легальные ходы задачи — для click-to-move на доске (без решения)."""
        user = _require_user(request)
        key = (source or "").strip().lower()
        if key not in ("own", "master"):
            raise HTTPException(400, "source: own или master")
        chess_task_key = (task_key or "").strip()
        if not chess_task_key:
            raise HTTPException(400, "Нужен task_key")
        with Database(app.state.db_path) as db:
            db.init_db()
            if key == "master":
                task = eg.MASTER_TASK_BY_KEY.get(chess_task_key)
                if task is None:
                    raise HTTPException(404, "Задача не найдена")
                fen = task.fen
            else:
                rows = eg.own_task_rows(db.get_analyzed_games(user["nick"], limit=500))
                row = next(
                    (item for item in rows if item["task_key"] == chess_task_key), None
                )
                if row is None:
                    raise HTTPException(404, "Задача не найдена")
                fen = row["fen"]
        board = chess.Board(fen)
        moves = [
            {
                "from": chess.square_name(m.from_square),
                "to": chess.square_name(m.to_square),
                "san": board.san(m),
            }
            for m in board.legal_moves
        ]
        return {
            "source": key,
            "task_key": chess_task_key,
            "fen": fen,
            "turn": "white" if board.turn == chess.WHITE else "black",
            "moves": moves,
        }

    @app.post("/api/endgame/answer")
    def api_endgame_answer(request: Request, body: EndgameAnswerBody):
        user = _require_user(request)
        nick = user["nick"]
        key = (body.source or "").strip().lower()
        if key not in ("own", "master"):
            raise HTTPException(400, "source: own или master")
        chess_task_key = (body.task_key or "").strip()

        def _check(db: Database) -> dict[str, Any]:
            if key == "master":
                task = eg.MASTER_TASK_BY_KEY.get(chess_task_key)
                if task is None:
                    raise HTTPException(404, "Задача не найдена")
                board = chess.Board(task.fen)
                solution = eg.master_solution(task)
                payload = eg.master_payload(task, with_solution=True)
            else:
                rows = eg.own_task_rows(db.get_analyzed_games(nick, limit=500))
                row = next(
                    (item for item in rows if item["task_key"] == chess_task_key), None
                )
                if row is None:
                    raise HTTPException(404, "Задача не найдена")
                board = chess.Board(row["fen"])
                solution = eg.own_solution(row)
                payload = {**eg.own_payload(row), **solution}

            move, san = eg.parse_move(board, body.move)
            if move is None:
                raise HTTPException(400, "Нет такого хода")
            correct = eg.same_san(san, solution["best_move_san"]) or any(
                eg.same_san(san, alt) for alt in solution.get("alt_moves_san") or []
            )

            # Судья опционален: Syzygy по SYZYGY_PATH, иначе Stockfish, сбой — None.
            after = board.copy()
            after.push(move)
            judge = eg.judge_position(after)

            result = "correct" if correct else "wrong"
            if correct:
                state = _endgame_solved_state(db, nick)
                done = list(state.get(key) or [])
                if chess_task_key not in done:
                    done.append(chess_task_key)
                    db.set_meta(
                        f"{_ENDGAME_SOLVED_KEY}_{nick}",
                        json.dumps({**state, key: done}, ensure_ascii=False),
                    )
            return {"ok": True, "result": result, "task": payload, "judge": judge}

        with Database(app.state.db_path) as db:
            db.init_db()
            return _check(db)

    # --- Auth: регистрация и вход (логин+пароль+lichess) ---
    @app.post("/api/auth/register")
    def api_auth_register(
        request: Request,
        response: Response,
        login: str = Form(""),
        password: str = Form(""),
        lichess: str = Form(""),
        fide: str = Form(""),
    ):
        from .identity import validate_nick

        clean = (login or "").strip()
        if not 1 <= len(clean) <= 64:
            raise HTTPException(400, "Логин: от 1 до 64 символов")
        if validate_nick(clean) is None:
            raise HTTPException(
                400, "Логин: 2–30 символов, латиница, цифры, _ и -"
            )
        reserved = {
            str(item).strip().lower()
            for item in settings.admin_nicks
            if str(item).strip()
        }
        if len(password or "") < PASSWORD_MIN_LENGTH:
            raise HTTPException(
                400, f"Пароль: минимум {PASSWORD_MIN_LENGTH} символов"
            )
        lich = (lichess or "").strip()
        if not 1 <= len(lich) <= 64:
            raise HTTPException(400, "Укажи Lichess-ник")
        fide_clean = (fide or "").strip() or None
        db = Database(app.state.db_path)
        db.init_db()
        rec = db.get_user_record(clean)
        if rec is None and clean.lower() in reserved:
            raise HTTPException(403, "Этот ник зарезервирован администратором")
        if rec and rec.get("password_hash"):
            raise HTTPException(409, "Логин занят")
        if rec:
            # Старый профиль без пароля — потенциальный захват аккаунта.
            # Claim разрешён только явным env-флагом для локальной миграции.
            if not _is_active_record(rec):
                raise HTTPException(403, "Доступ ограничен")
            if not settings.allow_legacy_claim:
                raise HTTPException(
                    409,
                    "Логин уже существует; попроси администратора сбросить пароль",
                )
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
        # Успешная регистрация/claim сбрасывает счётчики неудачных входов.
        _login_rate_clear(db, _login_rate_keys(request, nick))
        token = secrets.token_hex(32)
        db.create_session(nick, token, days=_SESSION_DAYS)
        _set_session_cookie(
            response, token, secure=_request_is_https(request)
        )
        _maybe_snapshot_rating(db, nick, _today())
        return {"ok": True, "nick": nick}

    @app.post("/api/auth/login")
    def api_auth_login(
        request: Request,
        response: Response,
        login: str = Form(""),
        password: str = Form(""),
    ):
        clean = (login or "").strip()
        db = Database(app.state.db_path)
        db.init_db()
        rate_keys = _login_rate_keys(request, clean)
        now = _now()
        if _login_rate_blocked(db, rate_keys, now):
            # Ответ намеренно generic: не раскрываем, существует ли аккаунт.
            raise HTTPException(
                429, "Слишком много попыток входа. Попробуй позже."
            )
        rec = db.get_user_record(clean)
        if not rec or not rec.get("password_hash"):
            _login_rate_record_failure(db, rate_keys, now)
            raise HTTPException(401, "Неверный логин или пароль")
        if not verify_password(password or "", rec["password_hash"]):
            _login_rate_record_failure(db, rate_keys, now)
            raise HTTPException(401, "Неверный логин или пароль")
        if not _is_active_record(rec):
            raise HTTPException(403, "Доступ ограничен")
        _login_rate_clear(db, rate_keys)
        token = secrets.token_hex(32)
        db.create_session(rec["nick"], token, days=_SESSION_DAYS)
        _set_session_cookie(
            response, token, secure=_request_is_https(request)
        )
        _maybe_snapshot_rating(db, rec["nick"], _today())
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
        if len(new or "") < PASSWORD_MIN_LENGTH:
            raise HTTPException(
                400, f"Новый пароль: минимум {PASSWORD_MIN_LENGTH} символов"
            )
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