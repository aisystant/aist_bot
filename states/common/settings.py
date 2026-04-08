"""
Стейт: Настройки системы (/settings).

Конфигурация системных параметров бота:
- Язык интерфейса
- Расписание напоминаний
- Подключения (GitHub, Цифровой двойник)

Принцип: настройки = КАК система работает (конфигурация).
Персональные данные (ЧТО бот знает обо мне) → Profile.

Вход: по кнопке "Настройки" или команде /settings
Выход: saved → mode_select, cancel → _previous
"""

import logging
import re
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice

from states.base import BaseState
from i18n import t, get_language_name, SUPPORTED_LANGUAGES
from db.queries.users import get_intern, update_intern

logger = logging.getLogger(__name__)


class SettingsState(BaseState):
    """
    Стейт настроек системы.

    Показывает системные настройки: язык, расписание, подключения.
    """

    name = "common.settings"
    display_name = {
        "ru": "Настройки",
        "en": "Settings",
        "es": "Ajustes",
        "fr": "Paramètres"
    }
    allow_global = []

    WAITING_FIELDS = {'schedule'}

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def _set_waiting(self, user, field: str | None) -> None:
        """Сохраняем waiting_for в current_context (персистентно в БД)."""
        chat_id = self._get_chat_id(user)
        ctx = user.get('current_context', {}) if isinstance(user, dict) else {}
        ctx['settings_waiting_for'] = field
        await update_intern(chat_id, current_context=ctx)
        if isinstance(user, dict):
            user['current_context'] = ctx

    def _get_waiting(self, user) -> str | None:
        """Читаем waiting_for из current_context."""
        if isinstance(user, dict):
            return user.get('current_context', {}).get('settings_waiting_for')
        return None

    async def exit(self, user) -> dict:
        """Очищаем settings_waiting_for при выходе из стейта."""
        await self._set_waiting(user, None)
        return {}

    async def enter(self, user, context: dict = None) -> None:
        """Показываем системные настройки — обзорный экран."""
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)
        if not intern:
            await self.send(user, t('profile.not_found', self._get_lang(user)))
            return

        lang = intern.get('language', 'ru') or 'ru'

        # --- Подключения: сводка (WP-209: Gateway вместо Aisystant+DT+GitHub) ---
        from clients.gateway_mcp import gateway_mcp
        gateway_status = "✅" if gateway_mcp.is_connected(chat_id) else "☐"

        from db.queries.wakatime import get_wakatime_connection
        waka_conn = await get_wakatime_connection(chat_id)
        waka_status = "✅" if waka_conn else "☐"

        from clients.google_calendar_oauth import google_calendar_oauth
        gcal_connected = await google_calendar_oauth.is_connected(chat_id)
        gcal_status = "✅" if gcal_connected else "☐"

        # --- Донаты: сводка ---
        from db.queries.subscription import get_active_subscription
        sub = await get_active_subscription(chat_id)
        if sub:
            amount = sub.get('stars_amount', 0)
            donation_line = t('settings.active_donation', lang, amount=amount)
        else:
            donation_line = t('settings.no_active_donations', lang)

        # --- Собираем текст ---
        connections_summary = (
            f"  {gateway_status} Gateway (IWE)\n"
            f"  {waka_status} WakaTime\n"
            f"  {gcal_status} Календарь Google"
        )

        text = (
            f"⚙️ *{t('settings.title', lang)}*\n\n"
            f"🌐 Language: {get_language_name(lang)}\n\n"
            f"🔗 {t('settings.connections_label', lang)}:\n{connections_summary}\n\n"
            f"💝 {t('donation.settings_label', lang)}: {donation_line}"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 " + t('buttons.profile', lang), callback_data="go_profile"),
                InlineKeyboardButton(text="🌐 Language", callback_data="upd_language"),
            ],
            [
                InlineKeyboardButton(text="🔗 " + t('settings.connections_label', lang), callback_data="upd_connections"),
                InlineKeyboardButton(text="💝 " + t('donation.settings_label', lang), callback_data="upd_subscription"),
            ],
            [
                InlineKeyboardButton(text="📂 " + t('settings.mydata_link', lang), callback_data="settings_go_mydata"),
            ],
            [
                InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back")
            ]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

        await self._set_waiting(user, None)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем ввод пользователя."""
        waiting_for = self._get_waiting(user)

        text = (message.text or "").strip()

        if waiting_for:
            return await self._handle_text_input(user, waiting_for, text, message)

        await self.enter(user)
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обрабатываем нажатия inline-кнопок."""
        data = callback.data

        # lang_ обрабатывает answer() сам (с текстом подтверждения)
        if data.startswith("lang_"):
            return await self._save_language(user, callback, data)

        await callback.answer()

        if data == "settings_back":
            return "cancel"

        if data == "go_profile":
            try:
                await callback.message.delete()
            except Exception:
                pass
            return "profile"

        if data == "upd_language":
            return await self._show_language_options(user, callback)

        if data == "upd_schedule":
            return await self._show_schedule_options(user, callback)
        if data == "upd_schedule_marathon":
            return await self._ask_for_field(user, callback, 'schedule_marathon')
        if data == "upd_schedule_feed":
            return await self._ask_for_field(user, callback, 'schedule_feed')

        if data == "conn_gateway":
            return await self._handle_gateway_connection(user, callback)

        if data == "conn_gateway_reconnect":
            return await self._gateway_reconnect(user, callback)

        if data == "conn_aisystant":
            return await self._show_aisystant_connection(user, callback)

        if data == "conn_aisystant_check":
            return await self._aisystant_check(user, callback)

        if data == "upd_connections":
            return await self._show_connections(user, callback)

        if data == "conn_github":
            return await self._handle_github_connection(user, callback)

        if data == "conn_twin":
            return await self._handle_twin_connection(user, callback)

        if data == "conn_twin_disconnect":
            return await self._twin_disconnect(user, callback)

        if data == "conn_club":
            return await self._handle_club_connection(user, callback)

        if data == "conn_club_disconnect":
            return await self._club_disconnect(user, callback)

        if data == "conn_waka":
            return await self._handle_waka_connection(user, callback)

        if data == "conn_waka_disconnect":
            return await self._waka_disconnect(user, callback)

        if data == "conn_gcal":
            return await self._handle_gcal_connection(user, callback)

        if data == "conn_gcal_disconnect":
            return await self._gcal_disconnect(user, callback)

        if data == "conn_iwe_toggle":
            return await self._show_iwe_details(user, callback)

        if data == "conn_iwe_do_toggle":
            return await self._toggle_iwe_updates(user, callback)

        if data == "conn_nudges_toggle":
            return await self._show_nudges_details(user, callback)

        if data == "conn_nudges_do_toggle":
            return await self._toggle_nudges(user, callback)

        if data == "github_select_repo":
            return await self._github_select_repo(user, callback)

        if data.startswith("github_repo:"):
            return await self._github_repo_selected(user, callback, data)

        if data == "github_select_knowledge_repo":
            return await self._github_select_knowledge_repo(user, callback)

        if data.startswith("github_knowledge_repo:"):
            return await self._github_knowledge_repo_selected(user, callback, data)

        if data == "github_disconnect":
            return await self._github_disconnect(user, callback)

        if data == "upd_subscription":
            return await self._show_donation(user, callback)
        if data == "donate_once":
            return await self._show_donate_amounts(user, callback)
        if data == "donate_once_custom":
            return await self._ask_donate_amount(user, callback)
        if data.startswith("donate_pay:"):
            return await self._create_once_donation(user, callback, data)
        if data == "sub_cancel_confirm":
            return await self._subscription_cancel_confirm(user, callback)
        if data == "sub_cancel_do":
            return await self._subscription_cancel_do(user, callback)

        if data == "settings_go_mydata":
            from handlers import get_dispatcher
            dispatcher = get_dispatcher()
            if dispatcher and dispatcher.is_sm_active:
                await dispatcher.route_command('mydata', user)
            return None

        if data == "settings_back_to_menu":
            try:
                await callback.message.delete()
            except Exception:
                pass
            await self.enter(user)
            return None

        return None

    async def _show_schedule_options(self, user, callback: CallbackQuery) -> Optional[str]:
        """Подменю расписания: Марафон / Лента."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)

        marathon_time = intern.get('schedule_time', '09:00')
        feed_time = intern.get('feed_schedule_time') or marathon_time

        text = (
            f"⏰ *{t('settings.schedule_label', lang)}*\n\n"
            f"📚 {t('settings.schedule_marathon', lang)}: *{marathon_time}*\n"
            f"📖 {t('settings.schedule_feed', lang)}: *{feed_time}*"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"📚 {t('settings.schedule_marathon', lang)}", callback_data="upd_schedule_marathon"),
                InlineKeyboardButton(text=f"📖 {t('settings.schedule_feed', lang)}", callback_data="upd_schedule_feed"),
            ],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")],
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _ask_for_field(self, user, callback: CallbackQuery, field: str) -> Optional[str]:
        """Запрашиваем ввод текстового поля."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        intern = await get_intern(chat_id)

        prompts = {
            'schedule_marathon': ('settings.schedule_marathon', 'update.when_remind', intern.get('schedule_time', '09:00')),
            'schedule_feed': ('settings.schedule_feed', 'update.when_remind', intern.get('feed_schedule_time') or intern.get('schedule_time', '09:00')),
        }

        label_key, prompt_key, current_value = prompts.get(field, ('', '', ''))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        await callback.message.edit_text(
            f"⏰ *{t(label_key, lang)}:* {current_value}\n\n"
            f"{t(prompt_key, lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        await self._set_waiting(user, field)

        return None

    async def _handle_text_input(self, user, field: str, text: str, message: Message = None) -> Optional[str]:
        """Сохраняем текстовый ввод."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        time_pattern = r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$'

        if field == 'donate_amount':
            return await self._handle_donate_amount_input(user, text, message)

        if field == 'schedule_marathon':
            if re.match(time_pattern, text):
                h, m = text.split(':')
                normalized = f"{int(h):02d}:{int(m):02d}"
                await update_intern(chat_id, schedule_time=normalized)
                await self.send(user, f"✅ {t('settings.schedule_marathon', lang)}: *{normalized}*", parse_mode="Markdown")
            else:
                await self.send(user, t('modes.invalid_time_format', lang))
                return None

        elif field == 'schedule_feed':
            if re.match(time_pattern, text):
                h, m = text.split(':')
                normalized = f"{int(h):02d}:{int(m):02d}"
                await update_intern(chat_id, feed_schedule_time=normalized)
                await self.send(user, f"✅ {t('settings.schedule_feed', lang)}: *{normalized}*", parse_mode="Markdown")
            else:
                await self.send(user, t('modes.invalid_time_format', lang))
                return None

        elif field == 'wakatime_key':
            # Валидируем API key через WakaTime API
            key = text.strip()
            if not key.startswith('waka_') and len(key) < 20:
                await self.send(user, t('settings.waka_invalid_key', lang))
                return None

            from clients.wakatime import wakatime_client
            waka_user = await wakatime_client.validate_key(key)
            if not waka_user:
                await self.send(user, t('settings.waka_invalid_key', lang))
                return None

            from db.queries.wakatime import save_wakatime_connection
            await save_wakatime_connection(chat_id, key, waka_user.get('username'))

            username = waka_user.get('username') or waka_user.get('display_name') or ''
            await self.send(user, f"✅ WakaTime {t('settings.connected', lang)}: *{username}*\n/waka — {t('settings.waka_check_stats', lang)}", parse_mode="Markdown")

        # WP-151 Ф3: settings_changed (SM settings path)
        from db.queries.events import log_event
        await log_event(chat_id, 'settings_changed', {'field': field})

        await self._set_waiting(user, None)

        await self.enter(user)
        return None

    async def _show_language_options(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем варианты языка."""
        lang = self._get_lang(user)

        buttons = [
            [InlineKeyboardButton(text=get_language_name(l), callback_data=f"lang_{l}")]
            for l in SUPPORTED_LANGUAGES
        ]
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(
            t('settings.language.title', lang),
            reply_markup=keyboard
        )
        return None

    async def _save_language(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохраняем выбранный язык."""
        chat_id = self._get_chat_id(user)

        new_lang = data.replace("lang_", "")
        if new_lang not in SUPPORTED_LANGUAGES:
            new_lang = 'ru'

        old_lang = self._get_lang(user)
        await update_intern(chat_id, language=new_lang)
        # WP-151 Ф3: settings_changed (SM settings path)
        from db.queries.events import log_event
        await log_event(chat_id, 'settings_changed', {
            'field': 'language', 'old_value': old_lang, 'new_value': new_lang,
        })
        # Инвалидация пре-генерированного контента (мог быть на старом языке)
        from db.queries.marathon import invalidate_user_content
        await invalidate_user_content(chat_id)

        if isinstance(user, dict):
            user['language'] = new_lang
        else:
            user.language = new_lang

        # Toast с подтверждением
        await callback.answer(t('settings.language.changed', new_lang))

        # Редактируем текущее сообщение обратно в меню настроек (без нового сообщения)
        # Re-enter settings to rebuild with subscription status
        await self.enter(user)

        # Подсказка: /start обновляет меню на новом языке (после меню настроек)
        await self.send(user, t('settings.language.restart_hint', new_lang))
        return None

    async def _show_donation(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем экран донатов с объяснением и двумя вариантами."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from core.pricing import get_current_price
        from db.queries.subscription import get_active_subscription

        price = get_current_price()
        sub = await get_active_subscription(chat_id)

        text = (
            f"⭐ *{t('donation.title', lang)}*\n\n"
            f"{t('donation.explanation', lang)}"
        )

        buttons = []

        # Активный ежемесячный донат — показываем статус + отмену
        if sub:
            expires = sub.get('expires_at')
            date_str = expires.strftime('%d.%m.%Y') if expires else '—'
            amount = sub.get('stars_amount', price)
            text += f"\n\n{t('donation.recurring_active', lang, price=amount, date=date_str)}"
            buttons.append([InlineKeyboardButton(
                text=t('donation.cancel_recurring_button', lang),
                callback_data="sub_cancel_confirm",
            )])

        buttons.append([InlineKeyboardButton(
            text=t('donation.once_button', lang),
            callback_data="donate_once",
        )])
        buttons.append([InlineKeyboardButton(
            text=t('donation.recurring_button', lang, price=price),
            callback_data="donate_recurring",
        )])
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _show_donate_amounts(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем выбор суммы для разового доната."""
        lang = self._get_lang(user)

        from core.pricing import get_current_price
        price = get_current_price()

        text = t('donation.choose_amount', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {price} Stars", callback_data=f"donate_pay:{price}")],
            [InlineKeyboardButton(text=t('donation.custom_amount_button', lang), callback_data="donate_once_custom")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_subscription")],
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _ask_donate_amount(self, user, callback: CallbackQuery) -> Optional[str]:
        """Запрашиваем произвольную сумму доната."""
        lang = self._get_lang(user)

        await callback.message.edit_text(
            t('donation.enter_amount', lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="donate_once")]
            ]),
            parse_mode="Markdown",
        )

        await self._set_waiting(user, 'donate_amount')
        return None

    async def _create_once_donation(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Создаём invoice для разового доната (из callback donate_pay:N)."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        try:
            amount = int(data.split(":")[1])
            if amount < 1 or amount > 10000:
                raise ValueError
        except (ValueError, IndexError):
            await callback.message.answer(t('errors.try_again', lang))
            return None

        try:
            link = await callback.bot.create_invoice_link(
                title=t('donation.once_invoice_title', lang),
                description=t('donation.once_invoice_description', lang),
                payload=f"donate_once_{chat_id}_{amount}",
                currency="XTR",
                prices=[LabeledPrice(label="Donation", amount=amount)],
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('donation.once_pay_button', lang, price=amount),
                    url=link,
                )]
            ])

            await callback.message.answer(
                t('donation.once_invoice_text', lang, price=amount),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"[Donation] Error creating invoice: {e}")
            await callback.message.answer(t('errors.try_again', lang))

        return None

    async def _handle_donate_amount_input(self, user, text: str, message: Message) -> Optional[str]:
        """Обрабатываем введённую сумму доната."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        try:
            amount = int(text)
            if amount < 1 or amount > 10000:
                raise ValueError
        except ValueError:
            await self.send(user, t('donation.invalid_amount', lang))
            return None

        await self._set_waiting(user, None)

        try:
            link = await message.bot.create_invoice_link(
                title=t('donation.once_invoice_title', lang),
                description=t('donation.once_invoice_description', lang),
                payload=f"donate_once_{chat_id}_{amount}",
                currency="XTR",
                prices=[LabeledPrice(label="Donation", amount=amount)],
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('donation.once_pay_button', lang, price=amount),
                    url=link,
                )]
            ])

            await message.answer(
                t('donation.once_invoice_text', lang, price=amount),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"[Donation] Error creating invoice from text input: {e}")
            await message.answer(t('errors.try_again', lang))

        return None

    async def _subscription_cancel_confirm(self, user, callback: CallbackQuery) -> Optional[str]:
        """Подтверждение отмены ежемесячного доната."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.subscription import get_active_subscription
        sub = await get_active_subscription(chat_id)
        expires = sub.get('expires_at') if sub else None
        date_str = expires.strftime('%d.%m.%Y') if expires else '—'

        text = f"⚠️ {t('donation.cancel_confirm', lang, date=date_str)}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=t('donation.cancel_confirm_button', lang), callback_data="sub_cancel_do"),
                InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_subscription"),
            ]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _subscription_cancel_do(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отменить ежемесячный донат через Telegram API."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.subscription import get_active_subscription, cancel_subscription

        sub = await get_active_subscription(chat_id)
        if not sub:
            await callback.answer(t('donation.no_active', lang))
            await self.enter(user)
            return None

        charge_id = sub.get('telegram_payment_charge_id')

        try:
            await callback.bot.edit_user_star_subscription(
                user_id=chat_id,
                telegram_payment_charge_id=charge_id,
                is_canceled=True,
            )
        except Exception as e:
            logger.error(f"[Donation] Cancel API error: {e}")

        await cancel_subscription(chat_id, charge_id)

        text = t('donation.cancelled', lang)

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
            ]),
            parse_mode="Markdown",
        )
        return None

    async def _show_aisystant_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус подключения Aisystant и кнопки привязки."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.aisystant import get_aisystant_id, save_aisystant_link
        aisystant_id = await get_aisystant_id(chat_id)

        if aisystant_id:
            text = t('settings.aisystant_connected_info', lang)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
            return None

        # Пробуем найти автоматически
        from clients.aisystant import aisystant
        try:
            found_id = await aisystant.find_user_by_tg(chat_id)
            if found_id:
                await save_aisystant_link(chat_id, found_id)
                text = t('link.found_auto', lang)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
                ])
                await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
                return None
        except Exception as e:
            logger.error(f"[Settings] aisystant find_user_by_tg error: {e}")

        # Не привязан — показываем инструкцию + ссылку
        text = t('settings.aisystant_not_connected_info', lang)
        buttons = []
        try:
            tg_username = callback.from_user.username if callback.from_user else None
            link_url = await aisystant.get_link_url(chat_id, tg_username)
            buttons.append([InlineKeyboardButton(text=t('link.btn_link', lang), url=link_url)])
        except Exception as e:
            logger.error(f"[Settings] get_link_url error: {e}")

        buttons.append([InlineKeyboardButton(text=t('link.btn_check', lang), callback_data="conn_aisystant_check")])
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _aisystant_check(self, user, callback: CallbackQuery) -> Optional[str]:
        """Повторная проверка привязки Aisystant из настроек."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.aisystant import get_aisystant_id, save_aisystant_link

        existing = await get_aisystant_id(chat_id)
        if existing:
            await callback.answer(t('link.check_success', lang), show_alert=True)
            text = t('settings.aisystant_connected_info', lang)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
            return None

        from clients.aisystant import aisystant
        try:
            aisystant_id = await aisystant.find_user_by_tg(chat_id)
        except Exception as e:
            logger.error(f"[Settings] aisystant check error: {e}")
            await callback.answer(t('link.error', lang), show_alert=True)
            return None

        if aisystant_id:
            await save_aisystant_link(chat_id, aisystant_id)
            await callback.answer(t('link.check_success', lang), show_alert=True)
            text = t('settings.aisystant_connected_info', lang)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
            # Обновляем тир и клавиатуру
            try:
                from core.tier_detector import detect_ui_tier
                from core.tier_ui import build_reply_keyboard, sync_menu_commands
                tier = await detect_ui_tier(chat_id)
                kb = build_reply_keyboard(tier, lang)
                await callback.message.answer("👌", reply_markup=kb)
                await sync_menu_commands(callback.message.bot, chat_id, tier, lang)
            except Exception as e:
                logger.error(f"[Settings] refresh tier error: {e}")
        else:
            await callback.answer(t('link.check_not_yet', lang), show_alert=True)

        return None

    async def _show_connections(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем подключения к сторонним сервисам."""
        await self._set_waiting(user, None)  # сброс waiting при возврате в список
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Gateway (WP-209: единое подключение, заменяет Aisystant+DT+GitHub)
        from clients.gateway_mcp import gateway_mcp
        gateway_connected = gateway_mcp.is_connected(chat_id)

        # WakaTime (отдельный сервис, не через Gateway)
        from db.queries.wakatime import get_wakatime_connection
        waka_conn = await get_wakatime_connection(chat_id)
        if waka_conn:
            waka_user = waka_conn.get('wakatime_username', '')
            waka_status = f"✅ @{waka_user}" if waka_user else "✅ " + t('settings.connected', lang)
        else:
            waka_status = t('settings.not_connected', lang)

        # Google Calendar (отдельный сервис, не через Gateway)
        from clients.google_calendar_oauth import google_calendar_oauth
        gcal_connected = await google_calendar_oauth.is_connected(chat_id)

        # Чекбоксы: ✅ или ☐
        def chk(connected): return "✅" if connected else "☐"

        text = (
            f"*{t('settings.connections_label', lang)}*\n\n"
            f"{chk(gateway_connected)} Gateway (IWE)\n"
            f"{chk(waka_conn)} WakaTime\n"
            f"{chk(gcal_connected)} Календарь Google\n"
        )

        intern = await get_intern(chat_id)

        buttons = [
            [
                InlineKeyboardButton(text="🌐 Gateway (IWE)", callback_data="conn_gateway"),
            ],
            [
                InlineKeyboardButton(text="📊 WakaTime", callback_data="conn_waka"),
                InlineKeyboardButton(text="📅 Календарь Google", callback_data="conn_gcal"),
            ],
        ]

        # Notification toggles
        from core.tier_detector import detect_ui_tier
        tier = await detect_ui_tier(chat_id)
        iwe_visible = tier >= 2
        nudges_visible = tier >= 3

        toggle_row = []
        if iwe_visible:
            toggle_row.append(InlineKeyboardButton(text="🔔 IWE", callback_data="conn_iwe_toggle"))
        if nudges_visible:
            toggle_row.append(InlineKeyboardButton(text="💪 " + t('nudges.settings_nudges_button', lang), callback_data="conn_nudges_toggle"))
        if toggle_row:
            buttons.append(toggle_row)
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _gateway_reconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Переподключение Gateway — генерируем новую ссылку Ory OAuth."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.ory_oauth import ory_oauth
        from config import ORY_CLIENT_ID

        if not ORY_CLIENT_ID:
            await callback.message.edit_text(
                "Переподключение временно недоступно.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                ]),
            )
            return None

        auth_url, _ = await ory_oauth.get_authorization_url(chat_id)
        text = (
            "🔄 *Переподключение Gateway*\n\n"
            "Нажмите кнопку ниже для повторной авторизации.\n"
            "Это обновит токены доступа."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Авторизоваться", url=auth_url)],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _handle_gateway_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Gateway (IWE) — подключение/переподключение через Ory OAuth (WP-209)."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.gateway_mcp import gateway_mcp
        from clients.ory_oauth import ory_oauth
        from config import ORY_CLIENT_ID

        is_connected = gateway_mcp.is_connected(chat_id)

        if is_connected:
            text = (
                f"🌐 *Подключение к IWE — {t('settings.connected', lang)}*\n\n"
                "Бот может искать по знаниям платформы и вашим репо, "
                "читать Цифровой двойник и персонализировать ответы."
            )
            buttons = [
                [InlineKeyboardButton(text="🔄 Переподключить", callback_data="conn_gateway_reconnect")],
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
            ]
        else:
            if not ORY_CLIENT_ID:
                text = "🌐 *Подключение к IWE*\n\nПодключение временно недоступно."
                buttons = [[InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]]
            else:
                auth_url, _ = await ory_oauth.get_authorization_url(chat_id)
                text = (
                    "🌐 *Подключение к IWE*\n\n"
                    "Авторизуйтесь, чтобы бот мог:\n"
                    "• Искать по знаниям платформы и вашим личным репо\n"
                    "• Читать ваш Цифровой двойник\n"
                    "• Персонализировать ответы\n\n"
                    "Нажмите кнопку ниже для авторизации."
                )
                buttons = [
                    [InlineKeyboardButton(text="🔗 Подключить", url=auth_url)],
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
                ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _handle_github_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус GitHub или кнопку подключения."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        is_connected = await github_oauth.is_connected(chat_id)

        if is_connected:
            user_info = await github_oauth.get_user(chat_id)
            login = user_info.get("login", "user") if user_info else "user"
            target_repo = await github_oauth.get_target_repo(chat_id)
            notes_path = await github_oauth.get_notes_path(chat_id)

            lines = [f"🐙 *GitHub {t('settings.connected', lang)}*\n"]
            lines.append(f"{t('settings.github_user', lang)}: *{login}*")

            buttons = []
            if target_repo:
                lines.append(f"{t('settings.github_repo', lang)}: `{target_repo}`")
                lines.append(f"{t('settings.github_path', lang)}: `{notes_path}`")
            else:
                buttons.append([InlineKeyboardButton(
                    text=t('settings.github_select_repo', lang),
                    callback_data="github_select_repo",
                )])

            knowledge_repo = await github_oauth.get_knowledge_repo(chat_id)
            if knowledge_repo:
                lines.append(f"{t('settings.github_knowledge_repo', lang)}: `{knowledge_repo}`")
                buttons.append([InlineKeyboardButton(
                    text=t('settings.github_change_knowledge_repo', lang),
                    callback_data="github_select_knowledge_repo",
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=t('settings.github_select_knowledge_repo', lang),
                    callback_data="github_select_knowledge_repo",
                )])

            buttons.append([InlineKeyboardButton(
                text=t('settings.github_disconnect', lang),
                callback_data="github_disconnect",
            )])
            buttons.append([InlineKeyboardButton(
                text=t('buttons.back', lang),
                callback_data="upd_connections",
            )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            try:
                auth_url, state = await github_oauth.get_authorization_url(chat_id)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('settings.github_connect', lang), url=auth_url)],
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
                ])
                await callback.message.edit_text(
                    f"🐙 *GitHub*\n\n{t('settings.github_connect_desc', lang)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except (ValueError, Exception) as e:
                logger.error(f"GitHub OAuth error: {e}")
                await callback.message.edit_text(
                    f"🐙 *GitHub*\n\n{t('settings.github_unavailable', lang)}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                    ]),
                )

        return None

    async def _github_select_repo(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем список репозиториев для выбора."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        if not await github_oauth.is_connected(chat_id):
            await callback.message.edit_text(
                t('settings.not_connected', lang),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        repos = await github_oauth.get_repos(chat_id, limit=20)
        if not repos:
            await callback.message.edit_text(
                f"🐙 {t('settings.github_no_repos', lang)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        buttons = []
        for repo in repos[:10]:
            full_name = repo.get("full_name", "")
            name = repo.get("name", "")
            buttons.append([InlineKeyboardButton(
                text=name, callback_data=f"github_repo:{full_name}",
            )])
        buttons.append([InlineKeyboardButton(
            text=t('buttons.back', lang), callback_data="conn_github",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"🐙 *{t('settings.github_select_repo', lang)}:*",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return None

    async def _github_repo_selected(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохраняем выбранный репозиторий."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        repo_full_name = data.split(":", 1)[1]
        await github_oauth.set_target_repo(chat_id, repo_full_name)
        notes_path = await github_oauth.get_notes_path(chat_id)

        await callback.message.edit_text(
            f"✅ {t('settings.github_repo', lang)}: `{repo_full_name}`\n"
            f"{t('settings.github_path', lang)}: `{notes_path}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
            ]),
        )
        return None

    async def _github_select_knowledge_repo(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем список репозиториев для выбора индекса знаний."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        if not await github_oauth.is_connected(chat_id):
            await callback.message.edit_text(
                t('settings.not_connected', lang),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        repos = await github_oauth.get_repos(chat_id, limit=20)
        if not repos:
            await callback.message.edit_text(
                f"🐙 {t('settings.github_no_repos', lang)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        buttons = []
        for repo in repos[:10]:
            full_name = repo.get("full_name", "")
            name = repo.get("name", "")
            buttons.append([InlineKeyboardButton(
                text=name, callback_data=f"github_knowledge_repo:{full_name}",
            )])
        buttons.append([InlineKeyboardButton(
            text=t('buttons.back', lang), callback_data="conn_github",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"🐙 *{t('settings.github_select_knowledge_repo', lang)}:*",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return None

    async def _github_knowledge_repo_selected(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохраняем выбранный репозиторий индекса знаний."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        repo_full_name = data.split(":", 1)[1]
        await github_oauth.set_knowledge_repo(chat_id, repo_full_name)

        await callback.message.edit_text(
            f"✅ {t('settings.github_knowledge_repo', lang)}: `{repo_full_name}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
            ]),
        )
        return None

    async def _github_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключаем GitHub."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        if await github_oauth.is_connected(chat_id):
            await github_oauth.disconnect(chat_id)

        await callback.message.edit_text(
            f"🐙 GitHub {t('settings.not_connected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        # Тир мог измениться (T4→T3) — подсказка обновить меню
        await self.send(user, t('settings.connection_changed_hint', lang))
        return None

    async def _handle_twin_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус Digital Twin или кнопку подключения."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.digital_twin import digital_twin

        if digital_twin.is_connected(chat_id):
            lines = [f"🤖 *{t('settings.twin_label', lang)} — {t('settings.connected', lang)}*\n"]

            profile = await digital_twin.get_user_profile(chat_id)
            if profile and isinstance(profile, dict):
                degree = profile.get('degree') or t('twin.not_set_m', lang)
                stage = profile.get('stage') or t('twin.not_set_m', lang)
                lines.append(f"🎓 {t('twin.degree_label', lang)}: *{degree}*")
                lines.append(f"📊 {t('twin.stage_label', lang)}: *{stage}*")

            buttons = [
                [InlineKeyboardButton(
                    text=t('twin.btn_disconnect', lang),
                    callback_data="conn_twin_disconnect",
                )],
                [InlineKeyboardButton(
                    text=t('buttons.back', lang),
                    callback_data="upd_connections",
                )],
            ]

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            try:
                auth_url, state = digital_twin.get_authorization_url(chat_id)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('twin.btn_connect', lang), url=auth_url)],
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
                ])
                await callback.message.edit_text(
                    f"🤖 *{t('twin.connect_title', lang)}*\n\n{t('twin.connect_desc', lang)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"DT OAuth error: {e}")
                await callback.message.edit_text(
                    f"🤖 *{t('settings.twin_label', lang)}*\n\n{t('twin.unavailable_short', lang)}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                    ]),
                )

        return None

    async def _handle_club_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус Club или инструкцию подключения."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.discourse import get_discourse_account, get_published_posts
        account = await get_discourse_account(chat_id)

        if account:
            username = account["discourse_username"]
            cat_id = account.get("blog_category_id")

            posts = await get_published_posts(chat_id)

            lines = [f"🏛 *{t('settings.club_label', lang)} — {t('settings.connected', lang)}*\n"]
            lines.append(f"Username: *{username}*")
            if cat_id:
                lines.append(f"Блог: категория {cat_id}")
            else:
                lines.append("Блог: не указан — переподключись через /club connect")
            lines.append(f"Публикаций: {len(posts)}")

            buttons = [
                [InlineKeyboardButton(text="📝 Опубликовать", callback_data="club_publish_start")],
                [InlineKeyboardButton(text="🔌 Отключить", callback_data="conn_club_disconnect")],
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
            ]

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            from clients.discourse import discourse
            if not discourse:
                await callback.message.edit_text(
                    f"🏛 *{t('settings.club_label', lang)}*\n\n{t('settings.not_connected', lang)}.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                    ]),
                )
            else:
                await callback.message.edit_text(
                    "🏛 *Подключение к systemsworld.club*\n\n"
                    "Привяжи аккаунт, чтобы публиковать посты в личный блог клуба.\n\n"
                    "Для подключения нужна ссылка на твой блог в клубе.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Подключить", callback_data="club_connect_start")],
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
                    ]),
                )

        return None

    async def _club_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключаем Club."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.discourse import unlink_discourse_account
        await unlink_discourse_account(chat_id)

        await callback.message.edit_text(
            f"🏛 {t('settings.club_label', lang)}: " + t('settings.not_connected', lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        return None

    async def _twin_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключаем Digital Twin."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.digital_twin import digital_twin

        if digital_twin.is_connected(chat_id):
            digital_twin.disconnect(chat_id)
        # Clear persistent flag
        try:
            from db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute('UPDATE public.users SET dt_connected_at = NULL WHERE telegram_id = $1', chat_id)
        except Exception:
            pass

        await callback.message.edit_text(
            f"🤖 {t('settings.twin_label', lang)}: {t('settings.not_connected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        # Тир мог измениться (T3→T2) — подсказка обновить меню
        await self.send(user, t('settings.connection_changed_hint', lang))
        return None

    # =========== Google Calendar ===========

    async def _handle_gcal_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус Google Calendar или предлагаем подключить."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.google_calendar_oauth import google_calendar_oauth

        if await google_calendar_oauth.is_connected(chat_id):
            data = await google_calendar_oauth._get_cached(chat_id)
            email = data.get('email', '') if data else ''
            email_line = f"\nАккаунт: *{email}*" if email else ""
            text = f"*Календарь Google {t('settings.connected', lang)}*{email_line}\n\n/calendar — просмотр событий"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('settings.waka_disconnect', lang), callback_data="conn_gcal_disconnect")],
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
            ])
        else:
            try:
                auth_url, state = google_calendar_oauth.get_authorization_url(chat_id)
            except ValueError:
                await callback.message.edit_text(
                    "Календарь Google не настроен.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                    ]),
                )
                return None

            text = "*Календарь Google*\n\nПодключите для просмотра событий через бота.\nБот получит доступ только для чтения."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Подключить", url=auth_url)],
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
            ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _gcal_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключить Google Calendar."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.google_calendar_oauth import google_calendar_oauth
        await google_calendar_oauth.disconnect(chat_id)

        await callback.message.edit_text(
            f"Календарь Google: {t('settings.not_connected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        return None

    # =========== WakaTime ===========

    async def _handle_waka_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус WakaTime или предлагаем подключить."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.wakatime import get_wakatime_connection
        waka_conn = await get_wakatime_connection(chat_id)

        if waka_conn:
            waka_user = waka_conn.get('wakatime_username', '')
            text = "📊 *WakaTime подключён*\n"
            if waka_user:
                text += f"User: *{waka_user}*\n"
            text += (
                "\nЧто даёт:\n"
                "• Время работы в IWE учитывается в цифровом двойнике\n"
                "• На основе этих данных считается мультипликатор IWE\n"
                "• Данные поступают автоматически\n"
                f"\n/waka — {t('settings.waka_check_stats', lang)}"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('settings.waka_disconnect', lang), callback_data="conn_waka_disconnect")],
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
            ])
        else:
            text = (
                "📊 *WakaTime* — трекинг времени работы в IWE\n\n"
                "Что даёт:\n"
                "• Автоматический учёт времени работы в VS Code\n"
                "• Данные попадают в цифровой двойник для аналитики\n"
                "• На основе этих данных считается мультипликатор IWE — "
                "сколько результатов вы создаёте на час работы\n"
                "• Статистика доступна через /waka"
            )

            # OAuth-кнопка (если настроен) + fallback на ввод API-ключа
            buttons = []
            try:
                from clients.wakatime_oauth import wakatime_oauth
                auth_url, state = wakatime_oauth.get_authorization_url(chat_id)
                buttons.append([InlineKeyboardButton(text="Подключить WakaTime", url=auth_url)])
                text += (
                    "\n\nНужен аккаунт на wakatime.com (бесплатная регистрация).\n\n"
                    "Для подключения нажмите кнопку под сообщением "
                    "(или вручную — введите API-ключ):"
                )
            except Exception:
                text += (
                    "\n\nНужен аккаунт на wakatime.com (бесплатная регистрация).\n\n"
                    f"{t('settings.waka_enter_key', lang)}"
                )

            buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await self._set_waiting(user, 'wakatime_key')

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _waka_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключить WakaTime."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from db.queries.wakatime import delete_wakatime_connection
        await delete_wakatime_connection(chat_id)

        await callback.message.edit_text(
            f"📊 WakaTime: {t('settings.not_connected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        return None

    async def _show_iwe_details(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показать описание IWE Updates с кнопкой включения/выключения."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        intern = await get_intern(chat_id)
        current = intern.get('notify_template_updates', False)
        status = "✅" if current else "❌"
        action = t('settings.iwe_updates_disable', lang) if current else t('settings.iwe_updates_enable', lang)

        status_label = "Статус" if lang == 'ru' else "Status"
        text = (
            t('settings.iwe_updates_description', lang) + "\n\n"
            f"*{status_label}:* {status}"
        )

        buttons = [
            [InlineKeyboardButton(
                text=f"{'🔕' if current else '🔔'} {action.capitalize()}",
                callback_data="conn_iwe_do_toggle",
            )],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
        ]

        await callback.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return None

    async def _toggle_iwe_updates(self, user, callback: CallbackQuery) -> Optional[str]:
        """Переключить подписку на обновления шаблона IWE."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        intern = await get_intern(chat_id)
        current = intern.get('notify_template_updates', False)
        new_value = not current

        await update_intern(chat_id, notify_template_updates=new_value)

        if new_value:
            await callback.answer(t('settings.iwe_updates_enabled_toast', lang), show_alert=True)
        else:
            await callback.answer(t('settings.iwe_updates_disabled_toast', lang))

        # Перерисовать экран деталей IWE
        return await self._show_iwe_details(user, callback)

    async def _show_nudges_details(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показать описание подбадривания с кнопкой вкл/выкл."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        intern = await get_intern(chat_id)
        current = intern.get('notify_nudges', True) if intern.get('notify_nudges') is not None else True

        status = "✅" if current else "❌"
        action = t('nudges.settings_nudges_disable', lang) if current else t('nudges.settings_nudges_enable', lang)

        text = (
            f"💪 *{t('nudges.settings_nudges_button', lang)}* {status}\n\n"
            f"{t('nudges.settings_nudges_description', lang)}"
        )

        buttons = [
            [InlineKeyboardButton(
                text=f"{'🔕' if current else '🔔'} {action}",
                callback_data="conn_nudges_do_toggle",
            )],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
        ]

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
        return None

    async def _toggle_nudges(self, user, callback: CallbackQuery) -> Optional[str]:
        """Переключить engagement nudge-уведомления (WP-85 5C)."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        intern = await get_intern(chat_id)
        current = intern.get('notify_nudges', True) if intern.get('notify_nudges') is not None else True
        new_value = not current

        await update_intern(chat_id, notify_nudges=new_value)

        # Перерисовать экран деталей nudges
        return await self._show_nudges_details(user, callback)
