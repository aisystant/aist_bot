"""
Генератор отчётов о соответствии кода сценариям.

Создаёт markdown-отчёт с цветовой индикацией:
- 🟢 Зелёный: ≥90% покрытия
- 🟡 Жёлтый: 70-89% покрытия
- 🔴 Красный: <70% покрытия
"""

from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from .code_analyzer import CheckResult


@dataclass
class ScenarioResult:
    """Результат проверки сценария."""
    id: str
    name: str
    priority: str
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        """Сценарий пройден, если все проверки успешны."""
        return all(c.passed for c in self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)


@dataclass
class ClassResult:
    """Результат проверки класса сценариев."""
    id: str
    name: str
    scenarios: list[ScenarioResult]

    @property
    def passed_scenarios(self) -> int:
        return sum(1 for s in self.scenarios if s.passed)

    @property
    def total_scenarios(self) -> int:
        return len(self.scenarios)

    @property
    def coverage(self) -> float:
        if self.total_scenarios == 0:
            return 0.0
        return (self.passed_scenarios / self.total_scenarios) * 100

    @property
    def critical_passed(self) -> int:
        return sum(1 for s in self.scenarios if s.priority == 'critical' and s.passed)

    @property
    def critical_total(self) -> int:
        return sum(1 for s in self.scenarios if s.priority == 'critical')

    @property
    def normal_passed(self) -> int:
        return sum(1 for s in self.scenarios if s.priority == 'normal' and s.passed)

    @property
    def normal_total(self) -> int:
        return sum(1 for s in self.scenarios if s.priority == 'normal')


