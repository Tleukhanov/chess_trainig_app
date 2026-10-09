"""Chess Trainer API — чистый JSON API для веб-интерфейса.

Бэкенд не знает о шаблонах/HTML. Фронтенд — отдельные статические файлы
в папке frontend/, которые потребляют этот API.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field

import chess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import data as shared_data
from .analyzer import Engine
from .coach import build_request, run_coach
from .config import settings
from .db import Database
from .drills import drills_to_json, drills_to_pgn
from .fide import (
    TournamentGame,
    build_fide_trend,
    build_tournament_report,
    fetch_fide_player,
    fetch_fide_ratings,
)
from .games import fetch_user_games
from .humanize import humanize_report
from .llm import ChatMessage, LLMClient, _is_local_host, clean_llm_text, looks_like_thinking
from .maia import MaiaLitePolicy
from .mentor import build_mentor_request, format_mentor_reply, run_mentor
from .metrics import metric
from .opponent import build_confrontations, build_opponent_profile
from .paths import user_dir
from .plan import build_plan
from .progress import build_progress
from .repertoire import Repertoire, opening_stats
from .report import build_report

# --- job registry ----------------------------------------------------------

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

def _register_job(job: CoachJob) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job

def _get_job(job_id: str) -> CoachJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)

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
                games = fetch_user_games(
                    job.user,
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

            job.total = len(target)
            job.phase = "Анализ движком"

            if target:
                with Engine(depth=params["depth"], multipv=params["multipv"]) as engine:
                    for i, game in enumerate(target, 1):
                        summary = engine.analyze_game(game).summary()
                        db.save_analysis(game.id, summary, depth=params["depth"])
                        job.done = i
                        job.current = game.id
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

# --- helpers ---------------------------------------------------------------

def _current_user(db_path: Path) -> dict | None:
    with Database(db_path) as db:
        db.init_db()
        return db.get_current_user()


def _call_data(fn, db_path: Path):
    """Вызывает слой данных; RuntimeError («нет партий») → HTTP 404."""
    try:
        with Database(db_path) as db:
            return fn(db)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc


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
    """Сегодняшняя локальная дата сервера в формате YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d")


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


