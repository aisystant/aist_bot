"""
Хендлеры нового марафона для новичков (WP-330).

Прямая работа с learning.marathon_queue / marathon_progress / marathon_state.
Не использует legacy aiogram FSM — состояние хранится в БД.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from db.queries.marathon_newcomer import (
    get_or_create_progress,
    update_progress,
    enqueue_day_items,
    save_checkin,
    clear_marathon_queue,
    clear_marathon_state,
    get_sent_checkins_count,
    get_total_checkins_count,
    has_recent_lesson_practice_sent,
    pause_marathon,
    resume_marathon,
)
from db.queries.users import moscow_now, update_intern
from db.queries.activity import record_active_day
from config import get_logger

logger = get_logger(__name__)

marathon_router = Router(name="marathon")

# Если старт до этого часа (МСК) — первый урок отправляется немедленно. DP.SC.157.
MARATHON_SAME_DAY_CUTOFF_HOUR = 18


async def start_marathon_flow(user_id: int, reply_msg, schedule_time: str = "04:00") -> None:
    """Запуск марафона: регистрация, заполнение очереди, первый урок. DP.SC.157."""
    from db.queries import get_intern
    _intern = await get_intern(user_id)
    if (_intern or {}).get("bot_blocked"):
        logger.warning("[Marathon] start_marathon_flow skipped for bot-blocked user %s", user_id)
        return

    progress = await get_or_create_progress(user_id)
    current_status = progress.get("status", "registered")

    if current_status == "active":
        await reply_msg.answer(
            "🎉 Ты уже в марафоне!\n\n"
            f"📅 День марафона: {progress.get('current_day', 0)} / 14\n\n"
            "📋 Команды:\n"
            "• /marathon_progress — статус и прогресс\n"
            "• /marathon_pause — пауза (прогресс сохранится)\n"
            "• /marathon_stop — выйти из марафона\n"
            "• /profile — изменить время и уровень сложности\n"
            "• /support — поддержка"
        )
        return

    if current_status == "paused":
        resumed = await resume_marathon(user_id, schedule_time)
        if resumed:
            await update_intern(user_id, marathon_status="active")
            await reply_msg.answer(
                "▶️ Возвращаю тебя в марафон!\n\n"
                f"📅 Ты на дне {progress.get('current_day', 0)} / 14. "
                f"Оставшиеся уроки придут по одному в день в {schedule_time} МСК, начиная с завтра.\n\n"
                "📋 /marathon_progress — прогресс | /marathon_pause — снова пауза"
            )
            logger.info("[Marathon] user_id=%s resumed from pause", user_id)
        else:
            await reply_msg.answer("Не получилось возобновить. Загляни в /support.")
        return

    if current_status == "completed":
        await reply_msg.answer(
            "✅ Ты уже завершил марафон!\n\n"
            "Если хочешь пройти снова — напиши в поддержку /support."
        )
        return

    now = moscow_now()
    # WP-330 Ф8.2: чистим marathon_state перед стартом — иначе унаследуем
    # checkin-записи прошлых тестов, и первый реальный чек-ин не инкрементирует
    # current_day (existing != None в callback_marathon_checkin). total_checkins
    # derived из marathon_state — отдельно не инкрементируется.
    await clear_marathon_state(user_id)
    await update_progress(
        user_id=user_id,
        status="active",
        started_at=now,
        current_day=1,
    )
    await update_intern(user_id, marathon_status="active", onboarding_completed=True)

    from core.marathon_content import get_day_text
    from db.queries import get_intern

    # WP-330 С9a: подаём intern в get_day_text для routing 4 версий контента
    intern_for_routing = await get_intern(user_id)

    # DP.SC.157: <18:00 МСК → день 1 немедленно; ≥18:00 → завтра 04:00 МСК
    sched_h, sched_m = map(int, schedule_time.split(":"))
    tomorrow_sched = datetime(now.year, now.month, now.day, sched_h, sched_m, 0, tzinfo=now.tzinfo) + timedelta(days=1)
    if now.hour < MARATHON_SAME_DAY_CUTOFF_HOUR:
        day1_time = now + timedelta(minutes=1)
        next_day_base = tomorrow_sched
        first_lesson_today = True
    else:
        day1_time = tomorrow_sched
        next_day_base = tomorrow_sched + timedelta(days=1)
        first_lesson_today = False

    for day in range(1, 15):
        scheduled = day1_time if day == 1 else next_day_base + timedelta(days=day - 2)
        # WP-330 B2: delivery-time rendering — не материализуем lesson_practice при enqueue.
        # Scheduler рендерит текст из свежего intern непосредственно перед отправкой.
        # checkin не routable — материализуем сразу.
        content_texts = {
            'lesson_practice': None,
            'checkin': get_day_text(day, 'checkin'),
        }
        await enqueue_day_items(user_id, day, scheduled, content_texts)

    logger.info("[Marathon] user_id=%s started. Queue 1-14 filled. immediate=%s", user_id, first_lesson_today)

    first_lesson_note = (
        f"Первый урок придёт через минуту — приготовься!\nСо дня 2 уроки приходят ежедневно в {schedule_time} МСК."
        if first_lesson_today
        else f"Первый урок и все последующие приходят ежедневно в {schedule_time} МСК."
    )
    await reply_msg.answer(
        "🚀 Добро пожаловать в марафон «Первые шаги в IWE»!\n\n"
        "14 дней × 20 мин/день, можно ставить паузу.\n\n"
        "Утром — теория и практика (один конкретный шаг), вечером — чек-ин.\n\n"
        f"📅 {first_lesson_note}\n\n"
        "📋 Команды:\n"
        "• /learn — получить урок\n"
        "• /marathon_progress — прогресс\n"
        "• /marathon_pause — пауза (прогресс сохранится)\n"
        "• /marathon_stop — выйти из марафона\n"
        "• /profile — изменить время и уровень сложности\n"
        "• /support — поддержка"
    )


async def reset_newcomer_marathon(user_id: int, schedule_time: str = "04:00") -> None:
    """Reset new-engine marathon for user: wipe queue/state, re-enqueue from day 1.

    Called from marathon_reset_do (mode_selector) and _reset_marathon (mydata)
    when the user is on the new engine (learning.marathon_progress exists).
    Unlike clear_marathon_queue, we DELETE ALL queue rows (including 'sent') so
    enqueue_day_items inserts fresh rows instead of preserving sent status.
    """
    from db.connection import get_learning_pool
    from core.marathon_content import get_day_text

    now = moscow_now()
    sched_h, sched_m = map(int, schedule_time.split(":"))
    tomorrow_sched = datetime(now.year, now.month, now.day, sched_h, sched_m, 0, tzinfo=now.tzinfo) + timedelta(days=1)

    await clear_marathon_state(user_id)

    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM learning.marathon_queue WHERE user_id = $1", user_id)

    await update_progress(user_id=user_id, current_day=1, status="active", started_at=now)

    day1_time = now + timedelta(minutes=1)
    for day in range(1, 15):
        scheduled = day1_time if day == 1 else tomorrow_sched + timedelta(days=day - 2)
        content_texts = {"lesson_practice": None, "checkin": get_day_text(day, "checkin")}
        await enqueue_day_items(user_id, day, scheduled, content_texts)

    logger.info("[Marathon] User %s reset newcomer marathon. Queue 1-14 re-filled.", user_id)


# ════════════════════════════════════════════════════════════════════
# WP-330 cutover /learn (2026-06-05): доставка урока дня в НОВОМ формате.
# Закрывает 4 входа в старую SM (workshop.marathon.lesson): /learn, кнопка
# «Учиться», меню-сервис marathon, кнопки-напоминания. Старый SM-поток
# (states/workshops/marathon/* + core/topics.py) deprecated, удалить после
# 2026-07-05. Прогресс читается из marathon_progress.current_day (сохраняется
# в Neon) — переключение не теряет позицию пользователя.
# ════════════════════════════════════════════════════════════════════


async def _deliver_marathon_lesson(user_id: int, target, day: int, intern: dict = None) -> None:
    """Отдать урок дня в новом формате: статический текст get_day_text + кнопка практики.

    Зеркало доставки scheduler (lesson_practice, core/scheduler.py). target —
    объект с .answer() (Message или callback.message).
    """
    from core.marathon_content import get_day_text
    from db.queries import get_intern

    if intern is None:
        intern = await get_intern(user_id)
    day = max(1, min(14, day or 1))

    # WP-330 С9a: routing по профилю (4 версии); fallback на legacy-ключ.
    lesson = get_day_text(day, 'lesson', intern=intern) or get_day_text(day, 'lesson')
    if not lesson:
        await target.answer("Урок для этого дня недоступен. Загляни в /support.")
        logger.warning(f"[Learn] No lesson content for day {day} (user {user_id})")
        return

    faq = get_day_text(day, 'faq_hint')
    text = lesson + (f"\n\n{faq}" if faq else "")
    if len(text) > 4000:
        text = text[:3990] + "\n\n…"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Перейти к практике", callback_data=f"marathon_practice:{day}")
    ]])
    await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    logger.info(f"[Learn] Delivered new-format lesson day {day} to {user_id}")


async def _migrate_old_marathon_to_new(user_id: int, intern: dict) -> dict:
    """WP-330 cutover fix: авто-миграция из старой системы в новую.

    Вызывается когда marathon_progress.status='registered' но intern.marathon_status='active'.
    Восстанавливает позицию пользователя: заполняет очередь на 14 дней, пропускает
    дни 1..(old_day-1), доставляет old_day через 2 минуты.
    """
    from core.topics import get_marathon_day
    from db.connection import get_learning_pool

    old_day = get_marathon_day(intern)
    old_day = max(1, min(14, old_day))
    sched_time = (intern or {}).get('schedule_time') or '04:00'
    now = moscow_now()

    logger.info(f"[Marathon] Auto-migrating user {user_id} from old system to new, day={old_day}")

    await clear_marathon_state(user_id)
    await clear_marathon_queue(user_id)
    await update_progress(user_id=user_id, status='active', started_at=now, current_day=old_day)
    await update_intern(user_id, marathon_status='active', onboarding_completed=True)

    # Заполнить очередь на 14 дней (дни 1..old_day-1 сразу помечаем sent)
    from core.marathon_content import get_day_text
    sched_h, sched_m = map(int, sched_time.split(':'))
    tomorrow_sched = datetime(now.year, now.month, now.day, sched_h, sched_m, 0,
                              tzinfo=now.tzinfo) + timedelta(days=1)
    for day in range(1, 15):
        if day == old_day:
            scheduled = now + timedelta(minutes=2)
        elif day < old_day:
            scheduled = now - timedelta(hours=1)  # в прошлом — будет помечен sent
        else:
            scheduled = tomorrow_sched + timedelta(days=day - old_day - 1)
        content_texts = {'lesson_practice': None, 'checkin': get_day_text(day, 'checkin')}
        await enqueue_day_items(user_id, day, scheduled, content_texts)

    # Пометить уже пройденные дни как sent
    if old_day > 1:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE learning.marathon_queue
                   SET status='sent', sent_at=NOW(), updated_at=NOW()
                   WHERE user_id=$1 AND day_number < $2 AND status='pending'""",
                user_id, old_day,
            )

    return await get_or_create_progress(user_id)


