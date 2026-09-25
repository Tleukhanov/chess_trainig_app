"""CLI шахматного тренера.

Подкоманда ``coach`` выгружает партии с Lichess, прогоняет их через
Stockfish и строит русскоязычный текстовый отчёт с ошибками игрока.
Подкоманда ``review`` берёт уже проанализированные партии из кеша и
строит LLM-разбор ключевых моментов (без переанализа движком).

Примеры:
    python -m trainer coach --user NICK
    python -m trainer coach --user NICK --max 10 --perf rapid --depth 14 --multipv 3
    python -m trainer coach --user NICK --cached-only
    python -m trainer review --user NICK --max 3
    python -m trainer review --user NICK --dry-run --max 2
    python -m trainer review --game 2SXfzXV2
    python -m trainer review --user NICK --json review.json
    python -m trainer drills --user NICK --out data/drills.pgn
    python -m trainer drills --user NICK --out data/drills.json --min-drop 10
    python -m trainer humanize --user NICK --out data/humanity.json
    python -m trainer drills --user NICK --humanity data/humanity.json --verdict unnatural
    python -m trainer repertoire --user NICK
    python -m trainer repertoire --user NICK --color black --no-write
"""

from __future__ import annotations

import argparse
import json
import textwrap
import time
from pathlib import Path

from app.analyzer import Engine
from app.coach import build_request, run_coach
from app.config import settings
from app.db import Database
from app.drills import collect_drills, drills_to_json, drills_to_pgn, summarize
from app.games import Game, fetch_user_games
from app.llm import ChatMessage, LLMClient
from app.mentor import build_mentor_request, format_mentor_reply, run_mentor
from app.plan import build_plan, format_plan
from app.progress import build_progress, format_progress
from app.report import build_report, format_report
from app.repertoire import (
    Repertoire,
    format_lines,
    opening_stats,
    repertoire_to_json,
    repertoire_to_pgn,
)

