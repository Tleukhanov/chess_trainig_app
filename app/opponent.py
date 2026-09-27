"""Подготовка к конкретному сопернику по его собственным партиям.

Идея в том, что «анализ соперника» сам по себе бесполезен: знать, что он
теряет 11% win% в Сицилианской, игроку ничего не даёт. Полезна связка его
дебютов с твоим репертуаром — «вот что он играет, вот чем ты отвечаешь,
вот где он спотыкается».

Поэтому модуль строит три вещи:

* :func:`build_opponent_profile` — профиль соперника по его парам
  (очки, рейтинг, цвета, контроль времени, дебюты, слабые фазы, тренд);
* :func:`build_confrontations` — точки встречи: его дебют с твоим ответом;
* :func:`format_prep` — текстовый лист подготовки.

Плюс тренировочная часть — :func:`build_opponent_drills`: дрели под матч
с этим соперником (его ошибки как твои наказания + твои провалы против
него) и :func:`format_opponent_drills` — лист задач.

Дебюты сопоставляются по семействам, а не по полному названию и не по
линиям репертуара. Три причины:

* в ``Repertoire`` сохраняются только ходы игрока, поэтому классифицировать
  по ним дебют нельзя — нужны партии целиком;
* полное название у двух игроков почти никогда не совпадает: на реальном
  кеше «Sicilian Defense: Smith-Morra Gambit Accepted» и «Sicilian
  Defense: O'Kelly Variation, Taimanov Line» — это одна и та же
  Сицилианская с точностью до варианта, и по полному имени пересечение
  не находится вовсе;
* для подготовки «Сицилианская» полезнее, чем «Сицилианская, вариант
  такой-то»: ответ нужен на уровне системы, в которую он играет.

Семейство — часть названия до двоеточия. Цвет берётся противоположный:
он белыми играет дебют X — значит встречаться будешь ты чёрными.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .drills import Drill, collect_drills
from .games import Game
from .patterns import PHASE_LABELS, weakness_stats
from .progress import build_progress
from .repertoire import UNKNOWN_OPENING, Repertoire, _plural_games

__all__ = [
    "DRILL_FOCUSES",
    "PREP_MIN_GAMES",
    "opening_family",
    "games_against",
    "build_opponent_profile",
    "build_confrontations",
    "format_prep",
    "build_opponent_drills",
    "format_opponent_drills",
]

#: Ниже этого числа партий выводы о репертуаре соперника шумные,
#: и лист подготовки об этом предупреждает.
PREP_MIN_GAMES = 20

_COLOR_RU = {"white": "белыми", "black": "чёрными"}
#: Подписи тренда намеренно говорят «игра», а не просто «растёт/падает»:
#: тренд считается по качеству ходов (потеря win%), а рядом печатается
#: рейтинг, который может двигаться в другую сторону.
_TREND_LABELS = {
    "improving": "игра улучшается",
    "worsening": "игра ухудшается",
    "flat": "без динамики",
    "mixed": "динамика смешанная",
}
_ERROR_CLASSES = frozenset({"blunder", "mistake"})

#: Бакет ходов без распознанного мотива: в лист подготовки он бесполезен,
#: поэтому в выдачу не попадает (он почти всегда самый частый).
_MOTIF_OTHER = "прочее"

#: Ответом считается ход, встретившийся хотя бы столько раз: единичное
#: повторение ничего не говорит о надёжности.
_MIN_REPLY_COUNT = 2

#: Разница в ошибках на партию, меньше которой силы сторон считаются равными:
#: различать «0.02 против 0.03» на 10 партиях — выдуманная точность.
_ERRORS_TIE = 0.25


def opening_family(opening: str) -> str:
    """Семейство дебюта: часть названия до двоеточия.

    «Sicilian Defense: O'Kelly Variation, Taimanov Line» -> «Sicilian Defense».
    Название без двоеточия остаётся само собой.
    """
    head = opening.split(":", 1)[0].strip()
    return head or opening


def _score_pct(pairs: list[tuple[Game, dict]]) -> float:
    if not pairs:
        return 0.0
    points = sum(game.user_result_points() for game, _ in pairs)
    return round(points / len(pairs) * 100.0, 1)


def _rating_delta(pairs: list[tuple[Game, dict]]) -> tuple[int | None, int | None, int | None]:
    """Рейтинг в первой партии, в последней и изменение между ними."""
    ratings = [game.user_rating for game, _ in pairs if isinstance(game.user_rating, int)]
    if not ratings:
        return None, None, None
    first, last = ratings[0], ratings[-1]
    return first, last, last - first


def _count_by(items: list[str], limit: int = 3) -> list[tuple[str, int]]:
    """Самые частые значения списка: [(значение, число), ...] по убыванию."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]


