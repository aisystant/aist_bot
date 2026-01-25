#!/usr/bin/env python3
"""
Основной скрипт проверки соответствия кода сценариям.

Использование:
    python -m tests.test_repo.scripts.checker

    # или напрямую
    python tests/test-repo/scripts/checker.py

Опции:
    --output, -o    Путь для сохранения отчёта (по умолчанию: reports/<дата>-тест-сценариев.md)
    --verbose, -v   Подробный вывод
    --json          Вывод в JSON формате
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .code_analyzer import CodeAnalyzer
from .report_generator import ReportGenerator, ClassResult, ScenarioResult


def load_requirements(requirements_path: Path) -> dict:
    """Загружает ТЗ из YAML файла."""
    with open(requirements_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def run_checks(project_root: Path, requirements: dict, verbose: bool = False) -> list[ClassResult]:
    """Выполняет все проверки и возвращает результаты."""
    analyzer = CodeAnalyzer(project_root)
    results = []

    # Проходим по всем классам сценариев
    for key, value in requirements.items():
        if not key.startswith('class_'):
            continue

        class_id = key
        class_name = value.get('name', key)
        scenarios_data = value.get('scenarios', [])

        if verbose:
            print(f"\n📋 Класс: {class_name}")

        scenario_results = []

        for scenario_data in scenarios_data:
            scenario_id = scenario_data.get('id', '?')
            scenario_name = scenario_data.get('name', 'Без названия')
            priority = scenario_data.get('priority', 'normal')
            checks_data = scenario_data.get('checks', [])

            if verbose:
                print(f"  └─ {scenario_id} {scenario_name}", end='')

            check_results = []
            for check_data in checks_data:
                result = analyzer.run_check(check_data)
                check_results.append(result)

            scenario_result = ScenarioResult(
                id=scenario_id,
                name=scenario_name,
                priority=priority,
                checks=check_results
            )
            scenario_results.append(scenario_result)

            if verbose:
                status = "✅" if scenario_result.passed else "❌"
                print(f" {status} ({scenario_result.passed_count}/{scenario_result.total_count})")

        class_result = ClassResult(
            id=class_id,
            name=class_name,
            scenarios=scenario_results
        )
        results.append(class_result)

    return results


def main():
    """Точка входа."""
    parser = argparse.ArgumentParser(
        description='Проверка соответствия кода сценариям'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help='Путь для сохранения отчёта'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Подробный вывод'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Вывод в JSON формате'
    )
    parser.add_argument(
        '--project-root',
        type=str,
        help='Корень проекта (по умолчанию определяется автоматически)'
    )

    args = parser.parse_args()

    # Определяем пути
    script_dir = Path(__file__).parent
    scenario_dir = script_dir.parent

    if args.project_root:
        project_root = Path(args.project_root)
    else:
        project_root = scenario_dir.parent.parent  # tests/test-repo -> aist_track_bot

    requirements_path = scenario_dir / 'requirements-scenarios.yaml'
    reports_dir = scenario_dir / 'reports'

    # Проверяем наличие ТЗ
    if not requirements_path.exists():
        print(f"❌ Файл ТЗ не найден: {requirements_path}", file=sys.stderr)
        sys.exit(1)

    # Загружаем ТЗ
    if args.verbose:
        print(f"📄 Загрузка ТЗ: {requirements_path}")

    requirements = load_requirements(requirements_path)

    # Выполняем проверки
    if args.verbose:
        print(f"🔍 Проверка кода: {project_root}")

    results = run_checks(project_root, requirements, verbose=args.verbose)

    # Генерируем отчёт
    thresholds = requirements.get('thresholds', {'green': 90, 'yellow': 70})
    weights = requirements.get('weights', {'critical': 2, 'normal': 1})

    generator = ReportGenerator(thresholds=thresholds, weights=weights)

    # Определяем путь для отчёта
    if args.output:
        output_path = Path(args.output)
    else:
        date_str = datetime.now().strftime('%Y-%m-%d')
        output_path = reports_dir / f'{date_str}-тест-сценариев.md'

    # Генерируем и сохраняем
    report = generator.generate_report(
        results,
        output_path,
        project_name="AIST Track Bot"
    )

    # Вычисляем итоговое покрытие
    total_scenarios = sum(c.total_scenarios for c in results)
    passed_scenarios = sum(c.passed_scenarios for c in results)
    coverage = (passed_scenarios / total_scenarios * 100) if total_scenarios else 0

    # Основные сценарии (critical)
    critical_total = sum(c.critical_total for c in results)
    critical_passed = sum(c.critical_passed for c in results)
    critical_coverage = (critical_passed / critical_total * 100) if critical_total else 100

    # Вспомогательные сценарии (normal)
    normal_total = sum(c.normal_total for c in results)
    normal_passed = sum(c.normal_passed for c in results)
    normal_coverage = (normal_passed / normal_total * 100) if normal_total else 100

    # Логика цветов:
    # 🟢 Зелёный: основные = 100% И вспомогательные = 100%
    # 🟡 Жёлтый: основные = 100% И общее ≥ 60%
    # 🔴 Красный: основные < 100% ИЛИ общее < 50%
    def get_status(cov: float, crit_cov: float, norm_cov: float) -> str:
        if crit_cov == 100 and norm_cov == 100:
            return 'green'
        if crit_cov == 100 and cov >= 60:
            return 'yellow'
        return 'red'

    status_code = get_status(coverage, critical_coverage, normal_coverage)

    if args.json:
        # JSON вывод
        json_result = {
            'date': datetime.now().isoformat(),
            'coverage': round(coverage, 1),
            'critical_coverage': round(critical_coverage, 1),
            'normal_coverage': round(normal_coverage, 1),
            'passed': passed_scenarios,
            'total': total_scenarios,
            'critical_passed': critical_passed,
            'critical_total': critical_total,
            'normal_passed': normal_passed,
            'normal_total': normal_total,
            'report_path': str(output_path),
            'status': status_code,
            'classes': [
                {
                    'id': c.id,
                    'name': c.name,
                    'coverage': round(c.coverage, 1),
                    'passed': c.passed_scenarios,
                    'total': c.total_scenarios,
                    'critical_passed': c.critical_passed,
                    'critical_total': c.critical_total,
                    'normal_passed': c.normal_passed,
                    'normal_total': c.normal_total
                }
                for c in results
            ]
        }
        print(json.dumps(json_result, ensure_ascii=False, indent=2))
    else:
        # Текстовый вывод
        emoji_map = {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}
        status = emoji_map[status_code]

        print(f"\n{status} Покрытие: {coverage:.1f}% ({passed_scenarios}/{total_scenarios})")
        print(f"   Основные: {critical_coverage:.1f}% ({critical_passed}/{critical_total})")
        print(f"   Вспомогательные: {normal_coverage:.1f}% ({normal_passed}/{normal_total})")
        print(f"📝 Отчёт сохранён: {output_path}")

    # Возвращаем код выхода: 0 если зелёный или жёлтый
    if status_code in ('green', 'yellow'):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