__all__ = ["main"]


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trainer",
        description="Chess Trainer — персональный шахматный тренер",
    )
    parser.add_argument("--version", action="version", version="chess-trainer 0.1.0")
    subparsers = parser.add_subparsers(dest="command", metavar="КОМАНДА")

    coach = subparsers.add_parser("coach", help="анализ партий и построение отчёта")
    coach.add_argument("--user", default=None, help="ник на Lichess (не нужен при --game)")
    coach.add_argument("--game", nargs="*", default=None, help="id партий (один или несколько): переанализировать их из кеша точечно, вместе с --depth")
    coach.add_argument("--max", type=int, default=settings.games_max, help="сколько партий взять (по умолчанию %(default)s)")
    coach.add_argument("--perf", default="rapid", help="пул партий: rapid/classical/blitz/...")
    coach.add_argument("--depth", type=int, default=settings.stockfish_depth, help="глубина анализа Stockfish")
    coach.add_argument("--multipv", type=int, default=settings.stockfish_multipv, help="число анализируемых линий")
    coach.add_argument("--since", type=int, default=None, help="брать партии с этого времени (unix, миллисекунды)")
    coach.add_argument("--cached-only", action="store_true", help="не ходить в сеть, брать только кеш")
    coach.add_argument("--refresh", action="store_true", help="переанализировать все партии заново")
    coach.add_argument("--json", metavar="PATH", default=None, help="сохранить отчёт дополнительно в JSON")
    coach.add_argument("--no-save", action="store_true", help="не записывать партии и анализ в кеш")

    review = subparsers.add_parser("review", help="LLM-разбор ключевых моментов партий из кеша")
    review.add_argument("--user", default=None, help="ник на Lichess (обязателен, если нет --game)")
    review.add_argument("--game", default=None, help="id конкретной партии")
    review.add_argument("--max", type=int, default=3, help="сколько лучших (самых поучительных) партий взять, по умолчанию %(default)s")
    review.add_argument("--model", default=None, help="модель LLM (переопределяет LLM_MODEL)")
    review.add_argument("--key", default=None, help="API-ключ (переопределяет LLM_API_KEY)")
    review.add_argument("--moments", type=int, default=6, help="максимум ключевых моментов на партию, %(default)s")
    review.add_argument("--dry-run", action="store_true", help="не звать LLM: вывести готовые промпты")
    review.add_argument("--json", metavar="PATH", default=None, help="сохранить разбор дополнительно в JSON")

    drills = subparsers.add_parser("drills", help="дрели из своих ошибок (из кеша): PGN/JSON")
    drills.add_argument("--user", default=None, help="ник на Lichess")
    drills.add_argument("--game", default=None, help="id конкретной партии")
    drills.add_argument("--out", default=None, help="файл вывода (по расширению .pgn/.json); по умолчанию data/drills.pgn")
    drills.add_argument("--limit", type=int, default=0, help="максимум дрелей всего (0 = без лимита)")
    drills.add_argument("--max-per-game", type=int, default=8, help="максимум на партию (по умолчанию %(default)s)")
    drills.add_argument("--min-drop", type=float, default=15.0, help="мин. потеря win%% для дрели (по умолчанию %(default)s)")
    drills.add_argument("--min-win", type=float, default=50.0, help="мин. win%% до хода (по умолчанию %(default)s)")
    drills.add_argument("--humanity", default=None, help="JSON-файл с человечностью (по умолчанию data/humanity.json)")
    drills.add_argument("--verdict", nargs="+", default=None, choices=["natural", "borderline", "unnatural"], help="оставить только эти вердикты (нужно --humanity/данные)")

    humanize = subparsers.add_parser("humanize", help="оценка «человечности» ошибок (Maia/MaiaLite)")
    humanize.add_argument("--user", default=None, help="ник на Lichess")
    humanize.add_argument("--game", default=None, help="id конкретной партии")
    humanize.add_argument("--engine", default="maia-lite", choices=["maia-lite", "maia"], help="режим человечности (по умолчанию %(default)s)")
    humanize.add_argument("--maia-path", default=None, help="путь к бинарю Maia (MAIA_PATH) для --engine maia")
    humanize.add_argument("--temperature", type=float, default=15.0, help="температура MaiaLite (по умолчанию %(default)s)")
    humanize.add_argument("--out", default=None, help="куда писать JSON с результатами (по умолчанию data/humanity.json)")

    repertoire = subparsers.add_parser("repertoire", help="личный дебютный репертуар по своим партиям (из кеша)")
    repertoire.add_argument("--user", default=None, help="ник на Lichess")
    repertoire.add_argument("--game", default=None, help="id конкретной партии")
    repertoire.add_argument("--color", default="both", choices=["white", "black", "both"], help="цвет роли (по умолчанию %(default)s)")
    repertoire.add_argument("--max-depth", type=int, default=16, help="глубина дерева репертуара, ходов пользователя (по умолчанию %(default)s)")
    repertoire.add_argument("--min-count", type=int, default=1, help="мин. число партий для строки (по умолчанию %(default)s)")
    repertoire.add_argument("--opening", nargs="+", default=None, help="фрагменты имён дебютов для фильтра (регистронезависимо)")
    repertoire.add_argument("--pgn", default=None, help="куда писать PGN (для both — объединение цветов); по умолчанию data/repertoire_{цвет}.pgn")
    repertoire.add_argument("--json", metavar="PATH", default=None, help="куда писать JSON (по умолчанию data/repertoire.json)")
    repertoire.add_argument("--no-write", action="store_true", help="не писать файлы PGN/JSON, только печать")

    overview = subparsers.add_parser(
        "overview", help="сводка тренера из кеша: ветви репертуара, слабые дебюты, человечность, дрели (без движка и сети)"
    )
    overview.add_argument("--user", default=None, help="ник на Lichess")
    overview.add_argument("--game", default=None, help="id конкретной партии")
    overview.add_argument("--humanity", default=None, help="JSON человечности (по умолчанию data/humanity.json)")
    overview.add_argument("--drills-dir", default=None, help="каталог с дрелями (по умолчанию settings.data_dir)")

    plan = subparsers.add_parser(
        "plan",
        help="план тренировки из кеша: ветви репертуара, слабые дебюты, узоры, человечность, дрели (без движка)",
    )
    plan.add_argument("--user", default=None, help="ник на Lichess")
    plan.add_argument("--game", default=None, help="id конкретной партии")
    plan.add_argument("--humanity", default=None, help="JSON человечности (по умолчанию data/humanity.json)")
    plan.add_argument("--drills-dir", default=None, help="каталог с дрелями (по умолчанию settings.data_dir)")
    plan.add_argument("--max-depth", type=int, default=16, help="глубина дерева репертуара, ходов пользователя (по умолчанию %(default)s)")

    mentor = subparsers.add_parser(
        "mentor",
        help="LLM-тренер: опиши стиль игры и составь план с учётом доп. заметок игрока (OpenRouter/LLM)",
    )
    mentor.add_argument("--user", default=None, help="ник на Lichess")
    mentor.add_argument("--game", default=None, help="id конкретной партии")
    mentor.add_argument("--humanity", default=None, help="JSON человечности (по умолчанию data/humanity.json)")
    mentor.add_argument("--drills-dir", default=None, help="каталог с дрелями (по умолчанию settings.data_dir)")
    mentor.add_argument("--max-depth", type=int, default=16, help="глубина дерева репертуара, ходов пользователя (по умолчанию %(default)s)")
    mentor.add_argument("--notes", default=None, help="твои доп. заметки/дополнительные вещи, которые должна учесть модель (цели, слабые места, время на тренировки и т.п.)")
    mentor.add_argument("--model", default=None, help="модель LLM (переопределяет LLM_MODEL)")
    mentor.add_argument("--key", default=None, help="API-ключ (переопределяет LLM_API_KEY)")
    mentor.add_argument("--base-url", default=None, help="базовый URL API (переопределяет LLM_BASE_URL)")
    mentor.add_argument("--timeout", type=float, default=180.0, help="таймаут запроса к LLM, сек (по умолчанию %(default)s)")
    mentor.add_argument("--no-progress", action="store_true", help="не включать тренд динамики в запрос тренера")
    mentor.add_argument("--dry-run", action="store_true", help="не звать LLM: вывести готовый промпт")
    mentor.add_argument("--json", metavar="PATH", default=None, help="сохранить ответ тренера дополнительно в JSON")

    progress = subparsers.add_parser(
        "progress", help="динамика прогресса по временным окнам (из кеша, без движка и сети)"
    )
    progress.add_argument("--user", default=None, help="ник на Lichess")
    progress.add_argument("--game", default=None, help="id конкретной партии")
    progress.add_argument("--humanity", default=None, help="JSON человечности (по умолчанию data/humanity.json)")
    progress.add_argument("--windows", type=int, default=5, help="число временных окон (по умолчанию %(default)s)")
    progress.add_argument("--json", metavar="PATH", default=None, help="сохранить динамику дополнительно в JSON")

    fide = subparsers.add_parser(
        "fide", help="профиль и тренд рейтинга FIDE (кеш data/fide.json или сеть)"
    )
    fide.add_argument(
        "--id", default=None, help="FIDE ID (по умолчанию — значения из настроек)"
    )
    fide.add_argument(
        "--windows", type=int, default=4, help="число временных окон (по умолчанию %(default)s)"
    )
    fide.add_argument(
        "--json", metavar="PATH", default=None,
        help="сохранить справку о FIDE дополнительно в JSON",
    )

    tournament = subparsers.add_parser(
        "tournament", help="итоги OTB-турнира: очки, перформанс, прирост рейтинга"
    )
    tournament.add_argument(
        "--games",
        metavar="ОППОНЕНТ:ЦВЕТ:РЕЗУЛЬТАТ[:РЕЙТИНГ_СОПЕРНИКА]",
        action="append",
        default=None,
        help=(
            "партия турнира: ОППОНЕНТ — имя, ЦВЕТ — white/black, "
            "РЕЗУЛЬТАТ — win/draw/loss, РЕЙТИНГ_СОПЕРНИКА — число (опционально); "
            "флаг можно повторять"
        ),
    )
    tournament.add_argument(
        "--initial", type=int, default=None,
        help="ваш рейтинг до турнира для прироста (K=20); без него прирост не считается",
    )
    tournament.add_argument(
        "--json", metavar="PATH", default=None,
        help="сохранить итоги турнира дополнительно в JSON",
    )
    return parser


