"""
Миграция 011: Таблица seminars — каталог семинаров для витрины (WP-5).

Создаёт:
  - seminars: каталог семинаров (бесплатных и платных)
  - seminar_payments: оплаты конкретных семинаров (расширение workshop_payments)

Запуск:
    python -m db.migrations.011_seminars
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создать таблицу seminars и заполнить seed-данными."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        has_table = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'seminars'
        """)
        if has_table > 0:
            print("Migration 011 already applied, skipping.")
            return

        print("Applying migration 011: seminars...")

        # 1. seminars — каталог
        await conn.execute("""
            CREATE TABLE seminars (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                price_rub INTEGER NOT NULL DEFAULT 0,
                price_stars INTEGER NOT NULL DEFAULT 0,
                is_free BOOLEAN NOT NULL DEFAULT FALSE,
                chat_id BIGINT,
                video_url TEXT,
                speaker TEXT,
                tilda_uid TEXT,
                duration TEXT,
                event_date TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        await conn.execute("""
            CREATE INDEX idx_seminars_active
                ON seminars (active, sort_order)
                WHERE active = TRUE;
        """)

        # 2. seminar_payments — оплаты за конкретные семинары
        await conn.execute("""
            CREATE TABLE seminar_payments (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                seminar_id INTEGER NOT NULL REFERENCES seminars(id),
                amount NUMERIC NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                source TEXT NOT NULL DEFAULT 'bot',
                payment_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                paid_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        await conn.execute("""
            CREATE INDEX idx_seminar_payments_tg
                ON seminar_payments (telegram_id, status);
            CREATE UNIQUE INDEX idx_seminar_payments_payment_id
                ON seminar_payments (payment_id)
                WHERE payment_id IS NOT NULL;
        """)

        # 3. Seed: 9 семинаров с витрины events.system-school.ru
        # chat_id из Excel "Витрина семинаров МИМ.xlsx", video_url из "семинары с url.xlsx"
        await conn.execute("""
            INSERT INTO seminars (title, description, price_rub, price_stars, is_free, chat_id, video_url, speaker, duration, event_date, sort_order, tilda_uid) VALUES
            ('Почему проседает собранность: ИИ как дополнительный фактор',
             'Диагностика расфокуса в условиях ИИ и уведомлений',
             0, 0, TRUE, -1003431778640, 'https://t.me/SSlab_s/12',
             'Церен Церенов', '2,5 часа', '2025-11-30', 1, 'SSlab_s'),

            ('Инженерия всего на пороге',
             'Инженерная оптика мышления для проектирования систем',
             0, 0, TRUE, -1002361217496, 'https://t.me/c/2361217496/135',
             'Анатолий Левенчук', '2 дня', '2024-12-07', 2, 'EA-2024.1-T'),

            ('Интеллектуальная рабочая среда IWE для управляемого развития',
             'Как перестать терять идеи, забывать договорённости и тонуть в хаосе задач — с помощью ИИ-агентов, которые планируют, фиксируют и считают за вас. Живая демонстрация системы, в которой работает ведущий',
             5000, 2500, FALSE, -1003674048529, 'https://t.me/c/3674048529/224',
             'Церен Церенов', '3,5 часа', '2026-02-28', 10, 'SE-2026.2-T'),

            ('Архитектура разговора: моделирование как инструмент влияния',
             'Управление сложными диалогами без манипуляций. Применимо к деловым и личным переговорам',
             8000, 4000, FALSE, -1003461150436, 'https://t.me/c/3461150436/89',
             'Пион Медведева', '2 часа', '2025-12-13', 11, 'SE-2025.4-T'),

            ('ИИ-агенты и экзокортекс: как делегировать рутину, не теряя мышление',
             'Взаимодействие с ИИ без потери фокуса и собранности',
             8000, 4000, FALSE, -1003262453102, 'https://t.me/c/3262453102/635',
             'Церен Церенов', '5 часов', '2025-11-15', 12, 'SE-2025.3-T'),

            ('First Principles Framework',
             'Фреймворк для работы с ИИ и сложными системами. Мышление на уровне первопринципов',
             10000, 5000, FALSE, -1003041329783, 'https://t.me/c/3041329783/220',
             'Анатолий Левенчук', '7 часов', '2025-10-04', 13, 'SE-2025.2-T'),

            ('First Principles Framework на пороге 2026',
             'Обновлённые возможности FPF для 2026. Ориентация в текущем состоянии',
             12000, 6000, FALSE, -1003387677562, 'https://t.me/c/3387677562/434',
             'Анатолий Левенчук', '4 часа', '2025-12-07', 14, 'FPF-2025.2'),

            ('Развитие для развитых в 2026',
             'Инженерный процесс разработки в эпоху AI. О проблематизации, управлении портфелем решений и гибридных командах',
             14000, 7000, FALSE, -1003660259339, 'https://t.me/c/3660259339/504',
             'Анатолий Левенчук', '7 часов', '2026-02-01', 15, 'SE-2026.1-T'),

            ('Мантры по организационному развитию',
             'Чеклисты мышления для работы с организацией как системой',
             20000, 10000, FALSE, -1002672942517, 'https://t.me/c/2672942517/46',
             'Анатолий Левенчук', '7 часов', '2025-04-26', 16, 'LS-2025.1-T');
        """)

        print("Migration 011 applied successfully.")
        print("  Table: seminars (9 seed rows), seminar_payments")

    finally:
        await conn.close()


async def rollback():
    """Откат миграции 011."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("Rolling back migration 011...")
        await conn.execute("DROP TABLE IF EXISTS seminar_payments;")
        await conn.execute("DROP TABLE IF EXISTS seminars;")
        print("Rollback 011 done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--rollback":
        asyncio.run(rollback())
    else:
        asyncio.run(migrate())
