"""
TailorEngine — сборка персонального занятия (WP-149, MIM.SOP.001 v3).

7 шагов SOP.001:
  1. Прочитать профиль и состояние → context
  2. Определить фазу программы
  3. Выбрать область (5 областей) и тип воздействия (worldview/mastery)
  4. Выбрать элемент из каталога (bottleneck-first)
  5. Собрать контент (ячейка, мем или практика)
  6. Адаптировать (домен, стиль, ошибки, ИТ-уровень)
  7. Упаковать (structured JSON)

Архитектура: 2 объекта (Мировоззрение + Мастерство) × 5 областей (FORM.081).
Портной работает от GAP, не от порядка модулей.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .catalogs import (
    get_memes_for_area,
    get_practices_for_area,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 5 областей (PD.FORM.081)
# ═══════════════════════════════════════════════════════════
AREA_NAMES = {
    1: 'Знания',
    2: 'Инструменты',
    3: 'Ограничения',
    4: 'Окружение',
    5: 'Организм',
}

# ═══════════════════════════════════════════════════════════
# Матрица весов: фаза × область (из SOP.001 v3)
# ═══════════════════════════════════════════════════════════
PHASE_WEIGHTS = {
    1: {1: 1.5, 2: 0.5, 3: 0.8, 4: 0.7, 5: 1.0},   # Ф1 Основания
    2: {1: 1.0, 2: 1.0, 3: 0.8, 4: 0.8, 5: 1.0},   # Ф2 Различения
    3: {1: 0.8, 2: 1.5, 3: 0.8, 4: 0.5, 5: 1.4},   # Ф3 Создание
    4: {1: 0.8, 2: 1.8, 3: 0.5, 4: 0.8, 5: 1.4},   # Ф4 Автономия
}

# Корректировка по состоянию (SOP.001 Шаг 3)
STATE_ADJUSTMENTS = {
    'chaos':       {5: 2.0, 1: 0.5},     # Организм ↑, Знания ↓
    'stuck':       {3: 1.5, 1: 1.5},     # Ограничения ↑, Знания ↑
    'turn':        {},
    'development': {2: 1.5},              # Инструменты ↑
}

# Ступень → Фаза (SOP.001 Шаг 2)
STAGE_TO_PHASE = {0: 1, 1: 1, 2: 2, 3: 3, 4: 4}

# Соотношение worldview/mastery по ступени (SOP.001 Шаг 3.8)
WORLDVIEW_RATIO = {
    0: 0.80,  # 80% worldview, 20% mastery
    1: 0.80,
    2: 0.50,  # 50/50
    3: 0.50,
    4: 0.20,  # 20% worldview, 80% mastery
}


class TailorEngine:
    """Собирает персональное занятие из каталогов и ячеек (SOP.001 v3)."""

    def assemble(
        self,
        user_profile: dict,
        learning_history: Optional[List[dict]] = None,
        last_area: Optional[int] = None,
        gap_report: Optional[Dict[int, dict]] = None,
    ) -> Optional[dict]:
        """Собрать одно персональное занятие (7 шагов SOP.001).

        Args:
            user_profile: профиль из ЦД
                {name, occupation, goals, interests, assessment_state,
                 student_stage, it_level, energy}
            learning_history: история прохождения
                [{element_id, element_type, passed, area, impact_type, ...}]
            last_area: область вчерашнего занятия (для ротации)
            gap_report: GAP-отчёт от Диагноста (MCP tool), если есть
                {area_id: {worldview_gap: float, mastery_gap: float}}

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

        # ─── Шаг 3: Область и тип воздействия ───
        area, impact_type = self._choose_area_and_type(
            phase=phase,
            student_stage=ctx['student_stage'],
            state=ctx['state'],
            energy=ctx['energy'],
            last_area=last_area,
            history=history,
            gap_report=gap_report,
        )
        if area is None:
            logger.warning("[Tailor] No area available")
            return None
        logger.info(
            f"[Tailor] Step 3: area={area} ({AREA_NAMES[area]}), "
            f"type={impact_type}"
        )

        # ─── Шаг 4: Элемент из каталога ───
        element = self._choose_element(
            area=area,
            impact_type=impact_type,
            student_stage=ctx['student_stage'],
            history=history,
        )
        if element is None:
            logger.warning(
                f"[Tailor] No element for area={area}, type={impact_type}"
            )
            return None
        logger.info(
            f"[Tailor] Step 4: element={element['element_id']}"
        )

        # ─── Шаг 5: Контент ───
        content = self._gather_content(element, impact_type)
        if content is None:
            logger.warning(
                f"[Tailor] No content for {element['element_id']}"
            )
            return None

        # ─── Шаг 6: Адаптация (в MVP — минимальная) ───
        # Полная адаптация через Claude — в следующих фазах.

        # ─── Шаг 7: Упаковка ───
        lesson = self._pack_lesson(
            content=content,
            element=element,
            area=area,
            impact_type=impact_type,
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
        }

    # ═══════════════════════════════════════════════════════════
    # Шаг 2: Фаза
    # ═══════════════════════════════════════════════════════════

    def _determine_phase(self, student_stage: int) -> int:
        return STAGE_TO_PHASE.get(student_stage, 1)

    # ═══════════════════════════════════════════════════════════
    # Шаг 3: Область и тип воздействия
    # ═══════════════════════════════════════════════════════════

    def _choose_area_and_type(
        self,
        phase: int,
        student_stage: int,
        state: str,
        energy: int,
        last_area: Optional[int],
        history: List[dict],
        gap_report: Optional[Dict[int, dict]] = None,
    ) -> Tuple[Optional[int], str]:
        """Выбрать область (1-5) и тип (worldview/mastery)."""

        # --- Выбор области ---
        base_weights = PHASE_WEIGHTS.get(phase, PHASE_WEIGHTS[1]).copy()

        # Корректировка по состоянию
        adjustments = STATE_ADJUSTMENTS.get(state, {})
        for a, mult in adjustments.items():
            if a in base_weights:
                base_weights[a] *= mult

        # Корректировка по энергии
        if energy <= 2:
            base_weights[5] = base_weights.get(5, 1.0) * 1.5

        # Ротация: обнулить вчерашнюю область (R1)
        if last_area and last_area in base_weights:
            base_weights[last_area] = 0.0

        # Фильтр: только области с весом > 0
        available = {a: w for a, w in base_weights.items() if w > 0}
        if not available:
            available = {a: 1.0 for a in AREA_NAMES}

        # Если есть GAP-отчёт — max(вес × gap)
        if gap_report:
            scored = {}
            for a, w in available.items():
                area_gap = gap_report.get(a, {})
                total_gap = (
                    area_gap.get('worldview_gap', 0)
                    + area_gap.get('mastery_gap', 0)
                )
                scored[a] = w * max(total_gap, 0.1)
            best_area = max(scored, key=scored.get)
        else:
            # Fallback: weighted random (без GAP)
            total = sum(available.values())
            r = random.random() * total
            cumulative = 0.0
            best_area = list(available.keys())[0]
            for a, w in available.items():
                cumulative += w
                if r <= cumulative:
                    best_area = a
                    break

        # --- Выбор типа ---
        impact_type = self._choose_impact_type(
            student_stage=student_stage,
            area=best_area,
            gap_report=gap_report,
        )

        return best_area, impact_type

    def _choose_impact_type(
        self,
        student_stage: int,
        area: int,
        gap_report: Optional[Dict[int, dict]] = None,
    ) -> str:
        """Выбрать worldview или mastery.

        Базовое соотношение по ступени, с корректировкой по GAP если есть.
        """
        base_ratio = WORLDVIEW_RATIO.get(student_stage, 0.50)

        if gap_report and area in gap_report:
            area_gap = gap_report[area]
            wv_gap = area_gap.get('worldview_gap', 0)
            ms_gap = area_gap.get('mastery_gap', 0)
            if wv_gap + ms_gap > 0:
                gap_ratio = wv_gap / (wv_gap + ms_gap)
                # Смешиваем базовое и GAP (50/50)
                base_ratio = (base_ratio + gap_ratio) / 2

        return 'worldview' if random.random() < base_ratio else 'mastery'

    # ═══════════════════════════════════════════════════════════
    # Шаг 4: Элемент из каталога
    # ═══════════════════════════════════════════════════════════

    def _choose_element(
        self,
        area: int,
        impact_type: str,
        student_stage: int,
        history: List[dict],
        _switched: bool = False,
    ) -> Optional[dict]:
        """Выбрать элемент из каталога по области и типу.

        Depth-aware: если элемент пройден на глубине N, предлагаем N+1.
        Мемы: max 3 глубины. Практики: max 4 степени.

        Returns:
            {element_id, element_type, area, impact_type, depth, data} или None
        """
        # Depth-aware tracking: element_id → max passed depth
        completed_depths = self._get_completed_depths(history)

        if impact_type == 'worldview':
            max_depth = 3  # Осознание → Различение → Компиляция
            memes = get_memes_for_area(area, student_stage)

            # Найти мем с наибольшим gap (не все глубины пройдены)
            best = None
            best_gap = -1
            for m in memes:
                current = completed_depths.get(m['id'], 0)
                gap = max_depth - current
                if gap > 0 and gap > best_gap:
                    best = m
                    best_gap = gap

            if best is None:
                if _switched:
                    return None
                return self._choose_element(
                    area, 'mastery', student_stage, history,
                    _switched=True,
                )

            next_depth = completed_depths.get(best['id'], 0) + 1
            return {
                'element_id': best['id'],
                'element_type': 'meme',
                'area': area,
                'impact_type': 'worldview',
                'depth': next_depth,
                'data': best,
            }
        else:
            # Практики: степень = depth (1-4)
            # Ceiling по student_stage
            stage_max = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4}
            max_degree = stage_max.get(student_stage, 1)

            practices = get_practices_for_area(area, student_stage)

            best = None
            best_gap = -1
            for p in practices:
                current = completed_depths.get(p['id'], 0)
                target = min(current + 1, max_degree)
                gap = max_degree - current
                if gap > 0 and gap > best_gap:
                    best = p
                    best_gap = gap

            if best is None:
                if _switched:
                    return None
                return self._choose_element(
                    area, 'worldview', student_stage, history,
                    _switched=True,
                )

            next_depth = min(completed_depths.get(best['id'], 0) + 1, max_degree)
            return {
                'element_id': best['id'],
                'element_type': 'practice',
                'area': area,
                'impact_type': 'mastery',
                'depth': next_depth,
                'data': best,
            }

    def _get_completed_depths(self, history: List[dict]) -> Dict[str, int]:
        """Построить карту element_id → max passed depth."""
        depths = {}
        for h in history:
            if h.get('passed') and h.get('element_id'):
                eid = h['element_id']
                d = h.get('depth', 1)
                depths[eid] = max(depths.get(eid, 0), d)
        return depths

    # ═══════════════════════════════════════════════════════════
    # Шаг 5: Контент
    # ═══════════════════════════════════════════════════════════

    def _gather_content(
        self,
        element: dict,
        impact_type: str,
    ) -> Optional[dict]:
        """Собрать контент из элемента каталога с учётом глубины."""
        data = element.get('data', {})
        depth = element.get('depth', 1)

        if impact_type == 'worldview':
            # Извлечь depth-specific данные если есть
            depths = data.get('depths', {})
            depth_data = depths.get(str(depth), {})

            return {
                'type': 'worldview',
                'depth': depth,
                'depth_name': depth_data.get('name', self._MEME_DEPTH_NAMES.get(depth, '')),
                'meme_unproductive': data.get('unproductive', ''),
                'meme_productive': data.get('productive', ''),
                'meme_function': data.get('function', ''),
                'compilation_method': data.get('compilation_method', ''),
                'diagnostic': data.get('diagnostic', ''),
                'source': data.get('source', ''),
                # Depth-specific
                'depth_can_do': depth_data.get('can_do', ''),
                'depth_activity': depth_data.get('activity', ''),
                'depth_assessment': depth_data.get('assessment', ''),
            }
        else:
            # Для практик depth = degree (1-4)
            # Данные уже подготовлены catalogs.py с нужной степенью
            return {
                'type': 'mastery',
                'depth': depth,
                'practice_name': data.get('name', ''),
                'current_degree': depth,
                'can_do': data.get('can_do', ''),
                'assignment': data.get('assignment', ''),
                'assessment': data.get('assessment', ''),
            }


    # Названия глубин для мемов (fallback если нет в JSON)
    _MEME_DEPTH_NAMES = {
        1: 'Осознание',
        2: 'Различение',
        3: 'Компиляция',
    }

    # ═══════════════════════════════════════════════════════════
    # Шаг 7: Упаковка
    # ═══════════════════════════════════════════════════════════

    def _pack_lesson(
        self,
        content: dict,
        element: dict,
        area: int,
        impact_type: str,
        phase: int,
        context: dict,
        history: List[dict],
    ) -> dict:
        """Упаковать в structured lesson (SOP.001 Шаг 7)."""
        retrieval = self._build_retrieval(history)
        it_scaffolding = self._build_it_scaffolding(context['it_level'])

        return {
            'area': area,
            'area_name': AREA_NAMES.get(area, ''),
            'impact_type': impact_type,
            'element_id': element['element_id'],
            'element_type': element['element_type'],
            'depth': element.get('depth', 1),
            'phase': phase,

            # Контент
            'content': content,

            # Связки
            'retrieval': retrieval,
            'it_scaffolding': it_scaffolding,

            # Метаданные
            'metadata': {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'source': element['element_type'],
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
        """Recall по предыдущему занятию (MIM.M.011 Retrieval Practice)."""
        passed = [
            h for h in history
            if h.get('passed') and h.get('element_id')
        ]
        if not passed:
            return None

        last = passed[-1]
        etype = last.get('element_type', '')
        eid = last.get('element_id', '')
        if etype == 'meme':
            return (
                f"В прошлый раз мы разбирали мем ({eid}). "
                f"Что ты запомнил? Заметил ли это убеждение у себя?"
            )
        return (
            f"В прошлый раз ты работал над практикой ({eid}). "
            f"Что получилось? Что оказалось сложнее, чем думал?"
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
