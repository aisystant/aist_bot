"""
Инструменты проверки и экспорта переводов

Использование:
    # Проверка полноты переводов
    python -m i18n.checker check
    python -m i18n.checker check --lang es

    # Экспорт для переводчика
    python -m i18n.checker export --lang fr --format csv

    # Импорт перевода
    python -m i18n.checker import --file fr_translations.csv
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional

import yaml

from .loader import I18N_DIR, get_i18n, SUPPORTED_LANGUAGES


def check_translations(lang: Optional[str] = None) -> bool:
    """
    Проверить полноту переводов

    Args:
        lang: конкретный язык или None для всех

    Returns:
        True если все переводы полные
    """
    i18n = get_i18n()
    stats = i18n.get_stats()
    all_ok = True

    print("\n=== Translation Status ===\n")

    languages = [lang] if lang else list(stats.keys())

    for check_lang in languages:
        if check_lang not in stats:
            print(f"  {check_lang}: Language not found!")
            all_ok = False
            continue

        s = stats[check_lang]
        pct = (s['translated'] / s['total'] * 100) if s['total'] > 0 else 0

        if s['missing'] == 0:
            status = "OK"
        else:
            status = f"MISSING {s['missing']}"
            all_ok = False

        print(f"  {check_lang}: {s['translated']}/{s['total']} ({pct:.0f}%) - {status}")

        # Показать недостающие ключи
        if s['missing'] > 0 and lang:
            missing = i18n.get_missing_keys(check_lang)
            print(f"\n  Missing keys for '{check_lang}':")
            for key in sorted(missing)[:20]:
                ru_text = i18n.t(key, 'ru')
                preview = ru_text[:50] + '...' if len(ru_text) > 50 else ru_text
                print(f"    - {key}: \"{preview}\"")
            if len(missing) > 20:
                print(f"    ... and {len(missing) - 20} more")

    print()
    return all_ok


def check_placeholders() -> bool:
    """Проверить консистентность плейсхолдеров"""
    i18n = get_i18n()
    all_ok = True

    print("\n=== Placeholder Check ===\n")

    ru_trans = i18n.translations.get('ru', {})

    for lang, trans in i18n.translations.items():
        if lang == 'ru':
            continue

        errors = []
        for key, text in trans.items():
            ru_text = ru_trans.get(key, '')

            # Извлечь плейсхолдеры
            ru_placeholders = set(re.findall(r'\{(\w+)\}', ru_text))
            lang_placeholders = set(re.findall(r'\{(\w+)\}', text))

            if ru_placeholders != lang_placeholders:
                errors.append({
                    'key': key,
                    'expected': ru_placeholders,
                    'found': lang_placeholders
                })

        if errors:
            all_ok = False
            print(f"  {lang}: {len(errors)} placeholder errors")
            for err in errors[:5]:
                print(f"    - {err['key']}: expected {err['expected']}, found {err['found']}")
            if len(errors) > 5:
                print(f"    ... and {len(errors) - 5} more")
        else:
            print(f"  {lang}: OK")

    print()
    return all_ok


def export_for_translator(
    lang: str,
    output_format: str = 'csv',
    output_file: Optional[str] = None
) -> None:
    """
    Экспортировать ключи для переводчика

    Args:
        lang: целевой язык
        output_format: формат вывода ('csv', 'json', 'yaml')
        output_file: путь к выходному файлу
    """
    i18n = get_i18n()

    # Собрать данные
    rows = []
    ru_trans = i18n.translations.get('ru', {})
    en_trans = i18n.translations.get('en', {})
    lang_trans = i18n.translations.get(lang, {})

    for key in sorted(ru_trans.keys()):
        ru_text = ru_trans.get(key, '')
        en_text = en_trans.get(key, '')
        lang_text = lang_trans.get(key, '')

        # Извлечь плейсхолдеры
        placeholders = re.findall(r'\{(\w+)\}', ru_text)

        rows.append({
            'key': key,
            'ru': ru_text,
            'en': en_text,
            lang: lang_text,
            'placeholders': ', '.join(placeholders) if placeholders else ''
        })

    # Статистика
    translated = sum(1 for r in rows if r[lang])
    total = len(rows)

    # Определить выходной файл
    if not output_file:
        output_file = f"{lang}_translations.{output_format}"

    # Экспорт
    if output_format == 'csv':
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['key', 'ru', 'en', lang, 'placeholders'])
            writer.writeheader()
            writer.writerows(rows)

    elif output_format == 'json':
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    elif output_format == 'yaml':
        # Для YAML создаём вложенную структуру
        nested = {}
        for row in rows:
            parts = row['key'].split('.')
            current = nested
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = row[lang] or f"# TODO: {row['ru']}"

        with open(output_file, 'w', encoding='utf-8') as f:
            yaml.dump(nested, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\nExported to {output_file}")
    print(f"Status: {translated}/{total} translated ({translated/total*100:.0f}%)")
    print(f"Missing: {total - translated} keys\n")


def import_translations(input_file: str, lang: str) -> None:
    """
    Импортировать переводы из файла

    Args:
        input_file: путь к файлу с переводами
        lang: целевой язык
    """
    path = Path(input_file)

    if not path.exists():
        print(f"Error: File not found: {input_file}")
        sys.exit(1)

    # Определить формат по расширению
    ext = path.suffix.lower()

    translations = {}

    if ext == '.csv':
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row.get('key', '')
                text = row.get(lang, '')
                if key and text:
                    translations[key] = text

    elif ext == '.json':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for row in data:
                key = row.get('key', '')
                text = row.get(lang, '')
                if key and text:
                    translations[key] = text

    elif ext in ('.yaml', '.yml'):
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        def flatten(d, prefix=''):
            for k, v in d.items():
                full_key = f"{prefix}{k}" if prefix else k
                if isinstance(v, dict):
                    flatten(v, f"{full_key}.")
                elif isinstance(v, str) and not v.startswith('# TODO'):
                    translations[full_key] = v

        flatten(data)

    else:
        print(f"Error: Unsupported format: {ext}")
        sys.exit(1)

    # Создать вложенную структуру для YAML
    nested = {}
    for key, text in sorted(translations.items()):
        parts = key.split('.')
        current = nested
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = text

    # Сохранить
    output_path = I18N_DIR / 'translations' / f'{lang}.yaml'
    output_path.parent.mkdir(exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(nested, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\nImported {len(translations)} translations to {output_path}\n")


def generate_template(lang: str) -> None:
    """Сгенерировать шаблон для нового языка"""
    export_for_translator(lang, 'yaml', str(I18N_DIR / 'translations' / f'{lang}.yaml'))


def main():
    parser = argparse.ArgumentParser(description='i18n translation tools')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # check
    check_parser = subparsers.add_parser('check', help='Check translation completeness')
    check_parser.add_argument('--lang', help='Specific language to check')

    # export
    export_parser = subparsers.add_parser('export', help='Export for translator')
    export_parser.add_argument('--lang', required=True, help='Target language')
    export_parser.add_argument('--format', choices=['csv', 'json', 'yaml'], default='csv')
    export_parser.add_argument('--output', help='Output file path')

    # import
    import_parser = subparsers.add_parser('import', help='Import translations')
    import_parser.add_argument('--file', required=True, help='Input file path')
    import_parser.add_argument('--lang', required=True, help='Target language')

    # generate
    gen_parser = subparsers.add_parser('generate', help='Generate template for new language')
    gen_parser.add_argument('--lang', required=True, help='Target language')

    args = parser.parse_args()

    if args.command == 'check':
        ok = check_translations(args.lang)
        check_placeholders()
        sys.exit(0 if ok else 1)

    elif args.command == 'export':
        export_for_translator(args.lang, args.format, args.output)

    elif args.command == 'import':
        import_translations(args.file, args.lang)

    elif args.command == 'generate':
        generate_template(args.lang)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