def _load_games(db: Database, args: argparse.Namespace) -> list[Game]:
    """Загружает партии: из кеша (--cached-only) или с Lichess + сохранение в кеш."""
    if args.cached_only:
        games = db.get_games(args.user, args.max)
        print(f"Режим --cached-only: загружено {len(games)} партий из кеша.")
        return games
    games = fetch_user_games(
        args.user, since_ts=args.since, max_games=args.max, perf=args.perf
    )
    if args.no_save:
        print(f"С Lichess получено {len(games)} партий (кеш не обновляю: --no-save).")
    else:
        new_games = db.save_games(games, args.user)
        print(f"С Lichess получено {len(games)} партий, новых в кеше: {new_games}.")
    return games


def _analyze(
    db: Database, engine: Engine, target: list[Game], args: argparse.Namespace
) -> dict[str, dict]:
    """Анализирует партии движком, сохраняет результаты и возвращает их по id."""
    results: dict[str, dict] = {}
    started = time.time()
    for index, game in enumerate(target, start=1):
        began = time.time()
        analysis = engine.analyze_game(game)
        summary = analysis.summary()
        results[game.id] = summary
        if not args.no_save:
            db.save_analysis(game.id, summary, depth=args.depth)
        print(f"[{index}/{len(target)}] {game.id} ... за {time.time() - began:.1f} с", flush=True)
    print(f"Анализ {len(target)} партий занял {time.time() - started:.1f} с.")
    return results


def _collect_pairs(
    db: Database,
    games: list[Game],
    fresh: dict[str, dict],
) -> list[tuple[Game, dict]]:
    """Собирает пары (Game, analysis) в порядке партий.

    Сначала берутся только что посчитанные результаты, затем — кеш
    (запись управляется отдельно, чтение кеша разрешено всегда).
    """
    pairs: list[tuple[Game, dict]] = []
    for game in games:
        summary = fresh.get(game.id)
        if summary is None:
            summary = db.get_analysis(game.id)
        if summary is not None:
            pairs.append((game, summary))
    return pairs


def _dump_json(report: dict, path: str) -> None:
    """Записывает отчёт в JSON-файл (utf-8, без экранирования юникода)."""
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Отчёт сохранён в JSON: {path}")


_RESULT_RU = {"win": "победа", "draw": "ничья", "loss": "поражение"}


def _player_name(player: dict | None) -> str:
    """Имя игрока из словаря white/black партии, иначе '—'."""
    if not player:
        return "—"
    user = player.get("user")
    if isinstance(user, dict) and user.get("name"):
        return str(user["name"])
    name = player.get("name") or player.get("username")
    return str(name) if name else "—"


def _print_wrapped(text: str, width: int = 90, indent: str = "  ") -> None:
    """Печатает текст с переносом по строкам заданной ширины."""
    for line in textwrap.wrap(text, width=width) if text else []:
        print(indent + line)


def _review_header(game: Game, analysis: dict) -> str:
    """Компактный заголовок партии для вывода review."""
    result = _RESULT_RU.get(
        analysis.get("result_for_user") or game.result_for_user or "draw"
    )
    acpl = analysis.get("acpl")
    acpl_txt = f"{acpl:.1f}" if isinstance(acpl, (int, float)) else "—"
    return (
        f"{game.id} ({_player_name(game.white)}—{_player_name(game.black)}, "
        f"{result}, {game.opening or '—'}, ACPL {acpl_txt})"
    )


