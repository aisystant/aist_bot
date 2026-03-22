"""
TailorEngine — сборка персонального занятия (WP-149, MIM.SOP.001).

7 шагов SOP.001:
  1. Прочитать профиль и состояние → context
  2. Определить фазу программы
  3. Выбрать направление (weighted random + коррекция)
  4. Выбрать тему и глубину (bottleneck-first)
  5. Собрать контент (ячейка или fallback)
  6. Адаптировать (домен, стиль, ошибки, ИТ-уровень)
  7. Упаковать (structured JSON)

MVP (Ф1): упрощённый — без полного ЦД, без Оркестратора.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .cells import (
    DIRECTION_TOPICS,
    F1_TOPICS,
    TOPIC_DIRECTIONS,
    get_available_depths,
    get_direction_for_topic,
    get_topic_name,
    load_cell,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# Матрица весов: фаза × направление (из MIM.SOP.001)
# ═══════════════════════════════════════════════════════════
PHASE_WEIGHTS = {
    1: {1: 1.5, 2: 1.0, 3: 0.8, 4: 0.5, 5: 0.7, 6: 1.0},
    2: {1: 1.0, 2: 1.0, 3: 0.8, 4: 1.0, 5: 0.8, 6: 1.2},
    3: {1: 0.5, 2: 1.0, 3: 0.8, 4: 1.5, 5: 0.5, 6: 1.6},
    4: {1: 0.5, 2: 1.0, 3: 0.5, 4: 2.0, 5: 0.5, 6: 1.8},
}

# Корректировка по состоянию (SOP.001 Шаг 3)
STATE_ADJUSTMENTS = {
    'chaos':    {6: 2.0, 2: 0.5},
    'stuck':    {3: 1.5, 1: 1.5},
    'turn':     {},
    'development': {4: 1.5},
}

# Ступень → Фаза (SOP.001 Шаг 2)
STAGE_TO_PHASE = {0: 1, 1: 1, 2: 2, 3: 3, 4: 4}


class TailorEngine:
    """Собирает персональное занятие из ячеек контента."""

    def assemble(
        self,
        user_profile: dict,
        learning_history: Optional[List[dict]] = None,
        last_direction: Optional[int] = None,
    ) -> Optional[dict]:
        """Собрать одно персональное занятие (7 шагов SOP.001).

        Args:
            user_profile: профиль из public.users или ЦД
                {name, occupation, goals, interests, assessment_state,
                 student_stage, it_level, energy}
            learning_history: история прохождения
                [{topic_id, bloom_level, passed, direction, errors, ...}]
            last_direction: направление вчерашнего занятия (для ротации)

        Returns:
            Structured lesson dict или None при ошибке.
        """
        history = learning_history or []

        # ─── Шаг 1: Контекст ───
        ctx = self._build_context(user_profile, history)
        logger.info(
            f"[Tailor] Step 1: stage={ctx['student_stage']}, "
            f"state={ctx['state']}, it={ctx['it_level']}"
        )

        # ─── Шаг 2: Фаза ───
        phase = self._determine_phase(ctx['student_stage'])

        # ─── Шаг 3: Направление ───
        direction = self._choose_direction(
            phase, ctx['state'], ctx['energy'],
            last_direction, history,
        )
        if direction is None:
            logger.warning("[Tailor] No direction available")
            return None
        logger.info(f"[Tailor] Step 3: direction={direction}")

        # ─── Шаг 4: Тема и глубина ───
        topic_id, bloom_depth = self._choose_topic(
            direction, history
        )
        if topic_id is None:
            logger.warning(
                f"[Tailor] No topic for direction={direction}"
            )
            return None
        logger.info(
            f"[Tailor] Step 4: topic={topic_id}, bloom={bloom_depth}"
        )

        # ─── Шаг 5: Контент ───
        cell = load_cell(topic_id, bloom_depth)
        if cell is None:
            # Fallback: попробовать @1
            cell = load_cell(topic_id, 1)
            bloom_depth = 1
        if cell is None:
            logger.warning(
                f"[Tailor] No cell for {topic_id}@{bloom_depth}"
            )
            return None

        # ─── Шаг 6: Адаптация (в MVP — минимальная) ───
        # Полная адаптация через Claude — в Ф2.
        # Сейчас: подставляем домен в forms.

        # ─── Шаг 7: Упаковка ───
        lesson = self._pack_lesson(
            cell=cell,
            topic_id=topic_id,
            direction=direction,
            bloom_depth=bloom_depth,
            phase=phase,
            context=ctx,
            history=history,
        )
        return lesson

    # ═══════════════════════════════════════════════════════════
    # Шаг 1: Контекст
    # ═══════════════════════════════════════════════════════════

    def _build_context(
        self, profile: dict, history: List[dict]
    ) -> dict:
        """Построить контекст из профиля и истории."""
        return {
            'name': profile.get('name', ''),
            'occupation': profile.get('occupation', ''),
            'goals': profile.get('goals', ''),
            'interests': profile.get('interests', ''),
            'student_stage': int(profile.get('student_stage', 0)),
            'it_level': int(profile.get('it_level', 0)),
            'state': profile.get('assessment_state', 'turn'),
            'energy': int(profile.get('energy', 3)),
            'style': profile.get('learning_style', ''),
            'depths': self._compute_depths(history),
        }

    def _compute_depths(
        self, history: List[dict]
    ) -> Dict[str, int]:
        """Текущая max глубина по теме (только passed=true)."""
        depths = {}
        for entry in history:
            if entry.get('passed'):
                tid = entry.get('topic_id', '')
                bl = entry.get('bloom_level', 0)
                depths[tid] = max(depths.get(tid, 0), bl)
        return depths

    # ═══════════════════════════════════════════════════════════
    # Шаг 2: Фаза
    # ═══════════════════════════════════════════════════════════

    def _determine_phase(self, student_stage: int) -> int:
        return STAGE_TO_PHASE.get(student_stage, 1)

    # ═══════════════════════════════════════════════════════════
    # Шаг 3: Направление
    # ═══════════════════════════════════════════════════════════

    def _choose_direction(
        self,
        phase: int,
        state: str,
        energy: int,
        last_direction: Optional[int],
        history: List[dict],
    ) -> Optional[int]:
        """Weighted random с корректировками (SOP.001 Шаг 3)."""
        base_weights = PHASE_WEIGHTS.get(phase, PHASE_WEIGHTS[1]).copy()

        # Корректировка по состоянию
        adjustments = STATE_ADJUSTMENTS.get(state, {})
        for d, mult in adjustments.items():
            if d in base_weights:
                base_weights[d] *= mult

        # Корректировка по энергии
        if energy <= 2:
            base_weights[6] = base_weights.get(6, 1.0) * 1.5

        # Ротация: обнулить вчерашнее направление
        if last_direction and last_direction in base_weights:
            base_weights[last_direction] = 0.0

        # Фильтр: только направления с темами
        available = {
            d: w for d, w in base_weights.items()
            if w > 0 and d in DIRECTION_TOPICS
        }
        if not available:
            # Fallback: любое с темами
            available = {
                d: 1.0 for d in DIRECTION_TOPICS
            }

        # Weighted random
        total = sum(available.values())
        if total <= 0:
            return None

        r = random.random() * total
        cumulative = 0.0
        for d, w in available.items():
            cumulative += w
            if r <= cumulative:
                return d

        return list(available.keys())[0]

    # ═══════════════════════════════════════════════════════════
    # Шаг 4: Тема и глубина
    # ═══════════════════════════════════════════════════════════

    def _choose_topic(
        self,
        direction: int,
        history: List[dict],
    ) -> Tuple[Optional[str], int]:
        """Bottleneck-first: тема с max gap (SOP.001 Шаг 4)."""
        topics = DIRECTION_TOPICS.get(direction, [])
        if not topics:
            return None, 0

        # Текущие глубины из истории
        depths = self._compute_depths(history)

        # Для каждой темы: gap = target - current
        # Target для MVP = 2 (max доступная глубина)
        target_depth = 2
        scored = []
        for tid in topics:
            current = depths.get(tid, 0)
            gap = target_depth - current
            if gap <= 0:
                gap = 0.1  # уже пройдено — низкий приоритет
            scored.append((tid, gap, current))

        # Сортировка по gap (убывание)
        scored.sort(key=lambda x: -x[1])

        # Выбираем тему с наибольшим gap
        best_topic, best_gap, current_depth = scored[0]

        # Глубина: current + 1, но не больше доступных
        available = get_available_depths(best_topic)
        next_depth = current_depth + 1
        if next_depth not in available:
            next_depth = available[0] if available else 1

        return best_topic, next_depth

    # ═══════════════════════════════════════════════════════════
    # Шаг 7: Упаковка
    # ═══════════════════════════════════════════════════════════

    def _pack_lesson(
        self,
        cell: dict,
        topic_id: str,
        direction: int,
        bloom_depth: int,
        phase: int,
        context: dict,
        history: List[dict],
    ) -> dict:
        """Упаковать ячейку в structured lesson (SOP.001 Шаг 7)."""
        # Извлечь bridge из ячейки
        bridge = cell.get('bridge_template')

        # Выбрать форму подачи: postformal (взрослая аудитория)
        forms = cell.get('forms', {})
        form = forms.get('postformal', forms.get('formal_operational', ''))

        # Assessment
        assessment = cell.get('assessment', {})

        # Retrieval: вопрос по предыдущей теме
        retrieval = self._build_retrieval(history)

        # IT scaffolding
        it_scaffolding = self._build_it_scaffolding(context['it_level'])

        return {
            'direction': direction,
            'topic_id': topic_id,
            'topic_name': cell.get('principle_name', get_topic_name(topic_id)),
            'bloom_depth': bloom_depth,
            'bloom_level': cell.get('bloom_level', 'Remember'),
            'phase': phase,

            # Контент
            'can_do': cell.get('can_do', []),
            'methods_notes': cell.get('methods', {}).get('notes', ''),
            'form': form,
            'common_errors': cell.get('common_errors', []),
            'domains': cell.get('domains', []),

            # Проверка
            'assessment': {
                'type': assessment.get('type', 'observation'),
                'criteria': assessment.get('criteria', ''),
                'transfer_test': assessment.get('transfer_test', ''),
                'retrieval_test': assessment.get('retrieval_test', ''),
            },

            # Связки
            'bridge': bridge,
            'retrieval': retrieval,
            'it_scaffolding': it_scaffolding,

            # Метаданные
            'metadata': {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'source': 'cell',
                'cell_id': f"{topic_id}@{bloom_depth}",
                'tailor_context': {
                    'phase': phase,
                    'student_stage': context['student_stage'],
                    'it_level': context['it_level'],
                    'state': context['state'],
                    'energy': context['energy'],
                },
            },
        }

    def _build_retrieval(self, history: List[dict]) -> Optional[str]:
        """Recall по предыдущей теме (MIM.M.011 Retrieval Practice)."""
        passed = [
            h for h in history
            if h.get('passed') and h.get('topic_id')
        ]
        if not passed:
            return None

        # Последняя пройденная тема
        last = passed[-1]
        topic_name = get_topic_name(last['topic_id'])
        return (
            f"Вспомни предыдущее занятие по теме "
            f"«{topic_name}». Что ты запомнил? "
            f"Попробуй назвать 2-3 ключевых мысли."
        )

    def _build_it_scaffolding(self, it_level: int) -> str:
        """Подводка к следующему ИТ-уровню (SOP.001 §Scaffolding)."""
        scaffolding = {
            0: (
                "Это занятие ты проходишь через бот. "
                "Скоро мы покажем, как сохранять заметки в файл — "
                "это первый шаг к твоей интеллектуальной рабочей среде (IWE)."
            ),
            1: (
                "Попробуй записать ключевые мысли из занятия "
                "в файл на компьютере (хотя бы в текстовый редактор). "
                "Это начало твоего экзокортекса."
            ),
            2: (
                "Создай заметку в своём экзокортексе (inbox). "
                "Запиши 2-3 различения из этого занятия. "
                "Используй markdown."
            ),
            3: (
                "Проанализируй материал занятия с помощью Claude Code. "
                "Попробуй связать его с тем, что уже есть в твоём Pack."
            ),
        }
        return scaffolding.get(it_level, scaffolding[0])
