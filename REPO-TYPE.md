# Тип репозитория

**Тип**: `Downstream/instrument`

**Source-of-truth**: no

## Роль

Telegram-бот марафона личного развития. Реализует взаимодействие с пользователем на основе знаний из pack'ов.

## Upstream dependencies

- [aisystant/PACK-personal](https://github.com/aisystant/PACK-personal) — source-of-truth области созидателя
- [aisystant/digital-twin-mcp](https://github.com/aisystant/digital-twin-mcp) — MCP для работы с индикаторами

## Downstream outputs

- Telegram-интерфейс для пользователей
- Марафон личного развития
- Интеграция с цифровым двойником

## Non-goals

- НЕ является source-of-truth (определения в pack'ах)
- НЕ определяет «что такое марафон» — реализует его
- НЕ содержит предметного знания области

## Что содержит

- Код Telegram-бота (Python)
- State Machine для диалогов
- Интеграция с Claude API
- Интеграция с digital-twin-mcp