def _by_color(pairs: list[tuple[Game, dict]], color: str) -> list[tuple[Game, dict]]:
    return [(game, analysis) for game, analysis in pairs if game.user_color == color]


def _user_errors(game: Game, analysis: dict) -> tuple[int, float]:
    """(число ошибок игрока в партии, сумма потерь win% на них)."""
    bad = 0
    drop_sum = 0.0
    user_is_white = game.user_color == "white"
    for ply, entry in enumerate(analysis.get("moves") or []):
        if not isinstance(entry, dict):
            continue
        if (ply % 2 == 0) != user_is_white:
            continue
        if entry.get("classification") not in _ERROR_CLASSES:
            continue
        bad += 1
        drop = entry.get("drop")
        if isinstance(drop, (int, float)) and not isinstance(drop, bool):
            drop_sum += float(drop)
    return bad, drop_sum


def _family_rows(
    pairs: list[tuple[Game, dict]],
    *,
    top: int | None = None,
) -> list[dict[str, Any]]:
    """Дебюты, собранные по семействам, по убыванию числа партий.

    Внутри семейства указывается главный вариант (самый частый) и сколько
    всего вариантов встречалось: «Sicilian Defense» с пятью разными
    продолжениями — это один тематический блок, а не пять дебютов.
    """
    groups: dict[str, dict[str, Any]] = {}
    for game, analysis in pairs:
        name = game.opening or UNKNOWN_OPENING
        if name == UNKNOWN_OPENING:
            continue
        group = groups.setdefault(
            opening_family(name),
            {
                "family": opening_family(name),
                "eco": game.eco,
                "games": 0,
                "points": 0.0,
                "bad_moves": 0,
                "sum_drop": 0.0,
                "variants": defaultdict(int),
            },
        )
        group["games"] += 1
        group["points"] += game.user_result_points()
        group["variants"][name] += 1
        bad, drop_sum = _user_errors(game, analysis)
        group["bad_moves"] += bad
        group["sum_drop"] += drop_sum

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        games = group["games"]
        bad = group["bad_moves"]
        variants = group["variants"]
        main_variant = max(variants.items(), key=lambda item: (item[1], item[0]))[0]
        rows.append(
            {
                "opening": group["family"],
                "variant": main_variant,
                "variants": len(variants),
                "eco": group["eco"],
                "games": games,
                "points_pct": round(group["points"] / games * 100.0, 1),
                "bad_moves": bad,
                "errors_per_game": round(bad / games, 2),
                "avg_drop": round(group["sum_drop"] / bad, 2) if bad else 0.0,
            }
        )

    rows.sort(key=lambda row: (-row["games"], row["opening"]))
    return rows if top is None else rows[:top]


def _my_replies(pairs: list[tuple[Game, dict]], color: str) -> dict[str, dict[str, Any]]:
    """Мой ответ в каждом моём дебюте: {семейство: сведения}.

    Ответ ищется только среди моих партий ЭТОГО ЖЕ семейства: глобально
    самый частый первый ход бессмыслен — на Сицилианской это будет c5, а в
    защите Каро-Канн другой.
    """
    by_family: dict[str, list[tuple[Game, dict]]] = defaultdict(list)
    for game, analysis in pairs:
        if game.user_color != color or not game.opening or game.opening == UNKNOWN_OPENING:
            continue
        by_family[opening_family(game.opening)].append((game, analysis))

    replies: dict[str, dict[str, Any]] = {}
    for family, group in by_family.items():
        lines = Repertoire()
        for game, analysis in group:
            lines.add_game(game, analysis, max_depth=2)
        candidates = [line for line in lines.lines(color, max_depth=1) if line.moves]
        if not candidates:
            continue
        best = max(candidates, key=lambda line: (line.count, line.moves[0]))
        replies[family] = {
            "reply": best.moves[0],
            "games": best.count,
            "ok_rate": best.ok_rate,
        }
    return replies


