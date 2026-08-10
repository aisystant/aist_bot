#!/usr/bin/env bash
# sync-marathon-content.sh — синхронизация контента марафона из авторского репо в рантайм бота.
#
# Source-of-truth: DS-marathon-v2-tseren/materials/participants/marathon-content.json (авторский).
# Runtime: aist_bot_newarchitecture/data/marathon-content.json (читается ботом при импорте,
#          marathon_content.py путь №1). На Railway деплоится ТОЛЬКО репо бота — поэтому
#          копия обязана жить здесь, fallback-путь на DS-marathon-v2 на проде не существует.
#
# Использование:
#   bash scripts/sync-marathon-content.sh           # синхронизировать (copy + validate)
#   bash scripts/sync-marathon-content.sh --check    # только проверить расхождение (exit 1 если есть)
#
# После sync: git add data/marathon-content.json && commit + push (бот перечитает при рестарте).

set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$BOT_DIR/data/marathon-content.json"

# Авторский файл ищем относительно бота (сиблинг в IWE root) или по env SRC override.
SRC="${MARATHON_CONTENT_SRC:-$BOT_DIR/../DS-marathon-v2-tseren/materials/participants/marathon-content.json}"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

if [[ ! -f "$SRC" ]]; then
  echo "❌ Авторский файл не найден: $SRC"
  echo "   Задай путь через MARATHON_CONTENT_SRC=/path/to/marathon-content.json"
  exit 2
fi

# Валидация: исходник — корректный JSON со словарём days.
if ! python3 - "$SRC" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
days = data.get("days", {})
assert isinstance(days, dict) and len(days) >= 1, "нет дней в контенте"
print(f"✓ Исходник валиден: {len(days)} дней")
PY
then
  echo "❌ Авторский файл не прошёл JSON-валидацию: $SRC"
  exit 3
fi

if cmp -s "$SRC" "$DEST"; then
  echo "✓ Уже синхронизировано — расхождений нет."
  exit 0
else
  CMP_CODE=$?
  if [[ $CMP_CODE -gt 1 ]]; then
    echo "❌ Не удалось сравнить авторский файл и bot-копию (cmp code $CMP_CODE)."
    exit 3
  fi
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "⚠️  Расхождение: авторский файл отличается от бот-копии."
  echo "   Запусти без --check, чтобы синхронизировать."
  exit 1
fi

cp "$SRC" "$DEST"
echo "✓ Скопировано: $SRC → $DEST"
echo "  Дальше: git add data/marathon-content.json && commit + push (бот подхватит при рестарте)."