class ReportGenerator:
    """Генератор отчётов."""

    def __init__(
        self,
        thresholds: Optional[dict] = None,
        weights: Optional[dict] = None
    ):
        self.thresholds = thresholds or {'green': 90, 'yellow': 70}
        self.weights = weights or {'critical': 2, 'normal': 1}

    def _get_status_emoji(
        self,
        coverage: float,
        critical_coverage: float = 100.0,
        normal_coverage: float = 100.0
    ) -> str:
        """Возвращает эмодзи статуса по покрытию.

        Логика цветов:
        - 🟢 Зелёный: основные = 100% И вспомогательные = 100%
        - 🟡 Жёлтый: основные = 100% И общее ≥ 60%
        - 🔴 Красный: основные < 100% ИЛИ общее < 50%
        """
        # Зелёный: ВСЕ основные И ВСЕ вспомогательные работают
        if critical_coverage == 100 and normal_coverage == 100:
            return '🟢'

        # Жёлтый: все основные работают и общее покрытие достаточное
        if critical_coverage == 100 and coverage >= 60:
            return '🟡'

        # Красный: основные не работают полностью или покрытие критически низкое
        return '🔴'

    def _get_status_text(
        self,
        coverage: float,
        critical_coverage: float = 100.0,
        normal_coverage: float = 100.0
    ) -> str:
        """Возвращает текст статуса по покрытию."""
        # Зелёный
        if critical_coverage == 100 and normal_coverage == 100:
            return 'Отлично — все сценарии работают'

        # Жёлтый
        if critical_coverage == 100 and coverage >= 60:
            return 'Требует внимания — вспомогательные сценарии не все работают'

        # Красный
        if critical_coverage < 100:
            return 'Критично — не все основные сценарии работают'
        return 'Критично — общее покрытие слишком низкое'

    def _calculate_weighted_coverage(self, classes: list[ClassResult]) -> float:
        """Вычисляет взвешенное покрытие с учётом приоритетов."""
        total_weight = 0
        weighted_passed = 0

        for cls in classes:
            for scenario in cls.scenarios:
                weight = self.weights.get(scenario.priority, 1)
                total_weight += weight
                if scenario.passed:
                    weighted_passed += weight

        if total_weight == 0:
            return 0.0
        return (weighted_passed / total_weight) * 100

    def generate_report(
        self,
        classes: list[ClassResult],
        output_path: Path,
        project_name: str = "AIST Track Bot"
    ) -> str:
        """Генерирует markdown-отчёт."""
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')

        # Общая статистика
        total_scenarios = sum(c.total_scenarios for c in classes)
        passed_scenarios = sum(c.passed_scenarios for c in classes)
        simple_coverage = (passed_scenarios / total_scenarios * 100) if total_scenarios else 0
        weighted_coverage = self._calculate_weighted_coverage(classes)

        # Критические сценарии (основные)
        critical_total = sum(c.critical_total for c in classes)
        critical_passed = sum(c.critical_passed for c in classes)
        critical_coverage = (critical_passed / critical_total * 100) if critical_total else 100

        # Вспомогательные сценарии (normal)
        normal_total = sum(c.normal_total for c in classes)
        normal_passed = sum(c.normal_passed for c in classes)
        normal_coverage = (normal_passed / normal_total * 100) if normal_total else 100

        # Логика цветов:
        # 🟢 Зелёный: critical ≥90% И normal ≥80% И общее ≥90%
        # 🟡 Жёлтый: critical ≥80% И общее ≥70%
        # 🔴 Красный: иначе
        main_status = self._get_status_emoji(weighted_coverage, critical_coverage, normal_coverage)
        critical_status = self._get_status_emoji(critical_coverage, critical_coverage, 100)

        lines = []

        # Заголовок с общим статусом
        lines.append(f"# {main_status} Тест сценариев — {date_str}")
        lines.append("")
        lines.append(f"> **Проект:** {project_name}")
        lines.append(f"> **Время проверки:** {time_str} MSK")
        lines.append(f"> **Версия ТЗ:** 1.0")
        lines.append("")

        # Общий статус
        lines.append("## Общий статус")
        lines.append("")
        lines.append("| Метрика | Значение | Статус |")
        lines.append("|---------|----------|--------|")
        lines.append(f"| **Взвешенное покрытие** | {weighted_coverage:.1f}% | {main_status} {self._get_status_text(weighted_coverage, critical_coverage, normal_coverage)} |")
        lines.append(f"| Простое покрытие | {simple_coverage:.1f}% ({passed_scenarios}/{total_scenarios}) | — |")
        lines.append(f"| **Основные сценарии** | {critical_coverage:.1f}% ({critical_passed}/{critical_total}) | {critical_status} |")
        lines.append(f"| Вспомогательные сценарии | {normal_coverage:.1f}% ({normal_passed}/{normal_total}) | — |")
        lines.append("")

        # Легенда
        lines.append("### Легенда")
        lines.append("")
        lines.append(f"| Статус | Условие | Интерпретация |")
        lines.append("|--------|---------|---------------|")
        lines.append(f"| 🟢 | Основные = 100% И вспомогательные = 100% | Бот работает — все сценарии покрыты |")
        lines.append(f"| 🟡 | Основные = 100% И общее ≥ 60% | Бот работает — вспомогательные не все |")
        lines.append(f"| 🔴 | Основные < 100% ИЛИ общее < 50% | Бот может не работать |")
        lines.append("")

        # Сводка по классам
        lines.append("---")
        lines.append("")
        lines.append("## Сводка по классам")
        lines.append("")
        lines.append("| № | Класс | Покрытие | Критичные | Статус |")
        lines.append("|---|-------|----------|-----------|--------|")

        for i, cls in enumerate(classes, 1):
            # Статус класса зависит от его критических и вспомогательных сценариев
            cls_critical_coverage = (cls.critical_passed / cls.critical_total * 100) if cls.critical_total else 100
            cls_normal_coverage = (cls.normal_passed / cls.normal_total * 100) if cls.normal_total else 100
            status = self._get_status_emoji(cls.coverage, cls_critical_coverage, cls_normal_coverage)
            critical_str = f"{cls.critical_passed}/{cls.critical_total}" if cls.critical_total else "—"
            lines.append(
                f"| {i} | [{cls.name}](#{cls.id}) | "
                f"{cls.coverage:.0f}% ({cls.passed_scenarios}/{cls.total_scenarios}) | "
                f"{critical_str} | {status} |"
            )

        lines.append("")

        # Детали по каждому классу
        lines.append("---")
        lines.append("")
        lines.append("## Детальные результаты")
        lines.append("")

        for cls in classes:
            cls_critical_coverage = (cls.critical_passed / cls.critical_total * 100) if cls.critical_total else 100
            cls_normal_coverage = (cls.normal_passed / cls.normal_total * 100) if cls.normal_total else 100
            status = self._get_status_emoji(cls.coverage, cls_critical_coverage, cls_normal_coverage)
            lines.append(f"### {status} {cls.name}")
            lines.append("")
            lines.append(f"<a id=\"{cls.id}\"></a>")
            lines.append("")
            lines.append(f"**Покрытие:** {cls.coverage:.0f}% ({cls.passed_scenarios}/{cls.total_scenarios})")
            lines.append("")

            # Таблица сценариев
            lines.append("| ID | Сценарий | Приоритет | Проверки | Статус |")
            lines.append("|----|----------|-----------|----------|--------|")

            for scenario in cls.scenarios:
                s_status = "✅" if scenario.passed else "❌"
                priority_icon = "🔥" if scenario.priority == 'critical' else "📋"
                lines.append(
                    f"| {scenario.id} | {scenario.name} | "
                    f"{priority_icon} {scenario.priority} | "
                    f"{scenario.passed_count}/{scenario.total_count} | {s_status} |"
                )

            lines.append("")

            # Детали непройденных проверок
            failed_checks = [
                (s, c) for s in cls.scenarios for c in s.checks if not c.passed
            ]

            if failed_checks:
                lines.append("<details>")
                lines.append(f"<summary>❌ Непройденные проверки ({len(failed_checks)})</summary>")
                lines.append("")

                for scenario, check in failed_checks:
                    lines.append(f"- **{scenario.id} {scenario.name}**")
                    lines.append(f"  - Тип: `{check.check_type}`")
                    lines.append(f"  - Файл: `{check.file}`")
                    lines.append(f"  - Искали: `{check.name}`")
                    lines.append(f"  - Описание: {check.description}")
                    if check.details:
                        lines.append(f"  - Детали: {check.details}")
                    lines.append("")

                lines.append("</details>")
                lines.append("")

            lines.append("---")
            lines.append("")

        # Рекомендации
        lines.append("## Рекомендации")
        lines.append("")

        if weighted_coverage >= self.thresholds['green']:
            lines.append("🟢 **Код хорошо соответствует сценариям.** Продолжайте поддерживать текущий уровень.")
        elif weighted_coverage >= self.thresholds['yellow']:
            lines.append("🟡 **Требуется внимание.** Рекомендуется:")
            lines.append("")
            lines.append("1. Проверить непройденные сценарии в классах ниже порога")
            lines.append("2. Приоритизировать критические сценарии")
            lines.append("3. Обновить ТЗ если какие-то проверки устарели")
        else:
            lines.append("🔴 **Критическая ситуация.** Необходимо:")
            lines.append("")
            lines.append("1. **Срочно** проверить критические сценарии")
            lines.append("2. Проверить, не сломалась ли кодовая база")
            lines.append("3. Возможно, ТЗ требует актуализации под текущий код")

        lines.append("")

        # Непройденные критические
        failed_critical = [
            (cls, s) for cls in classes
            for s in cls.scenarios
            if s.priority == 'critical' and not s.passed
        ]

        if failed_critical:
            lines.append("### Непройденные критические сценарии")
            lines.append("")
            for cls, scenario in failed_critical:
                lines.append(f"- **{scenario.id}** {scenario.name} (класс: {cls.name})")
            lines.append("")

        # Футер
        lines.append("---")
        lines.append("")
        lines.append(f"*Сгенерировано автоматически {date_str} {time_str}*")

        report = '\n'.join(lines)

        # Сохраняем отчёт
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding='utf-8')

        return report
