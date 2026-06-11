"""
Х3 — выбор траектории: делегация Диагносту + оффер курса (WP-406 Ф5, «Фундамент»).

# see DP.SC.170, DP.ROLE.067

Х3 закрывается тремя частями (карточка WP-406, критерий строки 123):
  Х3.1 ступень 1-5      — готово: Диагност R28 (handlers/diagnose.py).
  Х3.2 узкое место      — готово: Диагност возвращает bottleneck_slot.
  Х3.3 выбор курса      — Онбордер транслирует recommended_stream в оффер курса.

`recommended_stream` уже в проде (db/queries/cp_assessment.py:compute_cp_stage):
  "РР"  — для ступени 5 (Проактивный → программа «Рабочее развитие», WP-371);
  "S1".."S4" — поток развития по ступени.

«Фундамент»: describe_stream — реальная чистая трансляция потока в человеко-
читаемое имя (без выдумывания каталога курсов — конкретный курс выбирает срез Х3
по доменной таблице). run_x3 — заглушка интерфейса полного среза.
"""

# Человекочитаемые имена потоков. Источник семантики — compute_cp_stage
# (cp_assessment.py): "РР" = «Рабочее развитие» (WP-371), "S{n}" = поток ступени n.
_WORK_DEVELOPMENT_STREAM = "РР"


def describe_stream(recommended_stream: str) -> str:
    """Перевести код рекомендованного потока в человекочитаемое имя.

    Args:
        recommended_stream: значение из compute_cp_stage ("РР" | "S1".."S4").
    Returns:
        Строка для показа пилоту. Неизвестный код возвращается как есть
        (не выдумываем имя, см. правило «не изобретать имена артефактов»).
    """
    if recommended_stream == _WORK_DEVELOPMENT_STREAM:
        return "программа «Рабочее развитие»"
    if recommended_stream.startswith("S") and recommended_stream[1:].isdigit():
        return f"поток личного развития ступени {recommended_stream[1:]} ({recommended_stream})"
    return recommended_stream


async def run_x3(intern: dict, message) -> None:
    """Полный срез Х3: Диагност → recommended_stream → оффер курса → отметка.

    Заглушка интерфейса (complete vertical slice — отдельный заход):
      1. вызвать/прочитать результат Диагноста R28 (ступень + узкое место);
      2. describe_stream(recommended_stream) → человекочитаемый оффер курса;
      3. при подтверждении курса — storage.mark_x3_done(chat_id);
      4. перенаправить кнопку «Помоги выбрать» (handlers/onboarding.py:666)
         с Навигатора на Онбордера.
    """
    raise NotImplementedError(
        "run_x3: полный срез Х3 реализуется отдельным заходом "
        "(Диагност → recommended_stream → оффер → отметка + кнопка 666)."
    )