def _select_review_games(db: Database, args: argparse.Namespace) -> list[tuple[Game, dict]]:
    """Отбирает партии для разбора: по id или лучшие по «поучительности»."""
    if args.game:
        game = db.get_game(args.game)
        if game is None:
            raise RuntimeError(f"Партия {args.game} не найдена в кеше")
        analysis = db.get_analysis(args.game)
        if analysis is None:
            raise RuntimeError(
                f"Партия {args.game} не проанализирована — сначала прогони "
                "coach, напр.: python -m trainer coach --user <NICK>"
            )
        return [(game, analysis)]
    if not args.user:
        raise RuntimeError("Укажи --user или --game")
    pairs = db.get_analyzed_games(args.user, limit=500)
    if not pairs:
        raise RuntimeError(
            f"Нет проанализированных партий для {args.user}. "
            "Запусти сначала: python -m trainer coach --user <NICK>"
        )
    pairs.sort(
        key=lambda p: (
            -(len(p[1].get("blunders", [])) + len(p[1].get("mistakes", []))),
            -p[1].get("acpl", 0.0),
        )
    )
    return pairs[: args.max]


def _review_json_entry(
    game: Game, analysis: dict, idx: list[int], moments: dict[int, str], summary: str | None
) -> dict:
    """Собирает словарь с разбором партии для JSON-выгрузки."""
    moves = analysis.get("moves") or []
    entry_moments: list[dict] = []
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
        "opening": game.opening,
        "acpl": analysis.get("acpl"),
        "moments": entry_moments,
        "summary": summary,
    }