async def try_deliver_new_marathon(user_id: int, target, intern: dict = None, dedup_minutes: int = 60) -> bool:
    """Если пользователь в марафоне — отдать урок дня (новый формат) и вернуть True.

    Возвращает False только для НЕ-марафонских режимов (Лента) — тогда вызывающий
    идёт прежним путём. Для марафона всегда обрабатывает сам:
      active     → урок текущего дня,
      completed  → сообщение «завершил»,
      иначе      → подсказка /marathon_start (без авто-старта).

    dedup_minutes: окно дедупликации (720 мин для /learn, 10 мин для callback-кнопок).
    Короткое окно позволяет пользователю получить урок по кнопке catch-up уведомления
    даже если урок был недавно доставлен автоматически.
    """
    from db.queries import get_intern

    if intern is None:
        intern = await get_intern(user_id)
    mode = (intern or {}).get('mode') or 'marathon'
    if mode not in ('marathon', 'both'):
        return False  # Лента и пр. — не трогаем марафон-прогресс

    progress = await get_or_create_progress(user_id)
    status = progress.get('status')

    # WP-330 cutover fix (2026-06-06): пользователь был активен в старой системе,
    # но в новой — ещё не стартовал. Авто-мигрировать на правильный день.
    if status == 'registered' and (intern or {}).get('marathon_status') == 'active':
        progress = await _migrate_old_marathon_to_new(user_id, intern)
        status = progress.get('status')

    if status == 'active':
        # UX-audit Day 1 №4+№7: повторный /learn не должен дублировать урок дня.
        # Если lesson_practice уже отправлялся недавно — показываем статус.
        # При вызове из callback (catch-up кнопка) dedup_minutes=10 мин — короткое окно.
        if await has_recent_lesson_practice_sent(user_id, within_minutes=dedup_minutes):
            display_day = progress.get('current_day', 1) if progress.get('current_day', 0) > 0 else 1
            await target.answer(
                f"📚 Урок дня уже отправлен.\n\n"
                f"📅 День марафона: {display_day} / 14\n"
                f"🌙 Чек-ин придёт вечером.\n\n"
                "Если хочешь повторить практику — нажми кнопку «✏️ Перейти к практике» "
                "в уроке или используй /marathon_progress."
            )
            return True
        await _deliver_marathon_lesson(user_id, target, progress.get('current_day', 1), intern)
        return True

    if status == 'completed':
        await target.answer(
            "✅ Ты уже завершил марафон!\n\n"
            "Если хочешь пройти снова — напиши в поддержку /support."
        )
        return True

    # registered / dropped / не стартовал — подсказка, НЕ авто-старт (WP-330 cutover design)
    await target.answer(
        "🚀 Марафон ещё не запущен.\n\n"
        "Начни командой /marathon_start — придёт первый урок."
    )
    return True


