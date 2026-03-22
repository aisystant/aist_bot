"""
Загрузчик ячеек контента из DS-principles-curriculum/cells/ (WP-149).

Ячейки = файлы SS.F1.{NN}-{slug}.md с YAML-блоками внутри.
Каждый файл содержит N глубин (@1, @2, ...).
"""

import logging
import os
import re
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Путь к ячейкам (относительно корня IWE)
_CELLS_DIR = os.path.join(
    os.path.dirname(__file__),
    '..', '..', '..', '..', 'DS-principles-curriculum', 'cells'
)

# Кэш загруженных ячеек: {cell_id: cell_data}
_cell_cache: Dict[str, dict] = {}

# Маппинг topic_id → файл
_TOPIC_FILES = {
    'SS.F1.01': 'SS.F1.01-learner-role.md',
    'SS.F1.02': 'SS.F1.02-worldview.md',
    'SS.F1.03': 'SS.F1.03-uncertainty.md',
    'SS.F1.04': 'SS.F1.04-composure.md',
    'SS.F1.05': 'SS.F1.05-emotions.md',
    'SS.F1.06': 'SS.F1.06-mastery.md',
    'SS.F1.07': 'SS.F1.07-agency.md',
    'SS.F1.08': 'SS.F1.08-time.md',
    'SS.F1.09': 'SS.F1.09-path-map.md',
}

# Маппинг topic_id → направление (1-6)
TOPIC_DIRECTIONS = {
    'SS.F1.01': 2,  # Навыки
    'SS.F1.02': 1,  # Картины мира
    'SS.F1.03': 1,  # Картины мира
    'SS.F1.04': 6,  # Организм
    'SS.F1.05': 3,  # Ограничения
    'SS.F1.06': 2,  # Навыки
    'SS.F1.07': 1,  # Картины мира
    'SS.F1.08': 4,  # Экзокортекс
    'SS.F1.09': 2,  # Навыки
}

# Обратный маппинг: направление → список topic_ids
DIRECTION_TOPICS: Dict[int, List[str]] = {}
for _tid, _dir in TOPIC_DIRECTIONS.items():
    DIRECTION_TOPICS.setdefault(_dir, []).append(_tid)

# Все topic_ids фазы 1
F1_TOPICS = list(_TOPIC_FILES.keys())


def _parse_cells_from_file(filepath: str) -> Dict[int, dict]:
    """Извлечь YAML-ячейки из markdown-файла.

    Формат файла: markdown с ```yaml ... ``` блоками,
    каждый блок содержит cell: {...} с depth: N.

    Returns:
        {depth: cell_data}
    """
    cells = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Извлекаем все yaml-блоки
        yaml_blocks = re.findall(r'```yaml\n(.*?)```', content, re.DOTALL)

        for block in yaml_blocks:
            try:
                data = yaml.safe_load(block)
                if data and isinstance(data, dict) and 'cell' in data:
                    cell = data['cell']
                    depth = cell.get('depth')
                    if depth:
                        cells[int(depth)] = cell
            except yaml.YAMLError as e:
                logger.warning(f"[Tailor/Cells] YAML parse error in {filepath}: {e}")

    except FileNotFoundError:
        logger.warning(f"[Tailor/Cells] File not found: {filepath}")
    except Exception as e:
        logger.warning(f"[Tailor/Cells] Error reading {filepath}: {e}")

    return cells


def load_cell(topic_id: str, depth: int) -> Optional[dict]:
    """Загрузить ячейку по topic_id и глубине.

    Args:
        topic_id: 'SS.F1.01', 'SS.F1.02', ...
        depth: 1-5

    Returns:
        Словарь ячейки или None если не найдена.
    """
    cache_key = f"{topic_id}@{depth}"
    if cache_key in _cell_cache:
        return _cell_cache[cache_key]

    filename = _TOPIC_FILES.get(topic_id)
    if not filename:
        return None

    filepath = os.path.join(_CELLS_DIR, filename)
    cells = _parse_cells_from_file(filepath)

    # Кэшируем все глубины из файла
    for d, cell in cells.items():
        _cell_cache[f"{topic_id}@{d}"] = cell

    return _cell_cache.get(cache_key)


def get_topic_name(topic_id: str) -> str:
    """Человекочитаемое название темы."""
    cell = load_cell(topic_id, 1)
    if cell:
        return cell.get('principle_name', topic_id)
    return topic_id


def get_available_depths(topic_id: str) -> List[int]:
    """Какие глубины доступны для темы."""
    filename = _TOPIC_FILES.get(topic_id)
    if not filename:
        return []

    filepath = os.path.join(_CELLS_DIR, filename)
    cells = _parse_cells_from_file(filepath)
    return sorted(cells.keys())


def get_direction_for_topic(topic_id: str) -> Optional[int]:
    """Направление (1-6) для темы."""
    return TOPIC_DIRECTIONS.get(topic_id)