def build_opponent_profile(
    pairs: list[tuple[Game, dict]],
    opponent: str,
    *,
    top: int = 8,
    color: str = "both",
) -> dict[str, Any]:
    """Профиль соперника по его парам (партия, анализ).

    ``color`` ограничивает выборку ("white"/"black"/"both") — так можно
    посмотреть, как он играет, скажем, только чёрными. Пустой список партий
    не считается ошибкой: возвращается профиль с нулями и ``games: 0``, и
    вызывающий код сам решает, что делать дальше.
    """
    if color not in ("white", "black", "both"):
        raise ValueError(f"неизвестный цвет: {color!r}")
    chosen = pairs if color == "both" else _by_color(pairs, color)

    first, last, delta = _rating_delta(chosen)
    weakness = weakness_stats(chosen)
    white, black = _by_color(chosen, "white"), _by_color(chosen, "black")

    phases = [
        {
            "phase": phase,
            "label": PHASE_LABELS.get(phase, phase),
            "bad_moves": int(values.get("blunders", 0)) + int(values.get("mistakes", 0)),
            "avg_drop": values.get("avg_drop"),
        }
        for phase, values in (weakness["phases"] or {}).items()
    ]
    phases = [item for item in phases if item["bad_moves"]]
    phases.sort(key=lambda item: -item["bad_moves"])

    motifs = [
        {
            "motif": motif,
            "bad_moves": int(values.get("count", 0)),
            "avg_drop": values.get("avg_drop"),
        }
        for motif, values in (weakness["motifs"] or {}).items()
        if motif != _MOTIF_OTHER
    ]
    motifs = [item for item in motifs if item["bad_moves"]]
    motifs.sort(key=lambda item: -item["bad_moves"])

    progress = build_progress(chosen, user=opponent, windows=5)

    return {
        "opponent": opponent,
        "games": len(chosen),
        "score_pct": _score_pct(chosen),
        "colors": {"white": len(white), "black": len(black)},
        "speed": dict(_count_by([game.speed or "—" for game, _ in chosen])),
        "rating": {"first": first, "last": last, "delta": delta},
        "trend": _TREND_LABELS.get(progress["trend"].get("overall", ""), "—"),
        "openings": {
            "white": _family_rows(white, top=top),
            "black": _family_rows(black, top=top),
        },
        "weaknesses": {
            "phases": phases[:3],
            "motifs": motifs[:3],
            "total_bad": weakness["total_bad"],
        },
        "small_sample": len(chosen) < PREP_MIN_GAMES,
        "progress": progress,
    }


def build_confrontations(
    profile: dict[str, Any],
    your_pairs: list[tuple[Game, dict]],
    *,
    top: int = 8,
) -> list[dict[str, Any]]:
    """Точки встречи: его дебюты и твои ответы на них.

    Дебюты сопоставляются по семейству, цвет берётся противоположный: он
    белыми играет Сицилианскую — ты отвечаешь ей чёрными. Дебюты, по
    которым у тебя нет ответа, пропускаются: «его слабость» без твоего
    хода не даёт ничего готовить.
    """
    rows: list[dict[str, Any]] = []
    for his_color in ("white", "black"):
        your_color = "black" if his_color == "white" else "white"
        your_families = {
            row["opening"]: row
            for row in _family_rows(_by_color(your_pairs, your_color))
        }
        replies = _my_replies(_by_color(your_pairs, your_color), your_color)

        for his in profile["openings"][his_color]:
            mine = your_families.get(his["opening"])
            reply = replies.get(his["opening"])
            if mine is None or reply is None or reply["games"] < _MIN_REPLY_COUNT:
                continue
            rows.append(
                {
                    "opening": his["opening"],
                    "his_variant": his["variant"],
                    "his_variants": his["variants"],
                    "your_variant": mine["variant"],
                    "his_color": his_color,
                    "your_color": your_color,
                    "his_games": his["games"],
                    "his_score_pct": his["points_pct"],
                    "his_errors_per_game": his["errors_per_game"],
                    "his_avg_drop": his["avg_drop"],
                    "your_games": mine["games"],
                    "your_score_pct": mine["points_pct"],
                    "your_errors_per_game": mine["errors_per_game"],
                    "your_avg_drop": mine["avg_drop"],
                    "your_reply": reply["reply"],
                    "your_reply_games": reply["games"],
                    "your_reply_ok_rate": reply["ok_rate"],
                }
            )

    rows.sort(key=lambda row: (-row["his_games"], row["opening"]))
    return rows[:top]


