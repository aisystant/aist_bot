#!/bin/bash
# sync-profiler.sh — вендоринг R28 Профилировщика в бота
#
# Копирует canonical dt_calc.py из DS-ai-systems/profiler/ → db/queries/
# Запускать вручную после изменений в profiler/scripts/dt_calc.py
#
# Canonical source: DS-IT-systems/DS-ai-systems/profiler/scripts/dt_calc.py
# WP-197: R28 Профилировщик — архитектурное решение

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROFILER_SRC="$(cd "$BOT_ROOT/../../DS-ai-systems/profiler/scripts/dt_calc.py" && pwd)" 2>/dev/null || \
    PROFILER_SRC="$BOT_ROOT/../../DS-ai-systems/profiler/scripts/dt_calc.py"
DEST="$BOT_ROOT/db/queries/dt_calc.py"

if [ ! -f "$PROFILER_SRC" ]; then
    echo "ERROR: Canonical source not found: $PROFILER_SRC"
    echo "Expected: DS-IT-systems/DS-ai-systems/profiler/scripts/dt_calc.py"
    exit 1
fi

cp "$PROFILER_SRC" "$DEST"
echo "✓ Synced profiler/scripts/dt_calc.py → db/queries/dt_calc.py"
echo "  Source: $PROFILER_SRC"
echo "  Dest:   $DEST"
