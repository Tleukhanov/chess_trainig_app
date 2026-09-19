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
from app.games import Game, fetch_user_games
from app.llm import ChatMessage, LLMClient
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

    review = subparsers.add_parser("review", help="LLM-разбор ключевых моментов партий из кеша")
    review.add_argument("--user", default=None, help="ник на Lichess (обязателен, если нет --game)")
    review.add_argument("--game", default=None, help="id конкретной партии")
    review.add_argument("--max", type=int, default=3, help="сколько лучших (самых поучительных) партий взять, по умолчанию %(default)s")
    review.add_argument("--model", default=None, help="модель LLM (переопределяет LLM_MODEL)")
    review.add_argument("--key", default=None, help="API-ключ (переопределяет LLM_API_KEY)")
    review.add_argument("--moments", type=int, default=6, help="максимум ключевых моментов на партию, %(default)s")
    review.add_argument("--dry-run", action="store_true", help="не звать LLM: вывести готовые промпты")
    review.add_argument("--json", metavar="PATH", default=None, help="сохранить разбор дополнительно в JSON")
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
    if args.command == "review":
        return _cmd_review(args)
    parser.error(f"Неизвестная команда: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())