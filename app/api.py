"""Chess Trainer API — чистый JSON API для веб-интерфейса.

Бэкенд не знает о шаблонах/HTML. Фронтенд — отдельные статические файлы
в папке frontend/, которые потребляют этот API.
"""

from __future__ import annotations

import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import data as shared_data
from .analyzer import Engine
from .config import settings
from .db import Database
from .drills import drills_to_json, drills_to_pgn
from .games import fetch_user_games
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