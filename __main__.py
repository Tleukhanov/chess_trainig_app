"""CLI шахматного тренера.

Подкоманда ``coach`` выгружает партии с Lichess, прогоняет их через
Stockfish и строит русскоязычный текстовый отчёт с ошибками игрока.

Примеры:
    python -m trainer coach --user NICK
    python -m trainer coach --user NICK --max 10 --perf rapid --depth 14 --multipv 3
    python -m trainer coach --user NICK --cached-only
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.analyzer import Engine
from app.config import settings
from app.db import Database
from app.games import Game, fetch_user_games
from app.report import build_report, format_report

__all__ = ["main"]


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trainer",
        description="Chess Trainer — персональный шахматный тренер",
    )
    parser.add_argument("--version", action="version", version="chess-trainer 0.1.0")
    subparsers = parser.add_subparsers(dest="command", metavar="КОМАНДА")

    coach = subparsers.add_parser("coach", help="анализ партий и построение отчёта")
    coach.add_argument("--user", required=True, help="ник на Lichess")
    coach.add_argument("--max", type=int, default=settings.games_max, help="сколько партий взять (по умолчанию %(default)s)")
    coach.add_argument("--perf", default="rapid", help="пул партий: rapid/classical/blitz/...")
    coach.add_argument("--depth", type=int, default=settings.stockfish_depth, help="глубина анализа Stockfish")
    coach.add_argument("--multipv", type=int, default=settings.stockfish_multipv, help="число анализируемых линий")
    coach.add_argument("--since", type=int, default=None, help="брать партии с этого времени (unix, миллисекунды)")
    coach.add_argument("--cached-only", action="store_true", help="не ходить в сеть, брать только кеш")
    coach.add_argument("--refresh", action="store_true", help="переанализировать все партии заново")
    coach.add_argument("--json", metavar="PATH", default=None, help="сохранить отчёт дополнительно в JSON")
    coach.add_argument("--no-save", action="store_true", help="не записывать партии и анализ в кеш")
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


def _cmd_coach(args: argparse.Namespace) -> int:
    """Исполняет подкоманду coach: fetch -> анализ -> отчёт."""
    try:
        with Database() as db:
            db.init_db()
            games = _load_games(db, args)

            qualified = [game for game in games if game.is_finished() and game.moves]
            skipped = len(games) - len(qualified)
            if skipped:
                print(f"Пропущено незавершённых или пустых партий: {skipped}.")

            if args.refresh:
                target = list(qualified)
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


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: разбор аргументов и диспетчеризация подкоманд."""
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "coach":
        return _cmd_coach(args)
    parser.error(f"Неизвестная команда: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())