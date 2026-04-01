"""
Клиент ЮКасса API (WP-181 Ф7).

Прямые HTTP-запросы через aiohttp, без SDK.
Документация: https://yookassa.ru/developers/api
"""

import hashlib
import hmac
import json
import logging
import uuid

import aiohttp

logger = logging.getLogger(__name__)


class YooKassaClient:
    """Клиент для ЮКасса Payment API v3."""

    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str):
        self.shop_id = shop_id
        self.secret_key = secret_key

    async def create_payment(
        self,
        amount: int,
        currency: str = "RUB",
        description: str = "Семинар IWE",
        return_url: str = "https://t.me/aist_me_bot",
        metadata: dict | None = None,
        customer_email: str | None = None,
    ) -> dict:
        """Создать платёж и вернуть confirmation_url.

        Args:
            amount: сумма в рублях (целое число)
            currency: валюта (RUB)
            description: описание платежа
            return_url: URL для возврата после оплаты
            metadata: произвольные данные (telegram_id и т.д.)
            customer_email: email покупателя для чека (если нет — чек без email)

        Returns:
            dict с ключами: id, confirmation_url, status
        """
        idempotence_key = str(uuid.uuid4())

        # Фискальный чек (ФФД 1.2, обязателен при подключённой онлайн-кассе)
        receipt = {
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{amount}.00",
                        "currency": currency,
                    },
                    "vat_code": 1,  # без НДС
                    "payment_subject": "service",
                    "payment_mode": "full_payment",
                }
            ],
        }
        if customer_email:
            receipt["customer"] = {"email": customer_email}
        else:
            # ЮКасса требует хотя бы email или phone в customer
            receipt["customer"] = {"email": "seminar@aisystant.com"}

        payload = {
            "amount": {
                "value": f"{amount}.00",
                "currency": currency,
            },
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "capture": True,
            "description": description,
            "receipt": receipt,
        }
        if metadata:
            payload["metadata"] = metadata

        headers = {
            "Idempotence-Key": idempotence_key,
            "Content-Type": "application/json",
        }

        auth = aiohttp.BasicAuth(self.shop_id, self.secret_key)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE_URL}/payments",
                json=payload,
                headers=headers,
                auth=auth,
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"[YooKassa] create_payment error: status={resp.status}, body={data}")
                    raise Exception(f"YooKassa API error: {resp.status} {data}")

        confirmation_url = data.get("confirmation", {}).get("confirmation_url", "")
        logger.info(
            f"[YooKassa] payment created: id={data['id']}, status={data['status']}, "
            f"amount={amount} {currency}"
        )

        return {
            "id": data["id"],
            "confirmation_url": confirmation_url,
            "status": data["status"],
        }

    @staticmethod
    def verify_notification(body: bytes, provided_ip: str) -> bool:
        """Проверить что webhook пришёл от ЮКасса.

        ЮКасса отправляет webhooks с IP-адресов:
        185.71.76.0/27 и 185.71.77.0/27

        Args:
            body: тело запроса (bytes)
            provided_ip: IP-адрес отправителя
        """
        # ЮКасса рекомендует проверять по IP
        # https://yookassa.ru/developers/using-api/webhooks#ip
        yookassa_nets = [
            "185.71.76.",  # 185.71.76.0/27
            "185.71.77.",  # 185.71.77.0/27
            "77.75.153.",  # 77.75.153.0/25
            "77.75.156.",  # 77.75.156.0/24
            "127.0.0.1",   # localhost (dev)
        ]
        return any(provided_ip.startswith(net) for net in yookassa_nets)