# ════════════════════════════════════════════════════════════════════
# Ф2.3 /marathon_start — регистрация + заполнение очереди на 14 дней
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_start"))
async def cmd_marathon_start(message: Message):
    """Старт марафона новичков: регистрация, заполнение очереди, приветствие."""
    from db.queries import get_intern
    intern = await get_intern(message.chat.id)
    sched = (intern or {}).get("schedule_time") or "04:00"
    await start_marathon_flow(message.chat.id, message, schedule_time=sched)


# ════════════════════════════════════════════════════════════════════
# Ф2.5 /marathon_progress — статус и текущий день
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_progress"))
async def cmd_marathon_progress(message: Message):
    """Показать текущий прогресс участника марафона."""
    chat_id = message.chat.id
    progress = await get_or_create_progress(chat_id)

    status = progress.get("status", "registered")
    current_day = progress.get("current_day", 0)
    total_checkins = await get_total_checkins_count(chat_id)
    started_at = progress.get("started_at")

    status_emoji = {
        "registered": "📝",
        "active": "🏃",
        "paused": "⏸",
        "completed": "✅",
        "dropped": "🚫",
    }.get(status, "❓")

    lines = [
        f"{status_emoji} Статус: {status}",
    ]

    if status == "active":
        display_day = current_day if current_day > 0 else 1
        # WP-330 B1: считаем пропущенные от отправленных чек-инов, не от current_day.
        # current_day — это номер последнего задеплоенного дня, а не ожидаемых чек-инов.
        sent_checkins = await get_sent_checkins_count(chat_id)
        missed_checkins = max(0, sent_checkins - total_checkins)
        lines.append(f"📅 День марафона: {display_day} / 14")
        # UX-audit Day 1 №8: пояснить, что чек-ин приходит вечером.
        lines.append(f"🌙 Чек-инов: {total_checkins} (приходит вечером)")
        lines.append(f"❌ Пропущено чек-инов: {missed_checkins}")
        if started_at:
            started_str = started_at.strftime("%d.%m.%Y")
            lines.append(f"🚀 Старт: {started_str}")
    elif status == "registered":
        lines.append("\nНачни марафон командой /marathon_start")
    elif status == "completed":
        lines.append("\nПоздравляем с завершением! 🎉")

    await message.answer("\n".join(lines))