def _opening_line(row: dict[str, Any]) -> str:
    """Одна строка по дебютному столкновению."""
    diff = row["your_errors_per_game"] - row["his_errors_per_game"]
    if abs(diff) < _ERRORS_TIE:
        verdict = "ошибок примерно поровну"
    elif diff > 0:
        verdict = "твоя слабость"
    else:
        verdict = "его слабость"
    ok = row["your_reply_ok_rate"]
    ok_text = "—" if ok is None else f"{ok * 100:.0f}%"
    his_detail = row["his_variant"]
    if row["his_variants"] > 1:
        his_detail += f" (+{row['his_variants'] - 1} вариац.)"
    return (
        f"  {row['opening']} — он {his_detail}\n"
        f"    он {_COLOR_RU[row['his_color']]}: {row['his_games']} парт., "
        f"очки {row['his_score_pct']:.0f}%, ошибок/парт. {row['his_errors_per_game']:.2f}\n"
        f"    твой ответ ({_COLOR_RU[row['your_color']]}): {row['your_reply']} — "
        f"{row['your_reply_games']} парт., прочность {ok_text}, "
        f"твои ошибки/парт. {row['your_errors_per_game']:.2f} ({verdict})"
    )


def format_prep(
    profile: dict[str, Any],
    confrontations: list[dict[str, Any]] | None = None,
    *,
    user: str | None = None,
) -> str:
    """Лист подготовки к сопернику текстом."""
    who = profile["opponent"]
    title = f"Подготовка к сопернику: {who}"
    if user:
        title = f"{title} (ты — {user})"
    out: list[str] = ["=" * 60, title]

    games = profile["games"]
    if not games:
        out.append("")
        out.append("Нет проанализированных партий соперника — нечего готовить.")
        return "\n".join(out)

    out.append(
        f"Партий: {games}, очки {profile['score_pct']:.0f}%, "
        f"рейтинг {profile['rating']['first']} → {profile['rating']['last']} "
        f"({profile['trend']})"
    )
    white, black = profile["colors"]["white"], profile["colors"]["black"]
    favorite = "white" if white > black else "black"
    out.append(
        f"Цвет: белыми {white}, чёрными {black} "
        f"(чаще {_COLOR_RU[favorite]}, {max(white, black) / games * 100.0:.0f}%)"
    )
    if profile["speed"]:
        speeds = ", ".join(f"{name} — {count}" for name, count in profile["speed"].items())
        out.append(f"Контроль времени: {speeds}")
    if profile["small_sample"]:
        out.append(
            f"Внимание: меньше {PREP_MIN_GAMES} партий, выводы о репертуаре шумные."
        )

    for his_color, title_line in (("white", "Он белыми"), ("black", "Он чёрными")):
        rows = profile["openings"][his_color]
        if not rows:
            continue
        out.append("")
        out.append(f"{title_line}:")
        for row in rows:
            variants = ""
            if row["variants"] > 1:
                variants = f" ({row['variants']} вариац., чаще {row['variant']})"
            elif row["variant"] != row["opening"]:
                variants = f" ({row['variant']})"
            out.append(
                f"  {row['opening']}{variants} — {row['games']} парт., "
                f"очки {row['points_pct']:.0f}%, ошибок/парт. "
                f"{row['errors_per_game']:.2f}, ср. потеря win% {row['avg_drop']:.1f}"
            )

    if confrontations is not None:
        out.append("")
        out.append("Точки встречи (его дебют → твой ответ):")
        if not confrontations:
            out.append(
                "  Пересечений с твоим репертуаром не нашлось: готовиться пока не к чему."
            )
        for row in confrontations:
            out.append(_opening_line(row))

    weaknesses = profile["weaknesses"]
    if weaknesses["phases"] or weaknesses["motifs"]:
        out.append("")
        out.append("Где он ошибается больше всего:")
        for item in weaknesses["phases"]:
            detail = (
                f", ср. потеря win% {item['avg_drop']:.1f}" if item["avg_drop"] else ""
            )
            out.append(f"  {item['label']}: {item['bad_moves']} плохих ходов{detail}")
        for item in weaknesses["motifs"]:
            out.append(f"  {item['motif']}: {item['bad_moves']}")

    return "\n".join(out)