def _task_payload(db: Database, user: str, date: str | None = None) -> dict[str, Any]:
    """JSON для /api/task/today: позиция до хода, решение и объяснение."""
    day = date or _today()
    item = _pick_daily_task(db, user, day)
    game = item["game"]
    move = item["move"]
    ply = item["ply"]
    best = str(move.get("best_move_san"))
    explanation = db.get_task_explanation(item["task_key"])
    if explanation is not None and looks_like_thinking(explanation):
        # Отравленный кэш (reasoning-разбор вместо ответа): игнорируем,
        # кнопка «Объяснить» снова доступна, успешный повтор перезапишет
        # мусор через UPSERT save_task_explanation. Строки из БД не удаляем.
        explanation = None
    return {
        "date": day,
        "task_key": item["task_key"],
        "game_id": game.id,
        "ply": ply,
        "fen": item["fen"],
        "san_played": str(move.get("san") or game.moves[ply]),
        "best_san": best,
        "best_line": [str(s) for s in (move.get("best_line") or [])],
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
    """Один вызов LLM для объяснения задачи (с повтором при thinking-мусоре).

    thinking-блоки вырезаются, служебный разбор вместо ответа — это ошибка
    (job уйдёт в error, а не сохранит мусор в БД).
    """
    llm = LLMClient()
    messages = _task_explain_messages(payload)
    text = clean_llm_text(llm.chat(messages, temperature=0.4, max_tokens=800))
    if text and not looks_like_thinking(text):
        return text
    retry = [
        messages[0],
        ChatMessage(
            "user",
            messages[1].content + "\nБез разбора. Сразу объяснение по-русски.",
        ),
    ]
    second = clean_llm_text(llm.chat(retry, temperature=0.4, max_tokens=800))
    if not second or looks_like_thinking(second):
        raise RuntimeError("LLM вернул служебный разбор вместо объяснения")
    return second

# --- Цель и недельный план ------------------------------------------------

_PLAN_GOAL_FOCUSES = ("auto", "opening", "middlegame", "endgame")
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


def _load_plan_goal(db: Database, nick: str) -> dict[str, Any]:
    """Цель из meta: {"goal": str|None, "minutes": int, "focus": str}.

    Битый/частичный JSON чинится дефолтами, исключения не выбрасываются.
    """
    result: dict[str, Any] = {
        "goal": None,
        "minutes": _PLAN_GOAL_DEFAULT_MINUTES,
        "focus": "auto",
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
            if time.strftime("%Y-%m-%d", time.localtime(game.created_at / 1000)) != day:
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


# --- API factory -----------------------------------------------------------

def create_api(
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Chess Trainer API", version="0.4.0", docs_url="/api/docs", redoc_url=None)
    app.state.db_path = Path(db_path) if db_path else settings.db_path
    app.state.data_dir = Path(data_dir) if data_dir else settings.data_dir

    # --- User / Auth ---
    @app.get("/api/user/current")
    def api_current_user(request: Request):
        user = _current_user(app.state.db_path)
        if user:
            return {"user": user}
        raise HTTPException(status_code=404, detail="No current user")

    @app.get("/api/users")
    def api_list_users(request: Request):
        with Database(app.state.db_path) as db:
            db.init_db()
            users = db.list_users()
        return {"users": users}

    @app.post("/api/user/add")
    def api_user_add(nick: str = Form(...), fide: str = Form("")):
        from .identity import validate_nick
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        if not validate_nick(clean):
            raise HTTPException(400, f"Невалидный ник: {clean}")
        with Database(app.state.db_path) as db:
            db.init_db()
            db.upsert_user(clean, fide_id=fide.strip() or None)
            db.set_current_user(clean)
            db.touch_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/switch")
    def api_user_switch(nick: str = Form(...)):
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        with Database(app.state.db_path) as db:
            db.init_db()
            db.set_current_user(clean)
            db.touch_user(clean)
        return {"ok": True, "nick": clean}

    @app.post("/api/user/delete")
    def api_user_delete(nick: str = Form(...)):
        """Удаляет профиль и его партии/анализ из БД (файлы data/<ник>/ остаются)."""
        clean = nick.strip()
        if not clean:
            raise HTTPException(400, "Ник не может быть пустым")
        with Database(app.state.db_path) as db:
            db.init_db()
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: shared_data.overview(db, user["nick"], root=app.state.data_dir),
            app.state.db_path,
        )

    @app.get("/api/plan")
    def api_plan(request: Request, max_depth: int = 16):
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: shared_data.progress(
                db, user["nick"], root=app.state.data_dir, windows=windows
            ),
            app.state.db_path,
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            rep = shared_data.report(db, user["nick"])
        if not rep:
            raise HTTPException(404, "No report")
        return rep

    @app.get("/api/plan/goal")
    def api_plan_goal_get(request: Request):
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            return _load_plan_goal(db, user["nick"])

    @app.post("/api/plan/goal")
    def api_plan_goal_set(
        request: Request,
        goal: str = Form(""),
        minutes: str = Form(""),
        focus: str = Form(""),
    ):
        user = _current_user(app.state.db_path)
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
        saved = {"goal": text, "minutes": mins, "focus": focus_clean}
        with Database(app.state.db_path) as db:
            db.set_meta(
                _plan_goal_key(user["nick"]),
                json.dumps(saved, ensure_ascii=False),
            )
        return {"ok": True, **saved}

    @app.get("/api/plan/week")
    def api_plan_week(request: Request):
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        with Database(app.state.db_path) as db:
            return _plan_week_payload(db, user["nick"], app.state.data_dir)

    # --- Coach / Analysis jobs ---
    @app.post("/api/coach/run")
    def api_coach_run(
        request: Request,
        max: int = Form(50),
        perf: str = Form("rapid"),
        depth: int = Form(14),
        multipv: int = Form(3),
        cached_only: str | None = Form(None),
        refresh: str | None = Form(None),
        since: int | None = Form(None),
    ):
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")

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

    @app.get("/api/jobs/{job_id}")
    def api_job_status(job_id: str):
        job = _get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job.as_dict()

    # --- Games (просмотр партий) ---
    def _game_summary(g) -> dict:
        return {
            "id": g.id,
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

    @app.get("/api/games")
    def api_games(request: Request, limit: int = 100):
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _games_payload(db, user["nick"], limit),
            app.state.db_path,
        )

    @app.get("/api/games/{game_id}")
    def api_game_detail(game_id: str, request: Request):
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _game_detail_payload(db, user["nick"], game_id),
            app.state.db_path,
        )

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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        opp = opponent.strip()
        if not opp:
            raise HTTPException(400, "Укажи ник соперника")
        if not 1 <= max <= 200:
            raise HTTPException(400, "max должен быть от 1 до 200")
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        return _call_data(
            lambda db: _task_payload(db, user["nick"]), app.state.db_path
        )

    @app.post("/api/task/answer")
    def api_task_answer(request: Request, answer: str = Form(...)):
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        payload = _call_data(
            lambda db: _task_payload(db, user["nick"]), app.state.db_path
        )
        cached = payload.get("explanation")
        if cached and not looks_like_thinking(cached):
            return {"cached": True, "explanation": cached}
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
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
        if not user:
            raise HTTPException(404, "No current user")
        now = _now()
        key = _track_key(_today(), user["nick"])
        with Database(app.state.db_path) as db:
            state = _track_state(db, key)
            if state is None:
                total = 0.0
            else:
                gap = now - state["last"]
                total = state["total"] + gap if 0.0 <= gap <= 300.0 else state["total"]
            db.set_meta(key, json.dumps({"last": now, "total": total}))
        return {"ok": True, "total": int(total)}

    @app.post("/api/track/lichess_manual")
    def api_track_lichess_manual(request: Request, minutes: str = Form("")):
        user = _current_user(app.state.db_path)
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
        user = _current_user(app.state.db_path)
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
            if time.strftime("%Y-%m-%d", time.localtime(game.created_at / 1000)) != day:
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