# ════════════════════════════════════════════════════════════════════
# Ф3.1 / Ф3.2 — Обработка чек-ина: кнопки 😵/🧱/🔁
# ════════════════════════════════════════════════════════════════════

@marathon_router.callback_query(F.data.startswith("marathon_checkin:"))
async def callback_marathon_checkin(callback: CallbackQuery):
    """Сохранить состояние чек-ина, продвинуть current_day, подтвердить выбор."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return

    _, state, day_str = parts
    try:
        day = int(day_str)
    except ValueError:
        await callback.answer("Ошибка формата дня", show_alert=True)
        return

    user_id = callback.from_user.id

    # Атомарный UPSERT — возвращает True если это первый чек-ин за день.
    # Исключает TOCTOU при двойном тапе: решение о counting принимается на уровне DB.
    is_first_checkin = await save_checkin(user_id, day, state)
    is_completed = False

    # Инкремент прогресса только при первом чек-ине за день
    if is_first_checkin:
        progress = await get_or_create_progress(user_id)
        current_day = progress.get("current_day", 0)

        new_day = max(current_day, day)
        new_status = None
        if day >= 14:
            new_status = "completed"

        await update_progress(
            user_id=user_id,
            current_day=new_day,
            status=new_status,
        )

        # WP-7 / WP-330: фиксируем учебную активность в activity_log.
        # record_active_day идемпотентен по (chat_id, activity_date, activity_type).
        try:
            await record_active_day(user_id, 'marathon_checkin', mode='marathon')
        except Exception as _e:
            logger.warning("[MarathonCheckin] record_active_day failed for %s: %s", user_id, _e)

        if new_status == "completed":
            await update_intern(user_id, marathon_status="completed")
            is_completed = True

        logger.info(
            f"[MarathonCheckin] User {user_id} day {day} state={state} "
            f"current_day {current_day}→{new_day}"
        )
    else:
        logger.info(f"[MarathonCheckin] User {user_id} updated day {day} state={state} (already checked in)")

    state_labels = {
        "chaos": "😵 Хаос",
        "stuck": "🧱 Тупик",
        "turn": "🔁 Поворот",
    }
    label = state_labels.get(state, state)

    await callback.answer(f"Записано: {label}", show_alert=False)

    # Убираем кнопки и показываем выбор
    original_text = callback.message.text or callback.message.caption or ""
    footer = "" if is_completed else "\n\n📋 /marathon_progress — прогресс | /marathon_pause — пауза | /marathon_stop — выход"
    await callback.message.edit_text(
        f"{original_text}\n\n✅ Твой выбор: {label}{footer}",
        reply_markup=None,
    )

    if is_completed:
        await callback.message.answer(
            "🎉 Поздравляем с завершением марафона «Первые шаги в IWE»!\n\n"
            "14 дней пройдено — ты молодец! Ты освоил базовые инструменты "
            "системного развития.\n\n"
            "📋 /marathon_progress — посмотреть статистику\n"
            "• /support — вопросы и поддержка"
        )


# ════════════════════════════════════════════════════════════════════
# WP-330 Ф10.C — Callback «✏️ Перейти к практике»
# ════════════════════════════════════════════════════════════════════
# NOTE (2026-06-05): callbacks_router (handlers/__init__.py:84) подключён РАНЬШЕ
# marathon_router (:116), а его cb_marathon_actions с фильтром
# F.data.startswith("marathon_") перехватывает этот callback первым. Поэтому
# хендлер достижим через ЯВНЫЙ forward из handlers/callbacks.py
# (elif data.startswith("marathon_practice:")), а НЕ напрямую через этот
# декоратор роутера — та же схема, что у callback_marathon_checkin.

@marathon_router.callback_query(F.data.startswith("marathon_practice:"))
async def callback_marathon_practice(callback: CallbackQuery):
    """Доставить practice по нажатию кнопки. Повторяемо без ограничений (WP-330)."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return
    try:
        day = int(parts[1])
    except ValueError:
        await callback.answer("Ошибка формата дня", show_alert=True)
        return

    user_id = callback.from_user.id

    from db.queries.notifications import try_insert_notification
    # WP-330 (2026-06-05): практику можно получать повторно сколько угодно раз
    # (запрос пилота). Раньше was_notification_sent блокировал вторую доставку
    # сообщением «уже доставлена» — проверку убрали. Запись события оставляем:
    # она гасит напоминание (get_users_for_practice_nudge), но повторную
    # доставку НЕ блокирует.
    idempotency_key = f"marathon_practice:{user_id}:{day}"

    from core.marathon_content import get_day_text, resolve_variant
    from db.queries import get_intern
    # WP-330 С9a: routing по профилю → short_simple/short_complex/long_simple/long_complex
    intern_for_routing = await get_intern(user_id)
    variant = resolve_variant(
        intern_for_routing.get("study_duration") if intern_for_routing else None,
        intern_for_routing.get("complexity_level") if intern_for_routing else None,
    )
    practice = (
        get_day_text(day, 'practice', intern=intern_for_routing)
        or get_day_text(day, 'practice')
    )
    if not practice:
        await callback.answer("Практика для этого дня недоступна", show_alert=True)
        logger.warning(f"[MarathonPractice] No practice content for day {day} (user {user_id})")
        return

    practice_text = practice
    if len(practice_text) > 4000:
        practice_text = practice_text[:3990] + "\n\n…"

    try:
        try:
            await callback.message.answer(practice_text, parse_mode="Markdown")
        except Exception as md_err:
            if "can't parse entities" in str(md_err).lower():
                # Markdown parse error — retry without formatting to deliver content
                logger.warning(
                    f"[MarathonPractice] Markdown error day {day} user {user_id} "
                    f"variant={variant} bytes={len(practice_text.encode())}: {md_err}"
                )
                await callback.message.answer(practice_text)
            else:
                raise
        # UX-audit Day 1 №2: после практики сообщаем, что день завершён.
        # Формулировка без призыва «записаться в марафон» — пользователь уже в нём.
        next_day_msg = (
            f"\n\n📅 День {day + 1} начнётся завтра утром — урок придёт автоматически в запланированное время."
            if day < 14 else
            "\n\n🎉 Это был последний день марафона!"
        )
        await callback.message.answer(
            f"✅ День {day} завершён.\n\n"
            f"🌙 Вечером придёт чек-ин — короткая рефлексия.{next_day_msg}"
        )
        # Фиксируем факт первого получения практики (гасит напоминание-nudge).
        # На повторных кликах ON CONFLICT DO NOTHING → no-op, доставка не блокируется.
        await try_insert_notification(user_id, "marathon_practice", idempotency_key)
        # Кнопку НЕ снимаем: практику можно получать повторно сколько угодно раз.
        await callback.answer()
        logger.info(f"[MarathonPractice] Sent practice day {day} to {user_id} variant={variant}")
    except Exception as e:
        logger.error(
            f"[MarathonPractice] Failed to send practice day {day} to {user_id} "
            f"variant={variant}: {type(e).__name__}: {e}"
        )
        await callback.answer("Не удалось отправить практику. Попробуй ещё раз позже.", show_alert=True)