#: Что тренируем: свои провалы против него, его ошибки или оба набора.
DRILL_FOCUSES = ("mine", "his", "both")


def games_against(
    pairs: list[tuple[Game, dict]], nickname: str | None
) -> list[tuple[Game, dict]]:
    """Твои партии против конкретного соперника.

    Сравнение регистронезависимое: на Lichess ник нечувствителен к регистру,
    а в кеше остаётся ровно то, что прислал сервер. Пустой ник даёт пустой
    список, а не «все партии».
    """
    nick = (nickname or "").strip().lower()
    if not nick:
        return []
    return [
        (game, analysis)
        for game, analysis in pairs
        if (game.opponent or "").strip().lower() == nick
    ]


def _drills_from(
    pairs: list[tuple[Game, dict]],
    *,
    min_drop: float,
    min_win: float,
    max_per_game: int,
    humanity: list[dict] | None,
    allowed_verdicts: set[str] | None,
) -> list[Drill]:
    """Дрели из набора партий, отсортированные по размеру потери win%."""
    found: list[Drill] = []
    for game, analysis in pairs:
        found.extend(
            collect_drills(
                game,
                analysis,
                min_drop=min_drop,
                min_win=min_win,
                max_per_game=max_per_game,
                humanity=humanity,
                allowed_verdicts=allowed_verdicts,
            )
        )
    found.sort(key=lambda drill: -(drill.drop or 0.0))
    return found


def _families_of(drills: list[Drill], limit: int = 5) -> list[tuple[str, int]]:
    """Дебютные семейства дрелей по убыванию числа задач."""
    counts: dict[str, int] = defaultdict(int)
    for drill in drills:
        counts[opening_family(drill.opening or "")] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _verdicts_for(
    pairs: list[tuple[Game, dict]],
    humanity: list[dict] | None,
    allowed_verdicts: set[str] | None,
    label: str,
) -> tuple[set[str] | None, str]:
    """Вердикты человечности для набора партий и предупреждение, если их нет.

    Фильтр человечности привязан к конкретным партиям, поэтому один и тот же
    файл ``humanity.json`` не покрывает и твои партии, и чужие. Если в нём
    нет ни одной партии из набора, фильтр не применяется — иначе набор молча
    опустел бы, и это выглядело бы как «ошибок нет».
    """
    if not allowed_verdicts:
        return None, ""
    covered = {str(item.get("game_id")) for item in humanity or []}
    if not covered & {game.id for game, _ in pairs}:
        return None, (
            f"вердикты человечности не покрывают {label} — дрели отобраны без них"
        )
    return allowed_verdicts, ""


