# Тип репозитория

**Тип**: `Downstream/instrument`
**Система (SoI)**: Бот Aist
**Содержание**: code
**Для кого**: team
**Source-of-truth**: no

## Upstream dependencies

- [aisystant/PACK-personal](https://github.com/aisystant/PACK-personal) — source-of-truth области созидателя
- [aisystant/digital-twin-mcp](https://github.com/aisystant/digital-twin-mcp) — MCP для работы с индикаторами
- [TserenTserenov/PACK-digital-platform](https://github.com/TserenTserenov/PACK-digital-platform) — архитектура платформы

## Downstream outputs

- Telegram-интерфейс для пользователей
- Марафон личного развития (State Machine архитектура)
- Интеграция с цифровым двойником, Linear, MCP Guides/Knowledge

## Non-goals

- НЕ является source-of-truth (определения в pack'ах)
- НЕ определяет «что такое марафон» — реализует его
- НЕ содержит предметного знания области