# ════════════════════════════════════════════════════════════════════
# Ф2.8 /marathon_stop — выход из марафона
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_pause"))
async def cmd_marathon_pause(message: Message):
    """Пауза марафона: статус → paused, очередь и прогресс сохраняются."""
    chat_id = message.chat.id
    progress = await get_or_create_progress(chat_id)

    if progress.get("status") != "active":
        await message.answer(
            "ℹ️ У тебя нет активного марафона, ставить на паузу нечего.\n"
            "Начать или продолжить — /marathon_start"
        )
        return

    paused = await pause_marathon(chat_id)
    if not paused:
        await message.answer("Не получилось поставить на паузу. Загляни в /support.")
        return
    await update_intern(chat_id, marathon_status="paused")

    logger.info(f"[Marathon] User {chat_id} paused marathon at day {progress.get('current_day', 0)}.")
    await message.answer(
        "⏸ Марафон на паузе.\n\n"
        f"Ты на дне {progress.get('current_day', 0)} / 14 — прогресс сохранён.\n"
        "Когда будешь готов продолжить — /marathon_start (с того же места).\n\n"
        "📋 /profile — настройки | /support — поддержка"
    )


@marathon_router.message(Command("marathon_stop"))
async def cmd_marathon_stop(message: Message):
    """Выйти из марафона: очистить очередь, статус → dropped (необратимо)."""
    chat_id = message.chat.id
    progress = await get_or_create_progress(chat_id)

    if progress.get("status") not in ("active", "paused"):
        await message.answer(
            "ℹ️ У тебя нет активного марафона.\n"
            "Начни командой /marathon_start"
        )
        return

    # Очищаем pending-записи из очереди
    await clear_marathon_queue(chat_id)
    # WP-330 Ф8.2: чистим marathon_state — иначе при перезапуске новые чек-ины
    # не инкрементируют current_day (existing != None блокирует update_progress).
    await clear_marathon_state(chat_id)

    # Обновляем статус
    await update_progress(
        user_id=chat_id,
        status="dropped",
    )
    await update_intern(chat_id, marathon_status="not_started")

    logger.info(f"[Marathon] User {chat_id} stopped marathon.")
    await message.answer(
        "🛑 Ты вышел из марафона. Прогресс сброшен.\n\n"
        "Если нужна была временная пауза — в следующий раз /marathon_pause "
        "(сохраняет прогресс).\n"
        "Начать заново с первого дня — /marathon_start.\n\n"
        "📋 /profile — настройки | /support — поддержка"
    )
