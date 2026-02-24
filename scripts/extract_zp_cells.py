"""
Скрипт извлечения данных ZP-ячеек из markdown-файлов DS-principles-curriculum.
Результат: data/curriculum/zp_cells.json
"""

import json
import re
import yaml
from pathlib import Path

CELLS_DIR = Path.home() / "Github" / "DS-principles-curriculum" / "cells"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "curriculum"

ZP_FILES = {
    "ZP.1": "ZP.1-axiomaticity.md",
    "ZP.2": "ZP.2-structure.md",
    "ZP.3": "ZP.3-multiscale.md",
    "ZP.4": "ZP.4-optimization.md",
    "ZP.5": "ZP.5-probability.md",
    "ZP.6": "ZP.6-limits.md",
}

ZP_NAMES = {
    "ZP.1": "Аксиоматичность",
    "ZP.2": "Структура",
    "ZP.3": "Многомасштабность",
    "ZP.4": "Оптимизация",
    "ZP.5": "Вероятность",
    "ZP.6": "Ограничения",
}


def parse_common_error(error_str: str) -> dict:
    """Parse 'Error text: explanation' into {error, why}."""
    # Try splitting on ': ' but only at the first colon that separates error from explanation
    parts = error_str.split(": ", 1)
    if len(parts) == 2:
        return {"error": parts[0].strip(), "why": parts[1].strip()}
    return {"error": error_str.strip(), "why": ""}


def fix_yaml_quotes(block: str) -> str:
    """Fix nested double quotes in YAML strings that break the parser.

    Problem: YAML like  transfer_test: "Через неделю: «Что значит "вероятность"?»"
    has unescaped " inside "..." which breaks yaml.safe_load.

    Fix: replace inner ASCII double quotes with curly quotes «» or single quotes.
    """
    lines = block.split("\n")
    fixed = []
    for line in lines:
        # Match lines with double-quoted YAML values
        m = re.match(r'^(\s+\w[\w\s]*:\s*)"(.+)"(\s*#.*)?$', line)
        if m:
            prefix = m.group(1)
            value = m.group(2)
            comment = m.group(3) or ""
            # Replace inner " with ' (inner = not at boundaries)
            value = value.replace('"', "'")
            line = f'{prefix}"{value}"{comment}'
        else:
            # Handle continuation of multi-word list items: - "text "inner" text"
            m2 = re.match(r'^(\s+-\s*)"(.+)"(\s*#.*)?$', line)
            if m2:
                prefix = m2.group(1)
                value = m2.group(2)
                comment = m2.group(3) or ""
                value = value.replace('"', "'")
                line = f'{prefix}"{value}"{comment}'
        fixed.append(line)
    return "\n".join(fixed)


def extract_cells_from_file(filepath: Path) -> dict:
    """Extract all depth cells from a single ZP markdown file."""
    content = filepath.read_text(encoding="utf-8")

    # Split by yaml code blocks
    yaml_blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)

    depths = {}
    for block in yaml_blocks:
        # Fix nested quotes before parsing
        block = fix_yaml_quotes(block)
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as e:
            print(f"  YAML parse error in {filepath.name}: {e}")
            continue

        if not data or "cell" not in data:
            continue

        cell = data["cell"]
        depth = cell.get("depth")
        if depth is None:
            continue

        # Extract assessment fields
        assessment = cell.get("assessment", {})

        # Extract forms
        forms_raw = cell.get("forms", {})
        forms = {
            "preoperational": forms_raw.get("preoperational", "Не применимо"),
            "concrete_operational": forms_raw.get("concrete_operational", "Не применимо"),
            "formal_operational": forms_raw.get("formal_operational", "Не применимо"),
            "postformal": forms_raw.get("postformal", "Не применимо"),
        }

        # Parse common errors
        raw_errors = cell.get("common_errors", [])
        common_errors = [parse_common_error(str(e)) for e in raw_errors]

        # Extract domains (may be list or string)
        domains = cell.get("domains", [])
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.split(",")]

        depths[str(depth)] = {
            "bloom_level": cell.get("bloom_level", ""),
            "can_do": cell.get("can_do", []),
            "transfer_test": assessment.get("transfer_test", ""),
            "retrieval_test": assessment.get("retrieval_test", ""),
            "criteria": assessment.get("criteria", ""),
            "common_errors": common_errors,
            "forms": forms,
            "bridge_template": cell.get("bridge_template"),
            "domains": [str(d) for d in domains],
        }

    return depths


def main():
    result = {}

    for principle_id, filename in ZP_FILES.items():
        filepath = CELLS_DIR / filename
        if not filepath.exists():
            print(f"WARNING: {filepath} not found, skipping")
            continue

        print(f"Parsing {filename}...")
        depths = extract_cells_from_file(filepath)
        print(f"  Found {len(depths)} depths")

        result[principle_id] = {
            "name": ZP_NAMES[principle_id],
            "depths": depths,
        }

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "zp_cells.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Stats
    total_cells = sum(len(p["depths"]) for p in result.values())
    print(f"\nDone: {len(result)} principles, {total_cells} cells")
    print(f"Output: {output_path}")
    print(f"Size: {output_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
