"""
Анализатор кода для проверки соответствия сценариям.

Использует AST для анализа Python-кода и regex для поиска паттернов.
"""

import ast
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class CheckResult:
    """Результат проверки одного требования."""
    passed: bool
    check_type: str
    file: str
    name: str
    description: str
    details: Optional[str] = None


class CodeAnalyzer:
    """Анализатор кода проекта."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self._cache: dict[str, str] = {}
        self._ast_cache: dict[str, ast.Module] = {}

    def _read_file(self, relative_path: str) -> Optional[str]:
        """Читает файл и кэширует содержимое."""
        if relative_path in self._cache:
            return self._cache[relative_path]

        file_path = self.project_root / relative_path
        if not file_path.exists():
            return None

        try:
            content = file_path.read_text(encoding='utf-8')
            self._cache[relative_path] = content
            return content
        except Exception:
            return None

    def _get_ast(self, relative_path: str) -> Optional[ast.Module]:
        """Парсит файл в AST и кэширует результат."""
        if relative_path in self._ast_cache:
            return self._ast_cache[relative_path]

        content = self._read_file(relative_path)
        if content is None:
            return None

        try:
            tree = ast.parse(content)
            self._ast_cache[relative_path] = tree
            return tree
        except SyntaxError:
            return None

    def check_function(self, file: str, name: str, description: str) -> CheckResult:
        """Проверяет наличие функции по имени."""
        tree = self._get_ast(file)
        if tree is None:
            return CheckResult(
                passed=False,
                check_type="function",
                file=file,
                name=name,
                description=description,
                details=f"Файл {file} не найден или не парсится"
            )

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return CheckResult(
                        passed=True,
                        check_type="function",
                        file=file,
                        name=name,
                        description=description,
                        details=f"Найдена функция {name}() в строке {node.lineno}"
                    )

        return CheckResult(
            passed=False,
            check_type="function",
            file=file,
            name=name,
            description=description,
            details=f"Функция {name}() не найдена"
        )

    def check_handler(self, file: str, name: str, description: str) -> CheckResult:
        """Проверяет наличие обработчика (функция с декоратором)."""
        tree = self._get_ast(file)
        if tree is None:
            return CheckResult(
                passed=False,
                check_type="handler",
                file=file,
                name=name,
                description=description,
                details=f"Файл {file} не найден или не парсится"
            )

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    # Проверяем наличие декораторов (признак обработчика)
                    has_decorator = len(node.decorator_list) > 0
                    return CheckResult(
                        passed=True,
                        check_type="handler",
                        file=file,
                        name=name,
                        description=description,
                        details=f"Найден обработчик {name}() в строке {node.lineno}" +
                                (f" с {len(node.decorator_list)} декоратором(ами)" if has_decorator else "")
                    )

        return CheckResult(
            passed=False,
            check_type="handler",
            file=file,
            name=name,
            description=description,
            details=f"Обработчик {name}() не найден"
        )

    def check_state(self, file: str, name: str, description: str) -> CheckResult:
        """Проверяет наличие состояния (класс State или переменная)."""
        content = self._read_file(file)
        if content is None:
            return CheckResult(
                passed=False,
                check_type="state",
                file=file,
                name=name,
                description=description,
                details=f"Файл {file} не найден"
            )

        # Ищем класс состояний или переменную
        patterns = [
            rf"class\s+{re.escape(name)}\s*[:\(]",  # class MyStates:
            rf"{re.escape(name)}\s*=",              # my_state = ...
            rf"['\"]?{re.escape(name)}['\"]?",      # строковое значение состояния
        ]

        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                # Находим номер строки
                line_no = content[:match.start()].count('\n') + 1
                return CheckResult(
                    passed=True,
                    check_type="state",
                    file=file,
                    name=name,
                    description=description,
                    details=f"Найдено состояние {name} в строке {line_no}"
                )

        return CheckResult(
            passed=False,
            check_type="state",
            file=file,
            name=name,
            description=description,
            details=f"Состояние {name} не найдено"
        )

    def check_constant(self, file: str, name: str, description: str) -> CheckResult:
        """Проверяет наличие константы."""
        content = self._read_file(file)
        if content is None:
            return CheckResult(
                passed=False,
                check_type="constant",
                file=file,
                name=name,
                description=description,
                details=f"Файл {file} не найден"
            )

        # Для составных имён типа Mode.MARATHON
        if '.' in name:
            parts = name.split('.')
            # Проверяем наличие класса и атрибута
            class_name = parts[0]
            attr_name = parts[1]

            class_pattern = rf"class\s+{re.escape(class_name)}\s*[:\(]"
            attr_pattern = rf"{re.escape(attr_name)}\s*="

            if re.search(class_pattern, content) and re.search(attr_pattern, content):
                return CheckResult(
                    passed=True,
                    check_type="constant",
                    file=file,
                    name=name,
                    description=description,
                    details=f"Найдена константа {name}"
                )
        else:
            # Простое имя константы
            pattern = rf"^{re.escape(name)}\s*=|^\s+{re.escape(name)}\s*="
            if re.search(pattern, content, re.MULTILINE):
                return CheckResult(
                    passed=True,
                    check_type="constant",
                    file=file,
                    name=name,
                    description=description,
                    details=f"Найдена константа {name}"
                )

        return CheckResult(
            passed=False,
            check_type="constant",
            file=file,
            name=name,
            description=description,
            details=f"Константа {name} не найдена"
        )

    def check_localization(self, file: str, keys: list[str], description: str) -> CheckResult:
        """Проверяет наличие ключей локализации."""
        content = self._read_file(file)
        if content is None:
            return CheckResult(
                passed=False,
                check_type="localization",
                file=file,
                name=", ".join(keys),
                description=description,
                details=f"Файл {file} не найден"
            )

        found_keys = []
        missing_keys = []

        for key in keys:
            # Ищем ключ в разных форматах
            patterns = [
                rf'["\']?{re.escape(key)}["\']?\s*[:\[]',  # "key": или key: или key[
                rf'["\']?{re.escape(key)}["\']?\s*=',      # key =
                rf'LANGUAGES.*{re.escape(key)}',          # для языков
            ]

            found = any(re.search(p, content, re.IGNORECASE) for p in patterns)
            if found:
                found_keys.append(key)
            else:
                missing_keys.append(key)

        passed = len(found_keys) > 0 and len(missing_keys) == 0
        return CheckResult(
            passed=passed,
            check_type="localization",
            file=file,
            name=", ".join(keys),
            description=description,
            details=f"Найдены: {found_keys}" + (f", отсутствуют: {missing_keys}" if missing_keys else "")
        )

    def check_pattern(self, file: str, pattern: str, description: str) -> CheckResult:
        """Проверяет наличие паттерна в коде."""
        content = self._read_file(file)
        if content is None:
            return CheckResult(
                passed=False,
                check_type="pattern",
                file=file,
                name=pattern,
                description=description,
                details=f"Файл {file} не найден"
            )

        try:
            # re.DOTALL позволяет . матчить переносы строк для многострочных паттернов
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if match:
                line_no = content[:match.start()].count('\n') + 1
                matched_text = match.group(0)[:50]
                return CheckResult(
                    passed=True,
                    check_type="pattern",
                    file=file,
                    name=pattern,
                    description=description,
                    details=f"Паттерн найден в строке {line_no}: '{matched_text}...'"
                )
        except re.error as e:
            return CheckResult(
                passed=False,
                check_type="pattern",
                file=file,
                name=pattern,
                description=description,
                details=f"Ошибка regex: {e}"
            )

        return CheckResult(
            passed=False,
            check_type="pattern",
            file=file,
            name=pattern,
            description=description,
            details="Паттерн не найден"
        )

    def run_check(self, check: dict) -> CheckResult:
        """Выполняет проверку на основе конфигурации."""
        check_type = check.get('type', 'pattern')
        file = check.get('file', '')
        name = check.get('name', '')
        pattern = check.get('pattern', name)
        description = check.get('description', '')
        keys = check.get('keys', [])

        if check_type == 'function':
            if pattern and pattern != name:
                return self.check_pattern(file, pattern, description)
            return self.check_function(file, name, description)

        elif check_type == 'handler':
            if pattern and pattern != name:
                return self.check_pattern(file, pattern, description)
            return self.check_handler(file, name, description)

        elif check_type == 'state':
            if pattern and pattern != name:
                return self.check_pattern(file, pattern, description)
            return self.check_state(file, name, description)

        elif check_type == 'constant':
            return self.check_constant(file, name, description)

        elif check_type == 'localization':
            return self.check_localization(file, keys, description)

        elif check_type == 'pattern':
            return self.check_pattern(file, pattern, description)

        elif check_type == 'class':
            return self.check_class(file, name, description)

        elif check_type == 'file':
            return self.check_file(file, description)

        elif check_type == 'transition':
            state = check.get('state', '')
            class_file = check.get('class_file', file)
            events = check.get('events', [])
            return self.check_transition(state, class_file, events, description)

        elif check_type == 'table':
            # Для проверки CREATE TABLE используем pattern
            return self.check_pattern(file, pattern, description)

        else:
            return CheckResult(
                passed=False,
                check_type=check_type,
                file=file,
                name=name or pattern,
                description=description,
                details=f"Неизвестный тип проверки: {check_type}"
            )
