"""
Загрузчик каталогов мемов (CAT.001) и практик (CAT.003) из JSON.

JSON-файлы в data/catalogs/ — выгрузка из Pack (PD.CAT.001, PD.CAT.003).
При обновлении каталогов в Pack — пересоздать JSON.
"""

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CATALOGS_DIR = os.path.join(
    os.path.dirname(__file__),
    '..', '..', 'data', 'catalogs'
)

# Кэши
_memes_cache: Optional[List[dict]] = None
_practices_cache: Optional[List[dict]] = None

# Маппинг «блокирует переход» → минимальная ступень, на которой мем актуален
_TRANSITION_TO_STAGE = {
    '1→2': 1,
    '2→3': 2,
    '3→4': 3,
    '4→5': 4,
}


def _load_memes() -> List[dict]:
    """Загрузить все мемы из JSON."""
    global _memes_cache
    if _memes_cache is not None:
        return _memes_cache

    filepath = os.path.join(_CATALOGS_DIR, 'memes.json')
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _memes_cache = data.get('memes', [])
        logger.info(f"[Catalogs] Loaded {len(_memes_cache)} memes")
    except FileNotFoundError:
        logger.warning(f"[Catalogs] memes.json not found at {filepath}")
        _memes_cache = []
    except Exception as e:
        logger.warning(f"[Catalogs] Error loading memes: {e}")
        _memes_cache = []

    return _memes_cache


def _load_practices() -> List[dict]:
    """Загрузить все практики из JSON."""
    global _practices_cache
    if _practices_cache is not None:
        return _practices_cache

    filepath = os.path.join(_CATALOGS_DIR, 'practices.json')
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _practices_cache = data.get('practices', [])
        logger.info(f"[Catalogs] Loaded {len(_practices_cache)} practices")
    except FileNotFoundError:
        logger.warning(f"[Catalogs] practices.json not found at {filepath}")
        _practices_cache = []
    except Exception as e:
        logger.warning(f"[Catalogs] Error loading practices: {e}")
        _practices_cache = []

    return _practices_cache


def _parse_transition(blocks_transition: str) -> int:
    """Извлечь минимальную ступень из строки вида '1→2' или '2→3'."""
    return _TRANSITION_TO_STAGE.get(blocks_transition, 0)


def get_memes_for_area(area: int, student_stage: int) -> List[dict]:
    """Получить мемы для области, релевантные ступени ученика.

    Мемы фильтруются по области и блокируемому переходу:
    - Мем с blocks_transition='1→2' актуален для ступеней 1-2
    - Мем с blocks_transition='2→3' актуален для ступеней 1-2
    - Мем с blocks_transition='3→4' актуален для ступеней 2-3

    Сортировка: сначала мемы, блокирующие ближайший переход.
    """
    all_memes = _load_memes()

    # Определить текущий и следующий переход
    current_transition = f"{student_stage}→{student_stage + 1}"

    candidates = []
    for m in all_memes:
        if m.get('area') != area:
            continue
        bt = m.get('blocks_transition', '')
        min_stage = _parse_transition(bt)
        # Мем актуален если ступень ученика >= минимальной
        # и ученик ещё не прошёл этот переход
        if min_stage <= student_stage and bt >= current_transition:
            # Нет смысла давать мем 3→4, если ученик на ступени 0
            # Но мем 1→2 актуален и для ступени 0 (подготовка)
            candidates.append(m)

    # Если строгий фильтр ничего не дал — расширяем
    if not candidates:
        candidates = [
            m for m in all_memes if m.get('area') == area
        ]

    # Сортировка: ближайший переход первым
    candidates.sort(
        key=lambda m: (
            0 if m.get('blocks_transition', '') == current_transition else 1,
            m.get('id', '')
        )
    )
    return candidates


def get_practices_for_area(
    area: int,
    student_stage: int,
    target_degree: Optional[int] = None,
) -> List[dict]:
    """Получить практики для области.

    Engine вызывает с target_degree = completed_depth + 1.
    Если target_degree не указан — ceiling по student_stage.
    """
    all_practices = _load_practices()

    # Ceiling: student_stage ограничивает max доступную степень
    stage_max = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4}
    max_degree = stage_max.get(student_stage, 1)

    candidates = []
    for p in all_practices:
        if p.get('area') != area:
            continue

        # Выбрать карточку нужной степени
        degree = min(target_degree or 1, max_degree)
        degrees = p.get('degrees', [])
        degree_card = None
        for d in degrees:
            if d.get('degree') == degree:
                degree_card = d
                break

        if degree_card is None and degrees:
            degree_card = degrees[0]
            degree = degree_card.get('degree', 1) if degree_card else 1

        candidates.append({
            'id': p['id'],
            'name': p.get('name', ''),
            'context': p.get('context', ''),
            'area': area,
            'current_degree': degree,
            'can_do': degree_card.get('can_do', '') if degree_card else '',
            'assignment': degree_card.get('assignment', '') if degree_card else '',
            'assessment': degree_card.get('assessment', '') if degree_card else '',
        })

    return candidates
