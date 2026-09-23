"""LLM-тренер: персональные рекомендации по стилю игры и плану тренировок.

Превращает структурированный план ``build_plan`` (репертуар, слабые дебюты,
узоры ошибок, фазы, человечность, дрели) в природный тренерский рассказ.
Модель, ключ и точка OpenAI-совместимого API берутся из настроек (env):
``LLM_MODEL``, ``LLM_API_KEY``, ``LLM_BASE_URL`` — и могут быть переопределены
через CLI. Дополнительные заметки игрока (цель, время, самооценка) передаются
пользователем как ``extra`` и вшиваются в запрос, чтобы тренер учитывал их.
"""

from __future__ import annotations

import textwrap
from typing import Any

from .llm import ChatMessage, LLMClient

__all__ = [
    "build_mentor_request",
    "format_mentor_reply",
    "run_mentor",
]

_DASH = "—"

_SYSTEM_PROMPT = """Ты — персональный шахматный тренер взрослого игрока с рейтингом ~2100 (КМС).
Пиши по-русски, конкретно и по делу: без общих слов вроде «больше решай тактику».
Опирайся только на факты из переданной «сводки тренера»; не сочиняй партий,
статистики и оценок, которых там нет. Твоя цель — объяснить игроку, КАК он играет,
и дать план из шагов: что закреплять в дебюте, какие узоры ошибок тренировать,
какие дрели решать и как следить за прогрессом. Уважай финальные 2-3 строки,
где перечислены «особые пожелания игрока», и отметь их в плане."""


def _opening_lines(opening_rows: list[dict[str, Any]]) -> str:
    """Компактное описание слабых дебютов одной строкой."""
    if not opening_rows:
        return "  (нет данных)"
    lines = []
    for row in opening_rows[:6]:
        lines.append(
            f"  • {row['opening']}: {row['games']} игр, {row['points_pct']:.0f}% очков, "
            f"{row['errors_per_game']} зевк.-ошибок/игру"
        )
    return "\n".join(lines)


def _motifs_lines(motifs: list[tuple[str, dict]]) -> str:
    """Компактное перечисление узоров ошибок."""
    if not motifs:
        return "  (нет данных)"
    lines = []
    for motif, stats in motifs[:6]:
        lines.append(
            f"  • {motif}: {stats['count']} раз, ср. потеря {stats['avg_drop']}%"
        )
    return "\n".join(lines)


def _phase_lines(phases: list[tuple[str, dict]]) -> str:
    """Компактное перечисление фаз, где теряется win%."""
    if not phases:
        return "  (нет данных)"
    return "\n".join(
        f"  • {ph}: {stats['count']} плохих ходов, ср. потеря {stats['avg_drop']}%"
        for ph, stats in phases[:6]
    )


def _branch_lines(branches: list[dict[str, Any]]) -> str:
    """Компактное описание дебютных ветвей (какая прочность)."""
    if not branches:
        return "  (нет данных)"
    return "\n".join(
        f"  • {' '.join(item['moves'])} ({item['count']} {_plural_games(item['count'])}, "
        f"прочность {item['ok_rate']:.0%})"
        for item in branches[:8]
    )


def _plural_games(num: int) -> str:
    n = abs(num) % 100
    digit = n % 10
    if 10 < n < 20:
        return "игр"
    if digit == 1:
        return "игра"
    if 2 <= digit <= 4:
        return "игры"
    return "игр"


def build_mentor_request(
    plan: dict[str, Any],
    *,
    extra: str = "",
) -> list[ChatMessage]:
    """Собирает (системный, пользовательский) запрос для тренера из плана."""
    rep = plan.get("repertoire", {})
    repertoire_txt = "\n\n".join(
        f"{color_ru}: \n{_branch_lines(rep.get(color, []))}"
        for color_ru, color in (("Белые", "white"), ("Чёрные", "black"))
    )

    users_ru = plan.get("user") or "игрок"
    score = plan.get("score_pct")
    acpl = plan.get("acpl")
    summary_anchor = f"""Стиль партий: {score}% очков, ACPL {acpl}."""

    sections = [
        f"Игрок: {users_ru}. Партий в кеше: {plan.get('games', 0)}.",
        summary_anchor,
        "",
        "ДЕБЮТНЫЙ РЕПЕРТУАР (что встречается чаще всего):",
        repertoire_txt,
        "",
        "СЛАБЫЕ ДЕБЮТЫ (где сыплешься — разбери варианты):",
        _opening_lines(rep.get("weak_openings", [])),
        "",
        "УЗОРЫ ОШИБОК (тренируй в тренажёре):",
        _motifs_lines(plan.get("patterns", {}).get("motifs", [])),
        "",
        "ФАЗЫ (где сливаешь win%):",
        _phase_lines(plan.get("patterns", {}).get("phases", [])),
        "",
        "ЧЕЛОВЕЧНОСТЬ (неестественные промахи → дрели):",
        _humanity_line(plan.get("humanity")),
        "",
        "ДРЕЛИ (внешние файлы с позициями):",
        f"  • unnatural: {plan.get('drills', {}).get('unnatural', 0)} позиций — решать ежедневно",
        f"  • всего дрелей: {plan.get('drills', {}).get('total', 0)}",
    ]

    if extra.strip():
        sections.append("")
        sections.append("ОСОБЫЕ ПОЖЕЛАНИЯ ИГРОКА (учти их в плане):")
        sections.append(textwrap.indent(extra.strip(), "  "))

    user_text = "\n".join(sections)

    request_text = (
        "Расскажи, как я играю (стиль: агрессия/позиционная игра, сильные и слабые стороны "
        "по данным выше), затем дай персональный план тренировки: 1) дебют — что закреплять "
        "и какие слабые дебюты разобрать, 2) узоры ошибок — какие дрели решать в первую "
        "очередь и почему, 3) фазы — где теряешь win% и что этим закрепить, 4) человечность "
        "— как снизить неестественные промахи. Итог — 2-3 предложения о том, как я должен "
        "выглядеть через месяц тренировок."
    )

    return [
        ChatMessage("system", _SYSTEM_PROMPT),
        ChatMessage(
            "user",
            f"СВОДКА ТРЕНЕРА:\n{user_text}\n\nЗАДАЧА:\n{request_text}",
        ),
    ]


def _humanity_line(humanity: dict[str, Any] | None) -> str:
    """Одна строка про человечность, если данные есть."""
    if not humanity:
        return "  (нет данных — запусти: python -m trainer humanize --user NICK)"
    return (
        f"  неестественных промахов: {humanity.get('unnatural')} из "
        f"{humanity.get('total', 0)} ({humanity.get('unnatural_share_pct')}%)"
    )


def format_mentor_reply(text: str) -> str:
    """Аккуратно печатает ответ тренера (обычный текст, без JSON)."""
    return textwrap.dedent(text).strip()


def run_mentor(llm: LLMClient, messages: list[ChatMessage]) -> str:
    """Зовёт LLM и возвращает текст тренерской рекомендации (без коуча-фаз)."""
    return llm.chat(messages, temperature=0.5, max_tokens=1200, json_mode=False)
