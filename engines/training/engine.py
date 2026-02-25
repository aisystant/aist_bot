"""
TrainingEngine — бизнес-логика режима Тренировка.

Жизненный цикл:
1. setup() — первоначальная настройка (когнитивный уровень + принципы)
2. get_dashboard_data() — дашборд с прогрессом
3. generate_assignment(principle_id) — генерация задания
4. evaluate_answer(...) — AI-оценка ответа → advance depth
"""

import random
from typing import Optional

from config import (
    get_logger,
    ZP_PRINCIPLES,
    TRAINING_MAX_DEPTH,
    TRAINING_COGNITIVE_LEVELS,
)
from db.queries.users import get_intern
from db.queries.training import (
    get_training_settings,
    save_training_settings,
    update_training_settings,
    get_training_progress,
    get_principle_depth,
    advance_principle_depth,
    increment_attempts,
    save_training_attempt,
    get_training_stats,
    reset_training_progress,
)

logger = get_logger(__name__)


class TrainingEngine:
    """Движок режима Тренировка."""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self._intern = None
        self._settings = None

    # === User context ===

    async def get_intern(self) -> Optional[dict]:
        if not self._intern:
            self._intern = await get_intern(self.chat_id)
        return self._intern

    async def get_lang(self) -> str:
        intern = await self.get_intern()
        return intern.get('language', 'ru') if intern else 'ru'

    async def get_settings(self) -> Optional[dict]:
        if not self._settings:
            self._settings = await get_training_settings(self.chat_id)
        return self._settings

    async def is_setup_complete(self) -> bool:
        settings = await self.get_settings()
        return settings is not None

    # === First-time setup ===

    async def setup(
        self,
        cognitive_level: str,
        enabled_principles: list,
        training_mode: str = 'shuffle',
        single_principle: str = None,
    ) -> bool:
        """Сохранить первоначальные настройки."""
        if cognitive_level not in TRAINING_COGNITIVE_LEVELS:
            return False
        valid = [p for p in enabled_principles if p in ZP_PRINCIPLES]
        if not valid:
            return False
        if training_mode not in ('shuffle', 'sequential', 'single'):
            training_mode = 'shuffle'
        self._settings = await save_training_settings(
            self.chat_id, cognitive_level, valid,
            training_mode=training_mode,
            single_principle=single_principle,
        )
        return True

    async def get_next_principle(self) -> Optional[str]:
        """Определить следующий принцип для тренировки на основе режима."""
        settings = await self.get_settings()
        if not settings:
            return None

        mode = settings.get('training_mode', 'shuffle')
        enabled = settings.get('enabled_principles', list(ZP_PRINCIPLES))
        progress_rows = await get_training_progress(self.chat_id)
        progress_map = {r['principle_id']: r['current_depth'] for r in progress_rows}

        if mode == 'single':
            pid = settings.get('single_principle')
            if pid and progress_map.get(pid, 0) < TRAINING_MAX_DEPTH:
                return pid
            return None

        # Собрать непройденные
        incomplete = [
            pid for pid in ZP_PRINCIPLES
            if pid in enabled and progress_map.get(pid, 0) < TRAINING_MAX_DEPTH
        ]
        if not incomplete:
            return None

        if mode == 'sequential':
            return incomplete[0]

        # shuffle (default)
        return random.choice(incomplete)

    async def update_training_mode(self, training_mode: str, single_principle: str = None) -> bool:
        """Обновить режим тренировки."""
        if training_mode not in ('shuffle', 'sequential', 'single'):
            return False
        if training_mode == 'single':
            # Для single — обновить enabled_principles тоже
            if single_principle and single_principle in ZP_PRINCIPLES:
                await update_training_settings(
                    self.chat_id,
                    training_mode=training_mode,
                    single_principle=single_principle,
                    enabled_principles=[single_principle],
                )
            else:
                return False
        elif training_mode in ('shuffle', 'sequential'):
            # Для shuffle/sequential — включить все
            await update_training_settings(
                self.chat_id,
                training_mode=training_mode,
                single_principle=None,
                enabled_principles=list(ZP_PRINCIPLES),
            )
        self._settings = None
        return True

    # === Dashboard ===

    async def get_dashboard_data(self) -> dict:
        """Данные дашборда: принципы, глубины, статистика."""
        settings = await self.get_settings()
        if not settings:
            return {}

        enabled = settings.get('enabled_principles', [])
        progress_rows = await get_training_progress(self.chat_id)
        progress_map = {r['principle_id']: r for r in progress_rows}
        stats = await get_training_stats(self.chat_id)

        from .planner import get_principle_name

        principles = []
        for pid in ZP_PRINCIPLES:
            prog = progress_map.get(pid)
            current_depth = prog['current_depth'] if prog else 0
            principles.append({
                'id': pid,
                'name': get_principle_name(pid),
                'current_depth': current_depth,
                'max_depth': TRAINING_MAX_DEPTH,
                'enabled': pid in enabled,
                'completed': current_depth >= TRAINING_MAX_DEPTH,
            })

        from .planner import get_principle_name as _gpn
        mode = settings.get('training_mode', 'shuffle')
        single_pid = settings.get('single_principle')

        # Метка текущего режима
        MODE_LABELS = {
            'shuffle': '🔀 Все вперемешку',
            'sequential': '📶 По порядку',
        }
        if mode == 'single' and single_pid:
            mode_label = f"🔹 {single_pid} {_gpn(single_pid)}"
        else:
            mode_label = MODE_LABELS.get(mode, '🔀 Все вперемешку')

        return {
            'principles': principles,
            'cognitive_level': settings.get('cognitive_level', 'postformal'),
            'cognitive_label': TRAINING_COGNITIVE_LEVELS.get(
                settings.get('cognitive_level', 'postformal'), 'Взрослый'
            ),
            'training_mode': mode,
            'mode_label': mode_label,
            'stats': stats,
        }

    # === Assignment ===

    async def generate_assignment(self, principle_id: str) -> Optional[dict]:
        """Сгенерировать задание для принципа на текущей глубине."""
        settings = await self.get_settings()
        if not settings:
            logger.warning(f"[Training] No settings for chat_id={self.chat_id}")
            return None

        enabled = settings.get('enabled_principles', [])
        if principle_id not in enabled:
            logger.warning(f"[Training] {principle_id} not in enabled={enabled}")
            return None

        current_depth = await get_principle_depth(self.chat_id, principle_id)
        target_depth = current_depth + 1
        if target_depth > TRAINING_MAX_DEPTH:
            logger.info(f"[Training] {principle_id} fully completed (depth={current_depth})")
            return None  # Принцип полностью пройден

        from .planner import load_zp_cells, generate_assignment_text, get_principle_name
        cells = load_zp_cells()
        principle_data = cells.get(principle_id)
        if not principle_data:
            logger.error(f"[Training] No cell data for {principle_id}, cells keys={list(cells.keys())[:3]}...")
            return None

        depth_data = principle_data.get('depths', {}).get(str(target_depth))
        if not depth_data:
            logger.error(f"[Training] No depth_data for {principle_id} depth={target_depth}")
            return None

        cognitive_level = settings.get('cognitive_level', 'postformal')
        intern = await self.get_intern()
        p_name = get_principle_name(principle_id)

        assignment_text = await generate_assignment_text(
            depth_data, cognitive_level, intern,
            p_name, target_depth
        )

        return {
            'principle_id': principle_id,
            'principle_name': p_name,
            'depth': target_depth,
            'bloom_level': depth_data.get('bloom_level', ''),
            'bridge_text': depth_data.get('bridge_template') or '',
            'assignment_text': assignment_text,
            'criteria': depth_data.get('criteria', ''),
            'common_errors': depth_data.get('common_errors', []),
            'can_do': depth_data.get('can_do', []),
        }

    # === Evaluation ===

    async def evaluate_answer(
        self,
        principle_id: str,
        depth: int,
        assignment_text: str,
        answer_text: str,
    ) -> dict:
        """Оценить ответ через AI. Возвращает {passed, partial, feedback, new_depth}."""
        from .planner import load_zp_cells, evaluate_training_answer

        cells = load_zp_cells()
        principle_data = cells.get(principle_id, {})
        depth_data = principle_data.get('depths', {}).get(str(depth), {})
        intern = await self.get_intern()

        result = await evaluate_training_answer(
            answer_text=answer_text,
            cell_data=depth_data,
            assignment_text=assignment_text,
            intern=intern,
        )

        passed = result.get('passed', False)
        feedback = result.get('feedback', '')

        # Сохранить попытку
        await save_training_attempt(
            self.chat_id, principle_id, depth,
            assignment_text, answer_text, passed, feedback
        )

        # Обновить прогресс
        await increment_attempts(self.chat_id, principle_id)

        new_depth = None
        if passed:
            new_depth = depth
            await advance_principle_depth(self.chat_id, principle_id, depth)
            # Сохранить в answers для кросс-режимной статистики
            await self._save_to_answers(principle_id, depth, answer_text)

        return {
            'passed': passed,
            'partial': result.get('partial', False),
            'feedback': feedback,
            'new_depth': new_depth,
        }

    async def _save_to_answers(self, principle_id: str, depth: int, answer_text: str):
        """Сохранить в общую таблицу answers для кросс-режимной статистики."""
        try:
            from db.queries.answers import save_answer
            await save_answer(
                chat_id=self.chat_id,
                mode='training',
                answer_type='training_answer',
                answer=answer_text,
                topic_id=f"{principle_id}@{depth}",
            )
        except Exception as e:
            logger.warning(f"Failed to save training answer to answers table: {e}")

    # === Settings ===

    async def update_cognitive_level(self, level: str) -> bool:
        if level not in TRAINING_COGNITIVE_LEVELS:
            return False
        await update_training_settings(self.chat_id, cognitive_level=level)
        self._settings = None
        return True

    async def toggle_principle(self, principle_id: str) -> bool:
        """Переключить принцип вкл/выкл."""
        if principle_id not in ZP_PRINCIPLES:
            return False
        settings = await self.get_settings()
        if not settings:
            return False
        enabled = settings.get('enabled_principles', [])
        if principle_id in enabled:
            enabled = [p for p in enabled if p != principle_id]
        else:
            enabled.append(principle_id)
        if not enabled:
            return False  # Нельзя отключить все
        await update_training_settings(self.chat_id, enabled_principles=enabled)
        self._settings = None
        return True

    async def enable_all_principles(self) -> bool:
        """Включить все принципы."""
        await update_training_settings(
            self.chat_id, enabled_principles=list(ZP_PRINCIPLES)
        )
        self._settings = None
        return True

    async def reset_progress(self) -> None:
        """Сбросить весь прогресс тренировки (начать заново)."""
        await reset_training_progress(self.chat_id)
