#!/bin/bash
# Скрипт для запуска Telegram E2E тестов с автосохранением результатов
# Ошибка pytest должна сохранять ненулевой код, даже когда вывод пишется в файл.
set -o pipefail
#
# Использование:
#   ./tests/test-telegram/run_tests.sh              # все тесты
#   ./tests/test-telegram/run_tests.sh critical     # только критические
#   ./tests/test-telegram/run_tests.sh marathon     # только Марафон
#   ./tests/test-telegram/run_tests.sh feed         # только Лента
#   ./tests/test-telegram/run_tests.sh onboarding   # только онбординг

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results"

# Создаём папку для результатов если нет
mkdir -p "$RESULTS_DIR"

# Формируем имя файла: YYYY-MM-DD-HH-MM-test-telegram.txt
DATE=$(date +"%Y-%m-%d-%H-%M")
RESULT_FILE="$RESULTS_DIR/${DATE}-test-telegram.txt"

# Определяем какие тесты запускать
MARKER=""
TEST_PATH="$SCRIPT_DIR"

case "$1" in
    critical)
        MARKER="-m critical"
        echo "Запуск критических тестов..."
        ;;
    marathon)
        TEST_PATH="$SCRIPT_DIR/test_02_marathon.py"
        echo "Запуск тестов Марафона..."
        ;;
    feed)
        TEST_PATH="$SCRIPT_DIR/test_03_feed.py"
        echo "Запуск тестов Ленты..."
        ;;
    onboarding)
        TEST_PATH="$SCRIPT_DIR/test_01_onboarding.py"
        echo "Запуск тестов онбординга..."
        ;;
    *)
        echo "Запуск всех тестов..."
        ;;
esac

echo "Результаты будут сохранены в: $RESULT_FILE"
echo ""

echo "Устанавливаю зависимости Telegram E2E..."
if ! python3 -m pip install --disable-pip-version-check --quiet telethon nest_asyncio; then
    echo "Не удалось установить зависимости Telegram E2E."
    exit 1
fi

# Запускаем тесты и сохраняем результаты
python3 -m pytest "$TEST_PATH" $MARKER -v 2>&1 | tee "$RESULT_FILE"

echo ""
echo "========================================"
echo "Результаты сохранены: $RESULT_FILE"
echo "========================================"
