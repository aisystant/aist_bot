"""
Root conftest — fix i18n import conflict.

pytest добавляет tests/ в sys.path, из-за чего tests/i18n/ перекрывает
корневой i18n/. Удаляем все пути с tests/ из sys.path.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
_TESTS = str(Path(__file__).resolve().parent / "tests")

# Удаляем tests/ и все подпути tests/ из sys.path
sys.path[:] = [p for p in sys.path if not p.startswith(_TESTS)]

# Вставляем корень первым
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Проверка: i18n должен резолвиться из корня
import importlib
if 'i18n' in sys.modules:
    del sys.modules['i18n']
