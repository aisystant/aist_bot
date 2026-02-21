"""
Конфигурация конверсионной воронки (DP.ARCH.002 § 12).

C5: Topic → Program mapping
C6: Goal → Program matching (keyword-based)
C7: Events calendar
"""

from datetime import date

# ═══════════════════════════════════════════════════════════
# C5: Topic → Program (§ 12.7)
# ═══════════════════════════════════════════════════════════

# Guide slug → Program key (maps to PLATFORM_URLS)
GUIDE_TO_PROGRAM = {
    "1-1-self-development": "lr",
    "1-2-self-development-methods": "lr",
    "1-3-systems-thinking-introduction": "lr",
}

# Marathon main_concept → Program key
CONCEPT_TO_PROGRAM = {
    "диагностика состояния": "lr",
    "самодиагностика": "lr",
    "ложное облегчение": "lr",
    "слот саморазвития": "lr",
    "рассеянное внимание": "lr",
    "выгорание": "lr",
    "систематичность vs героизм": "lr",
    "минимальное время": "lr",
    "эксперимент": "lr",
    "личный прогресс": "lr",
    "ИИ как усилитель": "lr",
    "набор практик vs мотивация": "lr",
    "роли и ролевое мастерство": "rr",
    "управление": "rr",
    "команды": "rr",
}

# Program key → display names
PROGRAM_NAMES = {
    "lr": {"ru": "Личное развитие", "en": "Personal Development"},
    "rr": {"ru": "Рабочее развитие", "en": "Professional Development"},
    "ir": {"ru": "Исследовательское развитие", "en": "Research Development"},
}


def get_program_for_guide(guide_slug: str) -> str | None:
    """Получить ключ программы по guide slug."""
    return GUIDE_TO_PROGRAM.get(guide_slug)


def get_program_for_concept(main_concept: str) -> str | None:
    """Получить ключ программы по main_concept темы."""
    if not main_concept:
        return None
    mc = main_concept.lower().strip()
    return CONCEPT_TO_PROGRAM.get(mc)


# ═══════════════════════════════════════════════════════════
# C6: Goal → Program (§ 12.8)
# ═══════════════════════════════════════════════════════════

# Keywords in user goals/interests → program
GOAL_KEYWORDS = {
    "lr": [
        "ритм", "привычк", "время", "стресс", "внимани", "фокус",
        "собранность", "мотивац", "саморазвит", "обучени", "практик",
        "мышлени", "интеллект", "развити", "дисциплин", "здоровь",
        "выгоран", "отдых", "баланс", "ученик", "системн",
    ],
    "rr": [
        "команд", "управлен", "менеджмент", "результат", "бизнес",
        "проект", "организац", "лидерств", "руководств", "процесс",
        "стратег", "KPI", "OKR", "agile", "операцион",
    ],
    "ir": [
        "исследован", "наук", "методолог", "диссертац", "публикац",
        "анализ", "данн", "модел", "теори", "гипотез",
    ],
}


def match_goals_to_program(goals: str, interests: str = "") -> str | None:
    """Матчинг целей/интересов пользователя к программе.

    Returns:
        Program key ('lr', 'rr', 'ir') or None.
    """
    text = f"{goals} {interests}".lower()
    if not text.strip():
        return None

    scores = {"lr": 0, "rr": 0, "ir": 0}
    for program, keywords in GOAL_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[program] += 1

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None
    return best


# ═══════════════════════════════════════════════════════════
# C7: Events Calendar (§ 12.7)
# ═══════════════════════════════════════════════════════════

UPCOMING_EVENTS = [
    {
        "name_ru": "ИРС для управляемого развития: как удерживать контур без героизма",
        "name_en": "IRS for Managed Development: maintaining control without heroism",
        "date": date(2026, 2, 28),
        "url": "https://system-school.ru/list",
        "advance_days": 5,  # За сколько дней уведомлять
    },
]


def get_upcoming_events(today: date, advance_days_override: int = None) -> list[dict]:
    """Получить события, о которых нужно уведомить сегодня."""
    result = []
    for event in UPCOMING_EVENTS:
        advance = advance_days_override or event.get("advance_days", 5)
        days_until = (event["date"] - today).days
        if 0 < days_until <= advance:
            result.append({**event, "days_until": days_until})
    return result