def build_opponent_drills(
    my_pairs: list[tuple[Game, dict]],
    his_pairs: list[tuple[Game, dict]],
    *,
    opponent: str,
    focus: str = "both",
    min_drop: float = 15.0,
    min_win: float = 50.0,
    max_per_game: int = 8,
    humanity: list[dict] | None = None,
    allowed_verdicts: set[str] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Дрели под матч с соперником: два набора задач.

    * ``mine`` — твои провалы **против него**: те же позиции, где ты
      зевнул в партии с этим человеком. Их мало (в кеше на человека
      приходится 1–2 партии), зато это ровно то, что повторится.
    * ``his`` — **его** ошибки, взятые из его собственных партий: позиции,
      где он стоял и проиграл. Это основной объём материала, потому что его
      партии уже разобраны командой ``prepare``. Смысл такой тренировки —
      заранее научиться находить ход в тех структурах, где он спотыкается.

    ``focus`` выбирает, что попадёт в результат. ``profile`` (его профиль
    из :func:`build_opponent_profile`) не обязателен: он добавляет в лист
    подсказку, какие мотивы в его ошибках самые частые.
    """
    if focus not in DRILL_FOCUSES:
        raise ValueError(f"неизвестный фокус: {focus!r}")

    against = games_against(my_pairs, opponent)
    mine_verdicts, mine_note = _verdicts_for(
        against, humanity, allowed_verdicts, "твои партии с ним"
    )
    his_verdicts, his_note = _verdicts_for(
        his_pairs, humanity, allowed_verdicts, "его партии"
    )

    mine = (
        _drills_from(
            against,
            min_drop=min_drop,
            min_win=min_win,
            max_per_game=max_per_game,
            humanity=humanity,
            allowed_verdicts=mine_verdicts,
        )
        if focus in ("mine", "both")
        else []
    )
    his = (
        _drills_from(
            his_pairs,
            min_drop=min_drop,
            min_win=min_win,
            max_per_game=max_per_game,
            humanity=humanity,
            allowed_verdicts=his_verdicts,
        )
        if focus in ("his", "both")
        else []
    )

    # Мотивы приходят двумя формами: weakness_stats отдаёт словарь
    # «мотив → счётчик», а профиль соперника — уже готовый список словарей.
    raw_motifs = (profile or {}).get("weaknesses", {}).get("motifs") or {}
    motif_items = list(raw_motifs.values()) if isinstance(raw_motifs, dict) else raw_motifs
    motifs = [
        {"motif": item["motif"], "bad_moves": item["bad_moves"]}
        for item in motif_items[:3]
        if item.get("bad_moves")
    ]

    return {
        "opponent": opponent,
        "focus": focus,
        "mine": {
            # партии против него считаем только если их набор вообще в фокусе,
            # иначе в листе появится «2 игры, 0 дрелей» и это будет путать
            "games": len(against) if focus in ("mine", "both") else 0,
            "drills": mine,
            "families": _families_of(mine),
        },
        "his": {
            "games": len(his_pairs) if focus in ("his", "both") else 0,
            "drills": his,
            "families": _families_of(his),
        },
        "motifs": motifs,
        "notes": [note for note in (mine_note, his_note) if note],
        "drills": mine + his,
    }


def _drill_count_line(count: int) -> str:
    """«3 дрели» с правильным окончанием."""
    return f"{count} {_plural_word(count, 'дреля', 'дрели', 'дрелей')}"


def _task_count_line(count: int) -> str:
    """«3 задачи» с правильным окончанием."""
    return f"{count} {_plural_word(count, 'задача', 'задачи', 'задач')}"


def _plural_word(count: int, one: str, few: str, many: str) -> str:
    n = abs(count) % 100
    if 10 < n < 20:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_opponent_drills(plan: dict[str, Any]) -> str:
    """Лист тренировки под соперника: что качаем и в каких дебютах."""
    out = ["=" * 60, f"Дрели против: {plan['opponent']}"]

    mine, his = plan["mine"], plan["his"]
    out.append("")
    out.append(
        f"Твоих провалов против него: {mine['games']} "
        f"{_plural_games(mine['games'])}, {_drill_count_line(len(mine['drills']))}"
    )
    out.append(
        f"Его ошибок — твои наказания: {his['games']} "
        f"{_plural_games(his['games'])}, {_drill_count_line(len(his['drills']))}"
    )

    if not plan["drills"]:
        out.append("")
        out.append("Дрелей не нашлось: снизь --min-drop/--min-win или разбери больше партий.")
        focus = plan["focus"]
        if focus in ("his", "both") and not his["games"]:
            out.append(
                f"Его партий в кеше нет — сначала: python -m trainer prepare "
                f"--user ТЫ --opponent {plan['opponent']}"
            )
        elif focus in ("mine", "both") and not mine["games"]:
            out.append(
                f"Партий с ним у тебя в кеше нет — разбери свои: "
                f"python -m trainer coach --user ТЫ"
            )
        return "\n".join(out)

    if his["families"]:
        out.append("")
        out.append("Дебюты, где он ошибается (сначала эти):")
        for family, count in his["families"]:
            out.append(f"  {family} — {_task_count_line(count)}")
    if mine["families"]:
        out.append("")
        out.append("Твои провалы в его дебютах:")
        for family, count in mine["families"]:
            out.append(f"  {family} — {_task_count_line(count)}")
    if plan["motifs"]:
        out.append("")
        out.append("Что он забывает чаще всего:")
        for item in plan["motifs"]:
            out.append(f"  {item['motif']}: {item['bad_moves']}")
    for note in plan["notes"]:
        out.append("")
        out.append(f"Замечание: {note}.")

    return "\n".join(out)
