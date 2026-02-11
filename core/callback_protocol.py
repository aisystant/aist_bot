"""
Единый протокол callback_data для inline кнопок.

Формат: {service_id}:{action}:{payload}
Примеры:
  - "service:learning"         — вход в сервис из главного меню
  - "learning:start:marathon"  — действие внутри сервиса
  - "plans:view:rp"            — просмотр РП
  - "feed:select:topic_1"      — выбор темы в ленте

Обратная совместимость: старые callback_data (без ":") продолжают работать
через fallback в handlers/callbacks.py.
"""


def encode(service_id: str, action: str, payload: str = "") -> str:
    """Кодирует callback_data по протоколу.

    Args:
        service_id: ID сервиса ("learning", "plans", ...)
        action: Действие ("start", "select", "view", ...)
        payload: Дополнительные данные (ID, параметры)

    Returns:
        Строка callback_data (макс. 64 байта для Telegram)
    """
    if payload:
        return f"{service_id}:{action}:{payload}"
    return f"{service_id}:{action}"


def decode(callback_data: str) -> tuple[str, str, str]:
    """Декодирует callback_data по протоколу.

    Args:
        callback_data: Строка из Telegram callback

    Returns:
        Кортеж (service_id, action, payload).
        Если формат не совпадает, возвращает ("", "", callback_data).
    """
    parts = callback_data.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return "", "", callback_data


def matches(callback_data: str, service_id: str) -> bool:
    """Проверяет, относится ли callback к указанному сервису.

    Args:
        callback_data: Строка из Telegram callback
        service_id: ID сервиса для проверки
    """
    decoded_service, _, _ = decode(callback_data)
    return decoded_service == service_id


def is_protocol(callback_data: str) -> bool:
    """Проверяет, использует ли callback_data новый протокол."""
    return ":" in callback_data