def _cmd_review(args: argparse.Namespace) -> int:
    """Исполняет подкоманду review: LLM-разбор ключевых моментов из кеша."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _select_review_games(db, args)

            llm: LLMClient | None = None
            if not args.dry_run:
                llm = (
                    LLMClient(model=args.model, api_key=args.key)
                    if args.model
                    else LLMClient(api_key=args.key)
                )

            json_games: list[dict] = []
            for game, analysis in pairs:
                messages, idx = build_request(game, analysis, args.moments)
                if not messages:
                    print(f"[{game.id}] нет ключевых моментов")
                    continue

                header = _review_header(game, analysis)
                if args.dry_run:
                    print(f"=== {header} промпты ===")
                    for message in messages:
                        print(f"-- {message.role}:")
                        _print_wrapped(message.content)
                    json_games.append(_review_json_entry(game, analysis, idx, {}, None))
                    continue

                moments, summary = run_coach(
                    game, analysis, llm, max_moments=args.moments
                )
                print(header)
                moves = analysis.get("moves") or []
                for number, ply in enumerate(idx, start=1):
                    move = moves[ply] if ply < len(moves) else {}
                    drop = move.get("drop")
                    drop_txt = f"{drop:.1f}%" if isinstance(drop, (int, float)) else "—"
                    color = "белые" if ply % 2 == 0 else "чёрные"
                    print(
                        f"Момент {number} (полуход {ply + 1}, {color}): "
                        f"{move.get('san') or '—'} "
                        f"[{move.get('classification') or '—'}, потеря {drop_txt}]"
                    )
                    text = moments.get(ply)
                    if text:
                        _print_wrapped(text)
                    else:
                        print("  (модель не дала объяснения)")
                if summary:
                    print("Итог:")
                    _print_wrapped(summary)
                else:
                    print("Итог: (модель не дала итога)")
                json_games.append(
                    _review_json_entry(game, analysis, idx, moments, summary)
                )

            if args.json and json_games:
                _dump_json({"games": json_games}, args.json)
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_coach(args: argparse.Namespace) -> int:
    """Исполняет подкоманду coach: fetch -> анализ -> отчёт."""
    try:
        with Database() as db:
            db.init_db()
            if args.game:
                if not args.user:
                    args.user = "?"
                ids = list(args.game)
                games = []
                missing = [gid for gid in ids if db.get_game(gid) is None]
                if missing:
                    raise RuntimeError(
                        f"Партии не найдены в кеше: {', '.join(missing)}. "
                        "Сначала загрузи их: python -m trainer coach --user <NICK>"
                    )
                for gid in ids:
                    game = db.get_game(gid)
                    if game is not None:
                        games.append(game)
                print(
                    f"Точечный переанализ {len(games)} партий (пользователь {args.user}, "
                    f"глубина {args.depth}, multipv {args.multipv}) — движок работает, "
                    "перезапись кеша для этих партий."
                )
            else:
                if not args.user:
                    raise RuntimeError("Укажи --user или --game")
                games = _load_games(db, args)

            qualified = [game for game in games if game.is_finished() and game.moves]
            skipped = len(games) - len(qualified)
            if skipped:
                print(f"Пропущено незавершённых или пустых партий: {skipped}.")

            if args.refresh or args.game:
                target = list(qualified)
                if not args.game:
                    print(f"Режим --refresh: переанализирую {len(target)} партий с перезаписью кеша.")
            else:
                game_ids = [game.id for game in qualified]
                unanalyzed = set(db.get_unanalyzed(game_ids))
                target = [game for game in qualified if game.id in unanalyzed]

            fresh: dict[str, dict] = {}
            if target:
                with Engine(depth=args.depth, multipv=args.multipv) as engine:
                    fresh = _analyze(db, engine, target, args)
            else:
                print("Все загруженные партии уже проанализированы, анализ не требуется.")

            pairs = _collect_pairs(db, qualified, fresh)
            if not pairs:
                print("Нет партий с анализом в кеше.")
                print("Если использован --cached-only — запусти без него, "
                      "чтобы подтянуть партии с Lichess и проанализировать их.")
                return 1

            report = build_report(pairs, args.user)
            if args.json:
                _dump_json(report, args.json)
            print(format_report(report))
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_drills(args: argparse.Namespace) -> int:
    """Исполняет подкоманду drills: дрели из своих ошибок по кешу анализа.

    Работает только по кешу: движок не запускается, сеть не используется.
    """
    try:
        with Database() as db:
            db.init_db()
            if args.game:
                game = db.get_game(args.game)
                if game is None:
                    raise RuntimeError(
                        f"Партия {args.game} не найдена в кеше "
                        "(сначала: python -m trainer coach --user NICK)"
                    )
                analysis = db.get_analysis(args.game)
                if analysis is None:
                    raise RuntimeError(
                        f"Партия {args.game} не проанализирована — сначала прогони "
                        "coach, напр.: python -m trainer coach --user <NICK>"
                    )
                pairs = [(game, analysis)]
            elif args.user:
                pairs = db.get_analyzed_games(args.user, limit=200)
                if not pairs:
                    raise RuntimeError(
                        f"Нет проанализированных партий для {args.user}. "
                        "Запусти сначала: python -m trainer coach --user <NICK>"
                    )
            else:
                raise RuntimeError("Укажи --user или --game")

            humanity_items: list[dict] | None = None
            allowed_verdicts: set[str] | None = None
            if args.verdict:
                humanity_path = (
                    Path(args.humanity)
                    if args.humanity
                    else settings.data_dir / "humanity.json"
                )
                if not humanity_path.exists():
                    raise RuntimeError(
                        f"Файл человечности не найден: {humanity_path}. "
                        "Сначала: python -m trainer humanize --user NICK"
                    )
                data = json.loads(humanity_path.read_text(encoding="utf-8"))
                humanity_items = (
                    data.get("items") if isinstance(data, dict) else None
                )
                allowed_verdicts = set(args.verdict)

            drills = []
            for game, analysis in pairs:
                drills.extend(
                    collect_drills(
                        game,
                        analysis,
                        min_drop=args.min_drop,
                        min_win=args.min_win,
                        max_per_game=args.max_per_game,
                        humanity=humanity_items,
                        allowed_verdicts=allowed_verdicts,
                    )
                )
            if args.limit and args.limit > 0:
                drills = drills[: args.limit]

            if not drills:
                print(
                    f"Дрелей не найдено: нет ошибок с потерями >= {args.min_drop}% "
                    f"и win% >= {args.min_win}%. Рекомендации: уменьши "
                    "--min-drop/--min-win, или проанализируй больше партий."
                )
                return 0

            out = Path(args.out) if args.out else settings.data_dir / "drills.pgn"
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.suffix.lower() == ".json":
                out.write_text(drills_to_json(drills), encoding="utf-8")
            else:
                out.write_text(drills_to_pgn(drills), encoding="utf-8")

            summary = summarize(drills)
            print(f"Дрелей: {summary['found']} из {summary['games']} партий → {out}")
            avg = f"{summary['avg_drop']:.1f}" if summary["avg_drop"] is not None else "—"
            print(f"Средняя потеря win%: {avg}")
            for item in summary["top"]:
                print(
                    f"  [{item['game_id']}] сыграно {item['san']} → лучше {item['best']}"
                )
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_humanize(args: argparse.Namespace) -> int:
    """Исполняет подкоманду humanize: оценка «человечности» ошибок (Maia).

    Прогоняет ошибки игрока через политику Maia/MaiaLite и пишет JSON-файл
    с вердиктами по каждому ходу, который затем фильтрует команда drills.
    """
    try:
        from app.humanize import VERDICT_LABELS, humanize_report
        from app.maia import (
            EngineNotConfiguredError,
            MaiaBinaryPolicy,
            MaiaLitePolicy,
        )
    except ImportError:
        print("Ошибка: ядро M3 ещё не собрано (app.humanize) — повтори позже")
        return 1

    try:
        with Database() as db:
            db.init_db()
            pairs: list[tuple[Game, dict]] = []
            if args.game:
                game = db.get_game(args.game)
                if game is None:
                    raise RuntimeError(f"Партия {args.game} не найдена в кеше")
                analysis = db.get_analysis(args.game)
                if analysis is None:
                    raise RuntimeError(
                        f"Партия {args.game} не проанализирована — сначала "
                        "прогони coach, напр.: python -m trainer coach --user <NICK>"
                    )
                pairs = [(game, analysis)]
            elif args.user:
                pairs = db.get_analyzed_games(args.user, limit=500)
                if not pairs:
                    raise RuntimeError(
                        f"Нет проанализированных партий для {args.user}. "
                        "Запусти сначала: python -m trainer coach --user <NICK>"
                    )
            else:
                raise RuntimeError("Укажи --user или --game")

            if args.engine == "maia":
                try:
                    policy: MaiaLitePolicy | MaiaBinaryPolicy = MaiaBinaryPolicy(
                        path=args.maia_path
                    )
                except EngineNotConfiguredError as exc:
                    print(f"{exc} → используется MaiaLite.")
                    policy = MaiaLitePolicy(temperature=args.temperature)
            else:
                policy = MaiaLitePolicy(temperature=args.temperature)

            with policy as engine:
                report = humanize_report(pairs, engine, k=8)

        out = Path(args.out) if args.out else settings.data_dir / "humanity.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        items = report.get("items") or []
        print(f"Человечность рассчитана: {len(items)} ошибок → {out}")
        for item in items:
            verdict = item.get("verdict")
            label = VERDICT_LABELS.get(verdict, verdict or "—")
            ply = item.get("ply")
            ply_txt = f"{ply + 1}" if isinstance(ply, int) else "—"
            san = item.get("san") or "—"
            classification = item.get("classification") or "—"
            drop = item.get("drop")
            drop_txt = f"{drop:>5}" if isinstance(drop, (int, float)) else "–"
            prob_user = item.get("prob_user")
            prob_user_txt = (
                f"{prob_user:.2f}" if isinstance(prob_user, (int, float)) else "–"
            )
            prob_best = item.get("prob_best")
            prob_best_txt = (
                f"{prob_best:.2f}" if isinstance(prob_best, (int, float)) else "–"
            )
            beta = item.get("beta")
            beta_txt = f"{beta:+.2f}" if isinstance(beta, (int, float)) else "–"
            print(
                f"  {item.get('game_id') or '—'}  п.{ply_txt} "
                f"{san:<8} {classification:<10} drop {drop_txt}%  "
                f"P(я)={prob_user_txt} P(лучш)={prob_best_txt} "
                f"β={beta_txt} — {label}"
            )
        counts = {"natural": 0, "borderline": 0, "unnatural": 0, "no-data": 0}
        for item in items:
            verdict = item.get("verdict")
            counts[verdict if verdict in counts else "no-data"] += 1
        print(
            "natural/borderline/unnatural/no-data = "
            f"{counts['natural']}/{counts['borderline']}/"
            f"{counts['unnatural']}/{counts['no-data']}"
        )
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _repertoire_pairs(db: Database, args: argparse.Namespace) -> list[tuple[Game, dict]]:
    """Загружает пары (Game, analysis) для репертуара из кеша.

    Работает только по кешу: движок не запускается, сеть не используется.
    """
    if args.game:
        game = db.get_game(args.game)
        if game is None:
            raise RuntimeError(f"Партия {args.game} не найдена в кеше")
        analysis = db.get_analysis(args.game)
        if analysis is None:
            raise RuntimeError(
                f"Партия {args.game} не проанализирована — сначала прогони "
                "coach, напр.: python -m trainer coach --user <NICK>"
            )
        return [(game, analysis)]
    if not args.user:
        raise RuntimeError("Укажи --user или --game")
    pairs = db.get_analyzed_games(args.user, limit=200)
    if not pairs:
        raise RuntimeError(
            f"Нет проанализированных партий для {args.user}. "
            "Запусти сначала: python -m trainer coach --user <NICK>"
        )
    return pairs


def _print_opening_stats_table(stats: list[dict], *, limit: int = 12) -> None:
    """Печатает таблицу «Слабые места репертуара» (топ-`limit` по ошибкам)."""
    if not stats:
        print("Слабые места репертуара: нет данных.")
        return
    print("Слабые места репертуара:")
    print(
        "  {:<38} {:>5} {:>7} {:>11} {:>11}".format(
            "Название", "игр", "оч%", "зев·ош/игру", "ср.потери%"
        )
    )
    for row in stats[:limit]:
        print(
            "  {:<38} {:>5} {:>6.0f}% {:>12.2f} {:>10.1f}%".format(
                row["opening"],
                row["games"],
                row["points_pct"],
                row["errors_per_game"],
                row["avg_drop"],
            )
        )


def _run_repertoire(
    pairs: list[tuple[Game, dict]], args: argparse.Namespace
) -> dict:
    """Вся логика подкоманды repertoire: деревья линий, слабости, запись файлов."""
    repertoire = Repertoire()
    for game, analysis in pairs:
        repertoire.add_game(game, analysis, max_depth=args.max_depth)

    colors = ["white", "black"] if args.color == "both" else [args.color]
    line_counts: dict[str, int] = {}
    for color in colors:
        lines = repertoire.lines(color, min_count=args.min_count)
        line_counts[color] = len(lines)
        print(format_lines(color, lines, limit=60))
        weak = repertoire.weak_lines(color, min_count=args.min_count)
        total = len(weak)
        print(f"Слабых линий: {total} (прочность < 70%)" if total else "Слабых линий: нет")

    stats = opening_stats(pairs, user=args.user)
    if args.opening:
        wanted = [token.strip().lower() for token in args.opening if token.strip()]
        stats = [
            row
            for row in stats
            if any(token in row["opening"].lower() for token in wanted)
        ]

    if stats:
        _print_opening_stats_table(stats)
    else:
        print("Слабые места репертуара: нет данных.")

    if not args.no_write:
        out_dir = settings.data_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        if args.pgn:
            path = Path(args.pgn)
            chunks = [
                repertoire_to_pgn(pairs, color, opening_filter=args.opening)
                for color in colors
            ]
            path.write_text("".join(chunks), encoding="utf-8")
            written.append(path)
        else:
            for color in colors:
                path = out_dir / f"repertoire_{color}.pgn"
                path.write_text(
                    repertoire_to_pgn(pairs, color, opening_filter=args.opening),
                    encoding="utf-8",
                )
                written.append(path)
        json_path = Path(args.json) if args.json else out_dir / "repertoire.json"
        json_path.write_text(repertoire_to_json(repertoire, pairs), encoding="utf-8")
        print(f"Репертуар записан → {' + '.join(str(p) for p in written)}")
        print(f"JSON → {json_path}")

    return {
        "white": line_counts.get("white", 0),
        "black": line_counts.get("black", 0),
        "openings": len(stats),
    }


def _cmd_repertoire(args: argparse.Namespace) -> int:
    """Исполняет подкоманду repertoire: репертуар и слабые дебюты из кеша."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _repertoire_pairs(db, args)
            _run_repertoire(pairs, args)
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _load_humanity(path: Path) -> dict | None:
    """Читает data/humanity.json: возвращает {total, natural, borderline, unnatural} или None."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, dict):
        return None
    total = data.get("total_bad")
    if not isinstance(total, int):
        total = sum(
            int(v) for v in verdicts.values() if isinstance(v, (int, float))
        )
    counts = {
        label: int(verdicts.get(label, 0) or 0)
        for label in ("natural", "borderline", "unnatural")
    }
    return {"total": total, **counts}


def _count_pgn_games(path: Path) -> int:
    """Число партий в PGN-файле (по заголовкам [Event …])."""
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("[Event ")) if text.strip() else 0


def _run_overview(
    pairs: list[tuple[Game, dict]],
    *,
    user: str | None = None,
    humanity_path: Path | None = None,
    drills_dir: Path | None = None,
) -> dict:
    """Сводка из кеша без движка и сети: ветви, слабости, человечность, дрели."""
    repertoire = Repertoire()
    for game, analysis in pairs:
        repertoire.add_game(game, analysis)

    print(f"Сводка по кешу · партий: {len(pairs)}")
    print("\n--- Репертуар (частые ветви) ---")
    branch_counts: dict[str, int] = {}
    for color in ("white", "black"):
        candidates = [
            line
            for line in repertoire.lines(color, min_count=1)
            if len(line.moves) <= 4
        ]
        if not candidates:
            continue
        strong = [line for line in candidates if line.count >= 2]
        chosen = sorted(strong or candidates, key=lambda line: line.count, reverse=True)[:8]
        branch_counts[color] = len(chosen)
        print(format_lines(color, chosen, limit=8))

    stats = opening_stats(pairs, user=user)
    print("\n--- Слабые места репертуара ---")
    _print_opening_stats_table(stats, limit=8)

    humanity = _load_humanity(humanity_path) if humanity_path is not None else None
    print("\n--- Человечность ошибок ---")
    if humanity:
        print(
            "Всего плохих ходов: {total} · естественных: {natural} · "
            "пограничных: {borderline} · неестественных: {unnatural}".format(**humanity)
        )
    else:
        print(f"Нет файла {humanity_path} — запусти: python -m trainer humanize --user NICK")

    out_dir = drills_dir if drills_dir is not None else settings.data_dir
    drills_total = _count_pgn_games(out_dir / "drills.pgn")
    drills_unnatural = _count_pgn_games(out_dir / "drills_unnatural.pgn")
    print("\n--- Дрели ---")
    print(f"data/drills.pgn: {drills_total} партий · data/drills_unnatural.pgn: {drills_unnatural}")

    return {
        "games": len(pairs),
        "white_lines": branch_counts.get("white", 0),
        "black_lines": branch_counts.get("black", 0),
        "openings": len(stats),
        "humanity": humanity,
        "drills_total": drills_total,
        "drills_unnatural": drills_unnatural,
    }


def _cmd_overview(args: argparse.Namespace) -> int:
    """Исполняет подкоманду overview: сводка тренера из кеша."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _repertoire_pairs(db, args)
            drills_dir = Path(args.drills_dir) if args.drills_dir else None
            humanity_path = (
                Path(args.humanity) if args.humanity else settings.data_dir / "humanity.json"
            )
            _run_overview(
                pairs,
                user=args.user,
                humanity_path=humanity_path,
                drills_dir=drills_dir,
            )
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_plan(args: argparse.Namespace) -> int:
    """Исполняет подкоманду plan: план тренировки из кеша."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _repertoire_pairs(db, args)
            humanity = _load_humanity(
                Path(args.humanity) if args.humanity else settings.data_dir / "humanity.json"
            )
            drills_dir = Path(args.drills_dir) if args.drills_dir else settings.data_dir
            plan_report = build_plan(
                pairs,
                user=args.user,
                humanity=humanity,
                drills_total=_count_pgn_games(drills_dir / "drills.pgn"),
                drills_unnatural=_count_pgn_games(drills_dir / "drills_unnatural.pgn"),
                max_depth=args.max_depth,
            )
            print(format_plan(plan_report))
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_mentor(args: argparse.Namespace) -> int:
    """Исполняет подкоманду mentor: LLM-тренер, стиль игры и план с доп. заметками."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _repertoire_pairs(db, args)
            drills_dir = Path(args.drills_dir) if args.drills_dir else settings.data_dir
            humanity_path = Path(args.humanity) if args.humanity else settings.data_dir / "humanity.json"
            plan_report = build_plan(
                pairs,
                user=args.user,
                humanity=_load_humanity(humanity_path),
                drills_total=_count_pgn_games(drills_dir / "drills.pgn"),
                drills_unnatural=_count_pgn_games(drills_dir / "drills_unnatural.pgn"),
                max_depth=args.max_depth,
            )
            progress_report = None
            if not args.no_progress:
                progress_report = build_progress(
                    pairs,
                    user=args.user,
                    humanity=_load_humanity_raw(humanity_path),
                    windows=5,
                )
            request = build_mentor_request(plan_report, extra=args.notes or "", progress=progress_report)

            if args.dry_run:
                print("=== mentor: готовые промпты ===")
                for message in request:
                    print(f"-- {message.role}:")
                    _print_wrapped(message.content)
                return 0

            kwargs: dict = {}
            if args.model:
                kwargs["model"] = args.model
            if args.key:
                kwargs["api_key"] = args.key
            if args.base_url:
                kwargs["base_url"] = args.base_url
            llm = LLMClient(timeout=args.timeout, **kwargs)
            print(f"Модель: {llm.model} · базовый URL: {llm.base_url}")
            reply = run_mentor(llm, request)
            text = format_mentor_reply(reply)
            print("=== ответ тренера ===")
            _print_wrapped(text, width=90, indent="")
            if args.json:
                _dump_json(
                    {
                        "user": plan_report.get("user"),
                        "model": llm.model,
                        "notes": args.notes or "",
                        "reply": text,
                    },
                    args.json,
                )
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _load_humanity_raw(path: Path) -> dict | None:
    """Читает data/humanity.json целиком ({items: [...]}) или None, если нет."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _cmd_progress(args: argparse.Namespace) -> int:
    """Исполняет подкоманду progress: динамика прогресса по окнам из кеша."""
    try:
        with Database() as db:
            db.init_db()
            pairs = _repertoire_pairs(db, args)
            humanity = _load_humanity_raw(
                Path(args.humanity) if args.humanity else settings.data_dir / "humanity.json"
            )
            progress_report = build_progress(
                pairs,
                user=args.user,
                humanity=humanity,
                windows=args.windows,
            )
            print(format_progress(progress_report))
            if args.json:
                _dump_json(progress_report, args.json)
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_fide(args: argparse.Namespace) -> int:
    """Исполняет подкоманду fide: профиль и тренд рейтинга FIDE."""
    try:
        from dataclasses import asdict

        from app.fide import (
            build_fide_trend,
            fetch_fide_player,
            fetch_fide_ratings,
            format_fide_brief,
            format_fide_trend,
        )

        fide_id = str(args.id or settings.fide_id or "").strip()
        if not fide_id:
            print("Ошибка: не указан FIDE ID. Передайте --id или задайте FIDE_ID в .env.")
            return 1
        player = fetch_fide_player(fide_id)
        ratings = fetch_fide_ratings(fide_id)
        trend = build_fide_trend(ratings, windows=args.windows)
        if player.name:
            year = f", {player.birth_year}" if player.birth_year else ""
            print(f"Игрок: {player.name} ({player.federation}{year}) — FIDE {fide_id}")
        brief = format_fide_brief(trend, user_fide=fide_id)
        if brief:
            print(brief)
        print(format_fide_trend(trend, user_fide=fide_id))
        if args.json:
            _dump_json({"id": fide_id, "player": asdict(player), "trend": trend}, args.json)
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def _cmd_tournament(args: argparse.Namespace) -> int:
    """Исполняет подкоманду tournament: итоги OTB-турнира."""
    try:
        from app.fide import TournamentGame, build_tournament_report, format_tournament

        games: list[TournamentGame] = []
        for spec in args.games or []:
            parts = [part.strip() for part in spec.split(":")]
            if len(parts) < 3 or not parts[0]:
                print(
                    f"Ошибка: неверный формат партии: {spec!r} "
                    "(ожидается ОППОНЕНТ:ЦВЕТ:РЕЗУЛЬТАТ[:РЕЙТИНГ_СОПЕРНИКА])"
                )
                return 1
            opponent, color, result = parts[0], parts[1], parts[2]
            opponent_rating = None
            if len(parts) > 3 and parts[3]:
                try:
                    opponent_rating = int(parts[3])
                except ValueError:
                    print(f"Ошибка: рейтинг соперника не число: {parts[3]!r}")
                    return 1
            if color not in ("white", "black"):
                print(f"Ошибка: цвет должен быть white или black, получено: {color!r}")
                return 1
            if result not in ("win", "draw", "loss"):
                print(f"Ошибка: результат должен быть win/draw/loss, получено: {result!r}")
                return 1
            games.append(
                TournamentGame(
                    opponent=opponent,
                    color=color,
                    result=result,
                    opponent_rating=opponent_rating,
                )
            )
        report = build_tournament_report(games, initial_rating=args.initial)
        print(format_tournament(report))
        if args.json:
            _dump_json(report, args.json)
        return 0
    except RuntimeError as exc:
        print(f"Ошибка: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as exc:
        print(f"Непредвиденная ошибка: {exc!r}")
        return 1


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: разбор аргументов и диспетчеризация подкоманд."""
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "coach":
        return _cmd_coach(args)
    if args.command == "review":
        return _cmd_review(args)
    if args.command == "drills":
        return _cmd_drills(args)
    if args.command == "humanize":
        return _cmd_humanize(args)
    if args.command == "repertoire":
        return _cmd_repertoire(args)
    if args.command == "overview":
        return _cmd_overview(args)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "mentor":
        return _cmd_mentor(args)
    if args.command == "progress":
        return _cmd_progress(args)
    if args.command == "fide":
        return _cmd_fide(args)
    if args.command == "tournament":
        return _cmd_tournament(args)
    parser.error(f"Неизвестная команда: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())