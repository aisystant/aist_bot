from __future__ import annotations

"""
Helper utilities для UI «Применить баллы?» в workshop/showcase оплатах (WP-327 Ф3).

Centralizes:
- Discount eligibility check (resolve_ory_id_from_chat → available_discount).
- UI: текст предложения скидки + клавиатура «применить / без скидки».
- Reserve-burn helpers для YooKassa (real payment_id) и TG Stars (provisional UUID).

Тексты пока на русском inline — TODO Этап 4: вынести в i18n/schema.yaml после калибровки.
"""

import logging
import uuid
from decimal import Decimal
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.queries.redeem import available_discount, reserve_burn
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)


async def prepare_burn_offer(chat_id: int, full_amount_rub: int) -> Optional[dict]:
    """Проверить применимость скидки баллами для пилота.

    Returns:
        dict с полями {copilka_pts, available_pts, discount_rub, payable_rub,
        qualification, ceiling_pts, account_id} — если discount_rub > 0.
        None — если пилот T0 (нет ory_id), без баланса, или ошибка вычисления.
    """
    try:
        account_id = await resolve_ory_id_from_chat(chat_id)
        if not account_id:
            return None

        info = await available_discount(account_id, Decimal(full_amount_rub))
        if info["discount_rub"] <= 0:
            return None

        info["account_id"] = account_id
        return info
    except Exception as e:
        logger.warning(f"[Redeem] prepare_burn_offer failed: tg={chat_id}, error={e}")
        return None


def format_burn_offer_text(burn_info: dict, item_title: str = "оплаты") -> str:
    """Текст «У вас N баллов, применить скидку X₽?»."""
    return (
        f"💰 На копилке {burn_info['copilka_pts']:.0f} баллов.\n\n"
        f"Можно применить скидку <b>{burn_info['discount_rub']:.0f} ₽</b> "
        f"(спишется {burn_info['available_pts']:.0f} баллов).\n\n"
        f"Степень: {burn_info['qualification']}\n"
        f"Доплата картой: <b>{burn_info['payable_rub']:.0f} ₽</b>\n\n"
        f"Применить скидку для {item_title}?"
    )


def build_burn_offer_keyboard(
    apply_data: str,
    skip_data: str,
    full_amount_rub: int,
    back_data: Optional[str] = None,
    back_text: str = "← Назад",
) -> InlineKeyboardMarkup:
    """Клавиатура: применить скидку / оплатить полностью / назад."""
    rows = [
        [InlineKeyboardButton(text="✅ Применить скидку", callback_data=apply_data)],
        [InlineKeyboardButton(text=f"💳 Без скидки ({full_amount_rub} ₽)", callback_data=skip_data)],
    ]
    if back_data:
        rows.append([InlineKeyboardButton(text=back_text, callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def reserve_for_yookassa(
    burn_info: dict,
    payment_id: str,
    purpose: str,
) -> tuple[bool, Decimal]:
    """Зарезервировать баллы для уже созданного YK-платежа.

    Returns:
        (success, points_amount). success=False → race lost, баллы НЕ списаны.
    """
    points_amount = Decimal(str(burn_info["available_pts"]))

    ok = await reserve_burn(
        account_id=burn_info["account_id"],
        payment_id=payment_id,
        points_amount=points_amount,
        payment_source="yookassa",
        purpose=purpose,
        qualification_snapshot=burn_info["qualification"],
        daily_cap_snapshot=Decimal(str(burn_info["ceiling_pts"])),
    )
    return ok, points_amount


async def reserve_for_tg_stars(
    burn_info: dict,
    purpose: str,
) -> tuple[Optional[str], Decimal]:
    """Зарезервировать баллы для TG Stars (provisional UUID как payment_id).

    Returns:
        (provisional_id, points_amount). provisional_id=None при ошибке.
    """
    provisional_id = f"tg_{uuid.uuid4().hex}"
    points_amount = Decimal(str(burn_info["available_pts"]))

    ok = await reserve_burn(
        account_id=burn_info["account_id"],
        payment_id=provisional_id,
        points_amount=points_amount,
        payment_source="tg_stars",
        purpose=purpose,
        qualification_snapshot=burn_info["qualification"],
        daily_cap_snapshot=Decimal(str(burn_info["ceiling_pts"])),
    )
    return (provisional_id if ok else None), points_amount


def discount_stars(burn_info: dict, full_amount_stars: int, full_amount_rub: int) -> int:
    """Сколько Stars осталось доплатить после применения скидки.

    Пропорционально RUB-цене: payable_stars = full_stars × (payable_rub / full_rub).
    """
    if full_amount_rub <= 0:
        return full_amount_stars
    ratio = Decimal(str(burn_info["payable_rub"])) / Decimal(full_amount_rub)
    payable = (Decimal(full_amount_stars) * ratio).quantize(Decimal("1"))
    return max(1, int(payable))
