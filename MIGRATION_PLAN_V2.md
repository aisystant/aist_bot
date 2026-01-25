# План миграции aist_bot: State Machine Architecture

## Цель документа

Этот документ — инструкция для Claude Code по пошаговой миграции Telegram-бота aist_bot на архитектуру State Machine. Миграция должна проходить **без нарушения работоспособности бота** через feature flags.

---

# Часть 1. Концептуальная модель

## 1.1. Принцип State Machine

**Один Python-файл = один стейт (состояние).**

Каждый пользователь в любой момент времени находится в определённом состоянии. Состояние хранится в базе данных. Когда приходит сообщение:

1. Загружаем `user.current_state` из БД
2. Находим соответствующий стейт-класс
3. Вызываем `state.handle(message)`
4. Стейт возвращает событие (например, `"correct"`, `"skip"`)
5. По таблице переходов определяем следующий стейт
6. Сохраняем новый `user.current_state` в БД

```
Сообщение → Загрузка состояния → Обработка → Событие → Переход → Сохранение
```

## 1.2. Четыре бизнес-категории

Стейты группируются по типу взаимодействия:

| Категория | Описание | Namespace | Характер стейтов |
|-----------|----------|-----------|------------------|
| **Общие** (common) | Общие процессы | `common.*` | Онбординг, выбор режима, консультация |
| **Мастерские** (workshops) | Программы со строгой структурой | `workshop.*` | Цепочки стейтов с прогрессом |
| **Лента** (feed) | Гибкое обучение по дайджестам | `feed.*` | Выбор тем → дайджест → фиксация |
| **Утилиты** (utilities) | Одно действие — один результат | `utility.*` | Атомарные стейты |

> **Важно:** Стейты называются по **процессам** (consultation), а не по агентам (consultant).

## 1.3. Мастерские

| Мастерская | Что изготавливается | Стейты |
|------------|---------------------|--------|
| **Марафон** | Мастерство ученика за 14 дней | lesson → question → bonus → task → (repeat) |
| **Экзокортекс** | Настроенный личный экзокортекс | audit → tools → setup → practice |
| **FPFkids** | Система обучения ребёнка | goals → topics → plan → session |
| **Задачник** | Навык через практику | topic_select → problem → solution → review |

## 1.4. Глобальные процессы

| Процесс | Триггер | Стейт | Возврат |
|---------|---------|-------|---------|
| **Консультация** | `?` | `common.consultation` | `_previous` |
| **Заметки** | `/note` | `utility.notes` | `_previous` |
| **Экспорт** | `/export` | `utility.export` | `_previous` |

## 1.5. Лента (Feed)

| Стейт | Назначение |
|-------|------------|
| `feed.topics` | Выбор тем на неделю |
| `feed.digest` | Показ дайджеста, ожидание фиксации |

## 1.6. Утилиты

| Утилита | Действие | Стейт |
|---------|----------|-------|
| **Заметочник** | Сохранить мысль | `utility.notes` |
| **Экспорт** | Выгрузить в Obsidian | `utility.export` |

## 1.7. Критическое правило: Единая консультация

**Консультация одна.** Нет отдельных консультаций по экзокортексу, детям, задачам.

Пользователь задаёт вопрос через `?`. Процесс консультации определяет тему и ищет сначала в базе знаний соответствующей мастерской, потом в общей базе.

```python
TOPIC_MAPPING = {
    "экзокортекс": "exocortex",
    "заметки": "exocortex",
    "obsidian": "exocortex",
    "ребёнок": "fpfkids",
    "дети": "fpfkids",
    "задача": "practice",
    "марафон": "marathon",
}
```

---

# Часть 2. Целевая структура репозитория

```
aist_bot/
├── bot.py                              # Точка входа
│
├── states/                              # 🎯 ВСЕ СТЕЙТЫ
│   ├── __init__.py
│   ├── base.py                         # BaseState
│   ├── registry.py                     # Реестр всех стейтов
│   │
│   ├── common/                         # Общие стейты
│   │   ├── __init__.py
│   │   ├── start.py                    # Начало / онбординг
│   │   ├── error.py                    # Обработка ошибок
│   │   ├── mode_select.py              # Выбор режима
│   │   └── consultation.py             # Консультация (глобальный процесс)
│   │
│   ├── workshops/                      # Стейты мастерских
│   │   ├── __init__.py
│   │   ├── marathon/
│   │   │   ├── __init__.py
│   │   │   ├── lesson.py               # Показ урока
│   │   │   ├── question.py             # Вопрос на понимание
│   │   │   ├── bonus.py                # Бонусный вопрос
│   │   │   └── task.py                 # Задание
│   │   ├── exocortex/
│   │   │   ├── __init__.py
│   │   │   ├── audit.py
│   │   │   ├── tools.py
│   │   │   └── setup.py
│   │   ├── fpfkids/
│   │   │   ├── __init__.py
│   │   │   ├── goals.py
│   │   │   ├── topics.py
│   │   │   └── session.py
│   │   └── practice/
│   │       ├── __init__.py
│   │       ├── topic_select.py
│   │       ├── problem.py
│   │       └── solution.py
│   │
│   ├── feed/                           # Стейты Ленты
│   │   ├── __init__.py
│   │   ├── topics.py                   # Выбор тем на неделю
│   │   └── digest.py                   # Показ дайджеста
│   │
│   └── utilities/                      # Стейты утилит
│       ├── __init__.py
│       ├── notes.py                    # Заметочник
│       └── export.py                   # Экспорт
│
├── core/                                # ⚙️ ЯДРО
│   ├── __init__.py
│   ├── machine.py                      # StateMachine — движок переходов
│   ├── storage.py                      # Хранение состояния в БД
│   ├── middleware.py                   # Telegram middleware
│   │
│   └── knowledge/                      # Базы знаний для консультанта
│       ├── __init__.py
│       ├── loader.py
│       ├── router.py                   # Роутинг по темам
│       └── base/                       # Общая база
│           └── systems_thinking.md
│
├── content/                             # 📚 КОНТЕНТ (отдельно от кода)
│   ├── workshops/
│   │   ├── marathon/
│   │   │   ├── day01/
│   │   │   │   ├── lesson.yaml
│   │   │   │   ├── question.yaml
│   │   │   │   └── task.yaml
│   │   │   └── ...
│   │   ├── exocortex/
│   │   │   ├── methodology/
│   │   │   │   └── tables/
│   │   │   └── steps/
│   │   ├── fpfkids/
│   │   │   └── scenarios/
│   │   └── practice/
│   │       └── problem_bank/
│   │
│   ├── feed/                           # Контент Ленты
│   │   └── topics.yaml
│   │
│   └── knowledge/                      # Базы знаний мастерских
│       ├── marathon.md
│       ├── exocortex.md
│       ├── fpfkids.md
│       └── practice.md
│
├── integrations/                        # 🔌 ИНТЕГРАЦИИ
│   ├── __init__.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── claude.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   └── keyboards.py
│   └── export/                         # Адаптеры экспорта
│       ├── __init__.py
│       ├── base.py
│       ├── obsidian.py
│       ├── notion.py
│       └── markdown.py
│
├── i18n/                                # 🌍 ЛОКАЛИЗАЦИЯ
│   ├── __init__.py
│   ├── loader.py
│   ├── ru/
│   │   ├── common.yaml
│   │   ├── states.yaml
│   │   └── errors.yaml
│   ├── en/
│   └── es/
│
├── db/                                  # 💾 БАЗА ДАННЫХ
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── user.py                     # User + current_state
│   │   ├── progress.py
│   │   └── note.py
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── user_repo.py
│   │   └── progress_repo.py
│   └── migrations/
│
├── config/                              # ⚙️ КОНФИГУРАЦИЯ
│   ├── __init__.py
│   ├── settings.py
│   ├── transitions.yaml                # 🎯 ТАБЛИЦА ПЕРЕХОДОВ
│   └── features.yaml                   # Feature flags
│
├── tests/
│   ├── unit/
│   │   ├── states/
│   │   └── core/
│   ├── integration/
│   └── manual/
│
└── docs/
    ├── ontology.md
    ├── architecture.md
    ├── states.md                       # Описание всех стейтов
    └── CLAUDE.md
```

---

# Часть 3. Ключевые компоненты

## 3.1. BaseState

```python
# states/base.py
from abc import ABC, abstractmethod
from typing import Optional, Any
from aiogram.types import Message


class BaseState(ABC):
    """
    Базовый класс для всех стейтов.
    Один стейт = один файл.
    """
    
    # Уникальный идентификатор стейта
    # Формат: "category.name" или "category.subcategory.name"
    # Примеры: "common.start", "workshop.marathon.lesson", "common.consultation"
    name: str = "base"

    # Человекочитаемое название для логов и отладки
    display_name: dict[str, str] = {"ru": "Базовый стейт", "en": "Base State"}

    # Глобальные команды, доступные в этом стейте
    # Эти команды вызывают переход независимо от логики стейта
    allow_global: list[str] = []  # ["consultation", "notes"]
    
    def __init__(self, bot, db, llm, i18n):
        """
        Args:
            bot: Telegram bot instance
            db: Database repository
            llm: LLM client (Claude)
            i18n: Localization service
        """
        self.bot = bot
        self.db = db
        self.llm = llm
        self.i18n = i18n
    
    async def enter(self, user, context: dict = None) -> None:
        """
        Вызывается при ВХОДЕ в стейт.
        Здесь обычно отправляется приветственное сообщение.
        
        Args:
            user: Объект пользователя из БД
            context: Дополнительные данные от предыдущего стейта
        """
        pass
    
    @abstractmethod
    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатывает входящее сообщение.
        
        Args:
            user: Объект пользователя
            message: Сообщение от Telegram
            
        Returns:
            Событие для перехода (str) или None если остаёмся в стейте.
            Примеры: "correct", "skip", "done", "error"
        """
        pass
    
    async def exit(self, user) -> dict:
        """
        Вызывается при ВЫХОДЕ из стейта.
        
        Returns:
            Контекст для передачи следующему стейту
        """
        return {}
    
    # === Вспомогательные методы ===
    
    def t(self, key: str, user, **kwargs) -> str:
        """Shortcut для локализации"""
        return self.i18n.t(key, user.language, **kwargs)
    
    async def send(self, user, text: str, **kwargs):
        """Shortcut для отправки сообщения"""
        await self.bot.send_message(user.telegram_id, text, **kwargs)
    
    async def send_with_keyboard(self, user, text: str, buttons: list[list[str]]):
        """Отправка с reply keyboard"""
        from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=btn) for btn in row] for row in buttons],
            resize_keyboard=True
        )
        await self.bot.send_message(user.telegram_id, text, reply_markup=keyboard)
```

## 3.2. Таблица переходов

```yaml
# config/transitions.yaml

# ============================================
# ТАБЛИЦА ПЕРЕХОДОВ STATE MACHINE
# ============================================
# Формат:
#   state_name:
#     events:
#       event_name: next_state_name
#     allow_global: [list of global events]
#
# Специальные значения next_state:
#   _previous - вернуться в предыдущий стейт
#   _same - остаться в текущем стейте
# ============================================

states:

  # ==========================================
  # ОБЩИЕ СТЕЙТЫ
  # ==========================================
  
  common.start:
    description: "Начало работы, онбординг нового пользователя"
    events:
      new_user: common.start           # Продолжаем онбординг
      onboarding_complete: common.mode_select
      existing_user: common.mode_select
      error: common.error
  
  common.error:
    description: "Обработка ошибок"
    events:
      retry: common.start
      continue: _previous
  
  common.mode_select:
    description: "Выбор режима работы"
    events:
      marathon: workshop.marathon.lesson
      feed: feed.topics
      exocortex: workshop.exocortex.audit
      fpfkids: workshop.fpfkids.goals
      practice: workshop.practice.topic_select
      settings: common.settings
    allow_global: [consultation, notes]

  common.settings:
    description: "Настройки пользователя"
    events:
      saved: common.mode_select
      cancel: _previous
  
  # ==========================================
  # МАСТЕРСКАЯ: МАРАФОН
  # ==========================================

  workshop.marathon.lesson:
    description: "Показ урока текущего дня"
    events:
      lesson_shown: workshop.marathon.question
      already_completed: workshop.marathon.task
      marathon_complete: common.mode_select
    allow_global: [consultation, notes]

  workshop.marathon.question:
    description: "Вопрос на понимание урока"
    events:
      correct: workshop.marathon.bonus   # Уровни 2-3 → бонусный вопрос
      correct_level_1: workshop.marathon.task  # Уровень 1 → сразу задание
      incorrect: _same                   # Повторяем вопрос
      skip: workshop.marathon.task
      hint: _same                        # Показываем подсказку
    allow_global: [consultation, notes]

  workshop.marathon.bonus:
    description: "Бонусный вопрос повышенной сложности"
    events:
      yes: _same                         # Отвечаем на бонусный вопрос
      answered: workshop.marathon.task   # Ответ принят → задание
      no: workshop.marathon.task         # Отказ → сразу к заданию
    allow_global: [consultation, notes]

  workshop.marathon.task:
    description: "Практическое задание"
    events:
      submitted: workshop.marathon.lesson  # Следующий урок
      feedback_requested: _same
      day_complete: workshop.marathon.lesson
    allow_global: [consultation, notes]
  
  # ==========================================
  # МАСТЕРСКАЯ: ЭКЗОКОРТЕКС
  # ==========================================
  
  workshop.exocortex.audit:
    description: "Аудит текущего состояния экзокортекса"
    events:
      audit_complete: workshop.exocortex.tools
      skip: workshop.exocortex.tools
    allow_global: [consultation, notes]
  
  workshop.exocortex.tools:
    description: "Выбор инструментов"
    events:
      tools_selected: workshop.exocortex.setup
      back: workshop.exocortex.audit
    allow_global: [consultation, notes]
  
  workshop.exocortex.setup:
    description: "Настройка выбранных инструментов"
    events:
      step_complete: _same              # Следующий шаг настройки
      setup_complete: workshop.exocortex.practice
      back: workshop.exocortex.tools
    allow_global: [consultation, notes]
  
  workshop.exocortex.practice:
    description: "Практика использования экзокортекса"
    events:
      practice_complete: common.mode_select
      continue: _same
    allow_global: [consultation, notes]
  
  # ==========================================
  # МАСТЕРСКАЯ: FPFKIDS
  # ==========================================
  
  workshop.fpfkids.goals:
    description: "Определение целей обучения ребёнка"
    events:
      goals_set: workshop.fpfkids.topics
      skip: workshop.fpfkids.topics
    allow_global: [consultation, notes]
  
  workshop.fpfkids.topics:
    description: "Выбор тем для изучения"
    events:
      topics_selected: workshop.fpfkids.plan
      back: workshop.fpfkids.goals
    allow_global: [consultation, notes]
  
  workshop.fpfkids.plan:
    description: "Составление плана занятий"
    events:
      plan_ready: workshop.fpfkids.session
      back: workshop.fpfkids.topics
    allow_global: [consultation, notes]
  
  workshop.fpfkids.session:
    description: "Проведение занятия"
    events:
      session_complete: workshop.fpfkids.session  # Следующее занятие
      program_complete: common.mode_select
      pause: common.mode_select
    allow_global: [consultation, notes]
  
  # ==========================================
  # МАСТЕРСКАЯ: ЗАДАЧНИК
  # ==========================================
  
  workshop.practice.topic_select:
    description: "Выбор темы для практики"
    events:
      topic_selected: workshop.practice.problem
      random: workshop.practice.problem  # Случайная задача
    allow_global: [consultation, notes]
  
  workshop.practice.problem:
    description: "Показ задачи"
    events:
      problem_shown: workshop.practice.solution
      skip: workshop.practice.problem    # Следующая задача
    allow_global: [consultation, notes]
  
  workshop.practice.solution:
    description: "Проверка решения"
    events:
      correct: workshop.practice.problem  # Следующая задача
      incorrect: _same                    # Повторная попытка
      show_answer: workshop.practice.problem
      done: common.mode_select
    allow_global: [consultation, notes]
  
  # ==========================================
  # ГЛОБАЛЬНЫЕ ПРОЦЕССЫ
  # ==========================================

  common.consultation:
    description: "Консультация (ответ на вопрос пользователя)"
    events:
      answered: _previous               # Возврат в предыдущий стейт
      followup: _same                   # Уточняющий вопрос
      done: _previous

  # ==========================================
  # ЛЕНТА (feed.*)
  # ==========================================

  feed.topics:
    description: "Выбор тем для Ленты"
    events:
      topics_selected: feed.digest
      skip: common.mode_select
    allow_global: [consultation, notes]

  feed.digest:
    description: "Показ дайджеста"
    events:
      digest_shown: _same               # Ждём фиксацию
      fixation_saved: _same             # Следующий дайджест
      change_topics: feed.topics
      done: common.mode_select
    allow_global: [consultation, notes]
  
  # ==========================================
  # УТИЛИТЫ
  # ==========================================
  
  utility.notes:
    description: "Заметочник"
    events:
      saved: _previous
      list_shown: _same
      error: _previous
  
  utility.export:
    description: "Экспорт данных"
    events:
      exported: _previous
      error: _previous


# ==========================================
# ГЛОБАЛЬНЫЕ СОБЫТИЯ
# ==========================================
# Эти события можно вызвать из любого стейта,
# если он указан в allow_global

global_events:
  consultation:
    trigger: "?"                        # Сообщение начинается с ?
    target: common.consultation

  notes:
    trigger: "/note"                    # Команда /note
    target: utility.notes
  
  export:
    trigger: "/export"
    target: utility.export
  
  help:
    trigger: "/help"
    target: common.help
  
  mode:
    trigger: "/mode"
    target: common.mode_select
```

## 3.3. StateMachine — движок переходов

```python
# core/machine.py
import yaml
from typing import Optional
from pathlib import Path
from aiogram.types import Message

from states.base import BaseState
from core.storage import StateStorage


class InvalidTransition(Exception):
    """Недопустимый переход между стейтами"""
    pass


class StateMachine:
    """
    Движок State Machine.
    Управляет переходами между стейтами на основе таблицы переходов.
    """
    
    def __init__(self, transitions_path: str, storage: StateStorage):
        """
        Args:
            transitions_path: Путь к transitions.yaml
            storage: Хранилище состояний (БД)
        """
        self.storage = storage
        self.states: dict[str, BaseState] = {}
        self._load_transitions(transitions_path)
    
    def _load_transitions(self, path: str):
        """Загружает таблицу переходов из YAML"""
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.transitions = config.get("states", {})
        self.global_events = config.get("global_events", {})
    
    def register(self, state: BaseState):
        """Регистрирует стейт в машине"""
        self.states[state.name] = state
    
    def register_all(self, states: list[BaseState]):
        """Регистрирует список стейтов"""
        for state in states:
            self.register(state)
    
    async def get_user_state(self, user) -> BaseState:
        """Возвращает текущий стейт пользователя"""
        state_name = user.current_state or "common.start"
        if state_name not in self.states:
            raise ValueError(f"Unknown state: {state_name}")
        return self.states[state_name]
    
    async def handle_message(self, user, message: Message) -> None:
        """
        Главный метод — обрабатывает входящее сообщение.
        
        1. Проверяет глобальные события
        2. Передаёт сообщение текущему стейту
        3. Выполняет переход если нужно
        """
        # Проверяем глобальные события
        global_event = self._check_global_events(user, message)
        if global_event:
            await self.transition(user, global_event, message)
            return
        
        # Получаем текущий стейт
        state = await self.get_user_state(user)
        
        # Обрабатываем сообщение
        event = await state.handle(user, message)
        
        # Если стейт вернул событие — выполняем переход
        if event:
            await self.transition(user, event, message)
    
    def _check_global_events(self, user, message: Message) -> Optional[str]:
        """Проверяет, не является ли сообщение глобальной командой"""
        text = message.text or ""
        current_state = user.current_state or "common.start"
        
        # Получаем разрешённые глобальные события для текущего стейта
        state_config = self.transitions.get(current_state, {})
        allowed = state_config.get("allow_global", [])
        
        for event_name, config in self.global_events.items():
            trigger = config.get("trigger", "")
            if text.startswith(trigger) and event_name in allowed:
                return event_name
        
        return None
    
    async def transition(self, user, event: str, message: Message = None) -> None:
        """
        Выполняет переход между стейтами.
        
        Args:
            user: Объект пользователя
            event: Событие, вызвавшее переход
            message: Исходное сообщение (для контекста)
        """
        current_state_name = user.current_state or "common.start"
        
        # Определяем следующий стейт
        next_state_name = self._get_next_state(current_state_name, event)
        
        if not next_state_name:
            raise InvalidTransition(
                f"No transition for event '{event}' from state '{current_state_name}'"
            )
        
        # Специальные переходы
        if next_state_name == "_previous":
            next_state_name = user.previous_state or "common.mode_select"
        elif next_state_name == "_same":
            return  # Остаёмся в текущем стейте
        
        # Получаем объекты стейтов
        current_state = self.states.get(current_state_name)
        next_state = self.states.get(next_state_name)
        
        if not next_state:
            raise ValueError(f"Unknown state: {next_state_name}")
        
        # Выполняем выход из текущего стейта
        context = {}
        if current_state:
            context = await current_state.exit(user) or {}
        
        # Сохраняем предыдущий стейт
        user.previous_state = current_state_name
        user.current_state = next_state_name
        
        # Сохраняем в БД
        await self.storage.save_state(user)
        
        # Выполняем вход в новый стейт
        await next_state.enter(user, context)
    
    def _get_next_state(self, current: str, event: str) -> Optional[str]:
        """Определяет следующий стейт по таблице переходов"""
        # Сначала проверяем глобальные события
        if event in self.global_events:
            return self.global_events[event].get("target")
        
        # Затем проверяем переходы текущего стейта
        state_config = self.transitions.get(current, {})
        events = state_config.get("events", {})
        return events.get(event)
    
    async def force_state(self, user, state_name: str, context: dict = None):
        """
        Принудительно переводит пользователя в указанный стейт.
        Используется для административных целей и восстановления.
        """
        if state_name not in self.states:
            raise ValueError(f"Unknown state: {state_name}")
        
        user.previous_state = user.current_state
        user.current_state = state_name
        await self.storage.save_state(user)
        await self.states[state_name].enter(user, context or {})
```

## 3.4. StateStorage — хранение состояния

```python
# core/storage.py
from typing import Optional


class StateStorage:
    """
    Хранение состояния пользователей.
    Сейчас: PostgreSQL
    Потом: может быть заменено на DigitalTwin/MCP
    """
    
    def __init__(self, db_repo):
        self.db = db_repo
    
    async def load_state(self, telegram_id: int) -> Optional[dict]:
        """Загружает состояние пользователя"""
        user = await self.db.get_user_by_telegram_id(telegram_id)
        if not user:
            return None
        return {
            "current_state": user.current_state,
            "previous_state": user.previous_state,
            "state_context": user.state_context or {}
        }
    
    async def save_state(self, user) -> None:
        """Сохраняет состояние пользователя"""
        await self.db.update_user(user.id, {
            "current_state": user.current_state,
            "previous_state": user.previous_state,
            "state_context": user.state_context
        })
    
    async def get_users_in_state(self, state_name: str) -> list:
        """Возвращает всех пользователей в указанном стейте"""
        return await self.db.find_users_by_state(state_name)
```

## 3.5. Модель пользователя

```python
# db/models/user.py
from sqlalchemy import Column, Integer, String, JSON, DateTime
from sqlalchemy.sql import func
from db.base import Base


class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    
    # Профиль
    name = Column(String(255))
    language = Column(String(10), default="ru")
    timezone = Column(String(50), default="Europe/Moscow")
    
    # State Machine
    current_state = Column(String(100), default="common.start", index=True)
    previous_state = Column(String(100), nullable=True)
    state_context = Column(JSON, default=dict)  # Дополнительные данные стейта
    
    # Прогресс (для мастерских)
    marathon_day = Column(Integer, default=1)
    marathon_started_at = Column(DateTime, nullable=True)
    difficulty_level = Column(Integer, default=1)
    
    # Метаданные
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
```

## 3.6. Пример стейта: MarathonQuestion

```python
# states/workshops/marathon/question.py
from typing import Optional
from aiogram.types import Message

from states.base import BaseState


class MarathonQuestionState(BaseState):
    """
    Стейт: Вопрос на понимание урока марафона.

    Входит после показа урока (workshop.marathon.lesson).
    Выходит в бонус или задание после правильного ответа.
    """

    name = "workshop.marathon.question"
    display_name = {
        "ru": "Вопрос марафона",
        "en": "Marathon Question"
    }
    allow_global = ["consultation", "notes"]
    
    async def enter(self, user, context: dict = None):
        """Показываем вопрос"""
        # Получаем вопрос для текущего дня
        question = await self._get_question(user)
        
        text = self.t("marathon.question_prompt", user) + "\n\n"
        text += f"❓ {question['text']}"
        
        # Кнопки
        buttons = [
            [self.t("marathon.skip_button", user)],
            [self.t("marathon.hint_button", user)]
        ]
        
        await self.send_with_keyboard(user, text, buttons)
    
    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем ответ"""
        text = message.text or ""
        
        # Проверка на кнопки
        if text == self.t("marathon.skip_button", user):
            await self.send(user, self.t("marathon.skipped", user))
            return "skip"
        
        if text == self.t("marathon.hint_button", user):
            hint = await self._get_hint(user)
            await self.send(user, f"💡 {hint}")
            return "hint"  # Остаёмся в стейте (_same)
        
        # Проверяем ответ
        is_correct = await self._check_answer(user, text)
        
        if is_correct:
            await self.send(user, self.t("marathon.correct", user))
            return "correct"
        else:
            attempts = user.state_context.get("attempts", 0) + 1
            user.state_context["attempts"] = attempts
            
            if attempts >= 3:
                await self.send(user, self.t("marathon.max_attempts", user))
                return "skip"
            
            await self.send(user, self.t("marathon.incorrect", user, attempts=3-attempts))
            return "incorrect"  # Остаёмся в стейте (_same)
    
    async def exit(self, user) -> dict:
        """Очищаем контекст при выходе"""
        attempts = user.state_context.pop("attempts", 0)
        return {"question_attempts": attempts}
    
    # === Приватные методы ===
    
    async def _get_question(self, user) -> dict:
        """Загружает вопрос для текущего дня"""
        day = user.marathon_day
        # Загрузка из content/workshops/marathon/dayXX/question.yaml
        # ...
        return {"text": "Вопрос...", "answer_keywords": ["ключ1", "ключ2"]}
    
    async def _get_hint(self, user) -> str:
        """Генерирует подсказку через LLM"""
        question = await self._get_question(user)
        # Вызов Claude для генерации подсказки
        # ...
        return "Подсказка..."
    
    async def _check_answer(self, user, answer: str) -> bool:
        """Проверяет ответ через LLM"""
        question = await self._get_question(user)
        # Вызов Claude для проверки ответа
        # ...
        return True
```

## 3.7. Пример стейта: Консультация

```python
# states/common/consultation.py
from typing import Optional
from aiogram.types import Message

from states.base import BaseState
from core.knowledge.router import KnowledgeRouter


class ConsultationState(BaseState):
    """
    Стейт: Консультация (глобальный процесс).

    Вход: из любого стейта по команде "?"
    Выход: возврат в предыдущий стейт
    """

    name = "common.consultation"
    display_name = {
        "ru": "Консультация",
        "en": "Consultation"
    }
    allow_global = []  # Из консультации нельзя вызвать консультацию

    def __init__(self, *args, knowledge_router: KnowledgeRouter, **kwargs):
        super().__init__(*args, **kwargs)
        self.knowledge_router = knowledge_router

    async def enter(self, user, context: dict = None):
        """Показываем приглашение"""
        # Если пришли с вопросом (? текст) — сразу отвечаем
        initial_question = context.get("question") if context else None

        if initial_question:
            await self._answer_question(user, initial_question)
        else:
            await self.send(user, self.t("consultation.prompt", user))

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем вопрос"""
        text = message.text or ""

        # Команда выхода
        if text.lower() in ["выход", "exit", "done", "/done"]:
            await self.send(user, self.t("consultation.goodbye", user))
            return "done"

        # Отвечаем на вопрос
        await self._answer_question(user, text)

        # Спрашиваем, есть ли ещё вопросы
        await self.send(user, self.t("consultation.followup_prompt", user))
        return "followup"  # Остаёмся в стейте

    async def _answer_question(self, user, question: str):
        """Отвечает на вопрос с использованием баз знаний"""
        # Показываем "думаю..."
        await self.send(user, self.t("consultation.thinking", user))
        
        # Получаем контекст из баз знаний
        context = await self.knowledge_router.get_context(question)
        
        # Генерируем ответ через LLM
        answer = await self.llm.generate_answer(
            question=question,
            context=context,
            user_profile={
                "name": user.name,
                "language": user.language,
                "level": user.difficulty_level
            }
        )
        
        await self.send(user, answer)
```

## 3.8. Роутер баз знаний

```python
# core/knowledge/router.py
from pathlib import Path
from core.knowledge.loader import KnowledgeLoader


class KnowledgeRouter:
    """
    Определяет, в какой базе знаний искать ответ.
    Правило: сначала ищем в базах мастерских по теме.
    """
    
    TOPIC_MAPPING = {
        # Экзокортекс
        "экзокортекс": "exocortex",
        "заметки": "exocortex",
        "obsidian": "exocortex",
        "notion": "exocortex",
        "второй мозг": "exocortex",
        "pkm": "exocortex",
        
        # FPFkids
        "ребёнок": "fpfkids",
        "ребенок": "fpfkids",
        "дети": "fpfkids",
        "обучение детей": "fpfkids",
        "родител": "fpfkids",
        
        # Задачник
        "задача": "practice",
        "практика": "practice",
        "упражнение": "practice",
        
        # Марафон
        "марафон": "marathon",
        "урок": "marathon",
        "14 дней": "marathon",
    }
    
    def __init__(self, loader: KnowledgeLoader):
        self.loader = loader
    
    def route(self, query: str) -> list[str]:
        """Возвращает список мастерских для поиска (в порядке приоритета)"""
        query_lower = query.lower()
        workshops = []
        
        for keyword, workshop in self.TOPIC_MAPPING.items():
            if keyword in query_lower and workshop not in workshops:
                workshops.append(workshop)
        
        return workshops
    
    async def get_context(self, query: str) -> str:
        """Собирает контекст из релевантных баз знаний"""
        workshops = self.route(query)
        context_parts = []
        
        # Сначала базы мастерских
        for workshop in workshops:
            kb = await self.loader.load(workshop)
            if kb:
                context_parts.append(f"### База знаний: {workshop}\n\n{kb}")
        
        # Потом общая база
        base_kb = await self.loader.load_base()
        if base_kb:
            context_parts.append(f"### Общая база знаний\n\n{base_kb}")
        
        return "\n\n---\n\n".join(context_parts)
```

---

# Часть 4. Feature Flags

```yaml
# config/features.yaml

# Флаги миграции
migration:
  use_state_machine: false      # Главный переключатель
  use_new_marathon: false
  use_new_feed: false
  use_new_i18n: false

# Активные мастерские
workshops:
  marathon:
    enabled: true
    visible: true
  exocortex:
    enabled: false
    visible: false
  fpfkids:
    enabled: false
    visible: false
  practice:
    enabled: false
    visible: false

# Глобальные процессы
global_processes:
  consultation:
    enabled: true

# Лента
feed:
  enabled: true

# Активные утилиты
utilities:
  notes:
    enabled: false
  export:
    enabled: false
```

```python
# config/__init__.py
import os
import yaml
from pathlib import Path


class FeatureFlags:
    def __init__(self):
        config_path = Path(__file__).parent / "features.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
    
    def is_enabled(self, path: str) -> bool:
        """
        Проверяет флаг. Env переменные имеют приоритет.
        
        Примеры:
            flags.is_enabled("migration.use_state_machine")
            flags.is_enabled("workshops.exocortex.enabled")
        """
        # Проверяем env override
        env_name = path.upper().replace(".", "_")
        env_value = os.getenv(env_name)
        if env_value is not None:
            return env_value.lower() in ("true", "1", "yes")
        
        # Читаем из конфига
        keys = path.split(".")
        value = self._config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return False
        return bool(value)


flags = FeatureFlags()
```

---

# Часть 5. Система локализации

```yaml
# i18n/ru/common.yaml
greeting: "Привет, {name}!"
help: "Справка по командам"
error: "Произошла ошибка. Попробуйте позже."
cancel: "Отмена"
back: "Назад"
next: "Далее"
done: "Готово"
```

```yaml
# i18n/ru/states.yaml
marathon:
  question_prompt: "Ответьте на вопрос по уроку:"
  skip_button: "⏭ Пропустить"
  hint_button: "💡 Подсказка"
  correct: "✅ Верно! Отличная работа."
  incorrect: "🔄 Не совсем. Осталось попыток: {attempts}"
  max_attempts: "Давайте перейдём к заданию. Вы сможете вернуться к этому позже."
  skipped: "Хорошо, переходим к заданию."

consultation:
  prompt: "Задайте ваш вопрос:"
  thinking: "🤔 Думаю..."
  followup_prompt: "Есть ещё вопросы? Напишите «выход» чтобы вернуться."
  goodbye: "Хорошо! Возвращаемся к предыдущему занятию."
```

```python
# i18n/loader.py
import yaml
from pathlib import Path


class I18n:
    def __init__(self):
        self._translations: dict[str, dict] = {}
        self._load_all()
    
    def _load_all(self):
        i18n_dir = Path(__file__).parent
        
        for lang_dir in i18n_dir.iterdir():
            if lang_dir.is_dir() and not lang_dir.name.startswith("_"):
                lang = lang_dir.name
                self._translations[lang] = {}
                
                for yaml_file in lang_dir.glob("*.yaml"):
                    namespace = yaml_file.stem
                    with open(yaml_file, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                        self._flatten(data, namespace, self._translations[lang])
    
    def _flatten(self, data: dict, prefix: str, result: dict):
        """Превращает вложенный dict в плоский с точками"""
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                self._flatten(value, full_key, result)
            else:
                result[full_key] = value
    
    def t(self, key: str, lang: str = "ru", **kwargs) -> str:
        """Получает перевод"""
        if lang not in self._translations:
            lang = "ru"
        
        text = self._translations[lang].get(key, key)
        
        if kwargs:
            try:
                return text.format(**kwargs)
            except KeyError:
                return text
        
        return text


i18n = I18n()

def t(key: str, lang: str = "ru", **kwargs) -> str:
    return i18n.t(key, lang, **kwargs)
```

---

# Часть 6. Точка входа и Middleware

```python
# bot.py
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import Message

from config import flags
from config.settings import settings
from db import create_db_pool, UserRepository
from core.machine import StateMachine
from core.storage import StateStorage
from core.middleware import StateMiddleware
from states.registry import register_all_states
from i18n import i18n
from integrations.llm.claude import ClaudeClient


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    # Инициализация
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    
    # База данных
    db_pool = await create_db_pool(settings.DATABASE_URL)
    user_repo = UserRepository(db_pool)
    
    # LLM
    llm = ClaudeClient(settings.ANTHROPIC_API_KEY)
    
    if flags.is_enabled("migration.use_state_machine"):
        logger.info("Using State Machine architecture")
        
        # State Machine
        storage = StateStorage(user_repo)
        machine = StateMachine(
            transitions_path="config/transitions.yaml",
            storage=storage
        )
        
        # Регистрируем все стейты
        register_all_states(machine, bot, user_repo, llm, i18n)
        
        # Middleware для загрузки пользователя
        dp.message.middleware(StateMiddleware(user_repo, machine))
        
        # Единый handler для всех сообщений
        @dp.message()
        async def handle_all(message: Message, user, machine: StateMachine):
            await machine.handle_message(user, message)
    
    else:
        logger.info("Using OLD architecture")
        # Старый код остаётся здесь
        from engines.marathon import setup_marathon_handlers
        from engines.feed import setup_feed_handlers
        setup_marathon_handlers(dp)
        setup_feed_handlers(dp)
    
    # Запуск
    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
```

```python
# core/middleware.py
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message


class StateMiddleware(BaseMiddleware):
    """
    Middleware для загрузки пользователя и передачи StateMachine.
    """
    
    def __init__(self, user_repo, machine):
        self.user_repo = user_repo
        self.machine = machine
    
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        telegram_id = event.from_user.id
        
        # Загружаем или создаём пользователя
        user = await self.user_repo.get_or_create(telegram_id)
        
        # Добавляем в контекст
        data["user"] = user
        data["machine"] = self.machine
        
        return await handler(event, data)
```

```python
# states/registry.py
from core.machine import StateMachine

# Импортируем все стейты
from states.common.start import StartState
from states.common.error import ErrorState
from states.common.mode_select import ModeSelectState
from states.common.consultation import ConsultationState

from states.workshops.marathon.lesson import MarathonLessonState
from states.workshops.marathon.question import MarathonQuestionState
from states.workshops.marathon.bonus import MarathonBonusState
from states.workshops.marathon.task import MarathonTaskState

from states.feed.topics import FeedTopicsState
from states.feed.digest import FeedDigestState

from states.utilities.notes import NotesState


def register_all_states(machine: StateMachine, bot, db, llm, i18n):
    """Регистрирует все стейты в машине"""

    # Общие аргументы для всех стейтов
    args = (bot, db, llm, i18n)

    states = [
        # Common
        StartState(*args),
        ErrorState(*args),
        ModeSelectState(*args),
        ConsultationState(*args),

        # Marathon
        MarathonLessonState(*args),
        MarathonQuestionState(*args),
        MarathonBonusState(*args),
        MarathonTaskState(*args),

        # Feed
        FeedTopicsState(*args),
        FeedDigestState(*args),

        # Utilities
        NotesState(*args),
    ]

    machine.register_all(states)
```

---

# Часть 7. Пошаговый план миграции

## Принцип: Strangler Fig + Feature Flags

Новый код растёт рядом со старым. В любой момент можно откатиться через feature flag.

---

## Неделя 1: Инфраструктура

**Цель:** Создать скелет State Machine без изменения работающего кода.

**Задачи:**

1. Создать структуру папок:
```bash
mkdir -p states/common states/workshops/marathon states/feed states/utilities
mkdir -p core/knowledge
mkdir -p content/workshops/marathon content/feed content/knowledge
mkdir -p i18n/ru i18n/en i18n/es
mkdir -p config
```

2. Создать базовые файлы:
   - `states/base.py` — BaseState
   - `core/machine.py` — StateMachine
   - `core/storage.py` — StateStorage
   - `core/middleware.py` — StateMiddleware
   - `config/transitions.yaml` — таблица переходов (только common стейты)
   - `config/features.yaml` — feature flags
   - `config/__init__.py` — FeatureFlags class
   - `i18n/loader.py` — I18n class
   - `states/registry.py` — регистрация стейтов

3. Создать заглушки стейтов:
   - `states/common/start.py`
   - `states/common/error.py`
   - `states/common/mode_select.py`

4. Добавить переключатель в `bot.py`

**Проверка:** 
- С `USE_STATE_MACHINE=false` — бот работает как раньше
- С `USE_STATE_MACHINE=true` — бот запускается, можно пройти start → mode_select

---

## Неделя 2: Marathon стейты

**Цель:** Перенести логику Марафона в стейты.

**Задачи:**

1. Создать стейты:
   - `states/workshops/marathon/lesson.py`
   - `states/workshops/marathon/question.py`
   - `states/workshops/marathon/bonus.py`
   - `states/workshops/marathon/task.py`

2. Перенести контент в `content/workshops/marathon/`

3. Добавить переходы в `transitions.yaml`

4. Обновить `states/registry.py`

5. Добавить локализацию в `i18n/ru/states.yaml`

**Проверка:**
- Пользователь может пройти полный цикл: lesson → question → bonus → task → lesson

---

## Неделя 3: Консультация и глобальные команды

**Цель:** Реализовать процесс консультации и систему глобальных переходов.

**Задачи:**

1. Создать `core/knowledge/loader.py` и `core/knowledge/router.py`

2. Создать `states/common/consultation.py`

3. Настроить глобальные события в `transitions.yaml`

4. Проверить переход `?` → common.consultation → _previous

**Проверка:**
- Из любого стейта можно вызвать консультацию через `?`
- После ответа возвращаемся в предыдущий стейт

---

## Неделя 4: Feed стейты

**Цель:** Перенести Ленту в стейты.

**Задачи:**

1. Создать стейты:
   - `states/feed/topics.py`
   - `states/feed/digest.py`

2. Перенести контент тем в `content/feed/`

3. Настроить переходы в `transitions.yaml`

**Проверка:**
- Пользователь может выбрать темы и получить дайджест

---

## Неделя 5: Локализация

**Цель:** Полный перенос на систему i18n.

**Задачи:**

1. Перенести все строки из `locales.py` в `i18n/ru/`

2. Создать `i18n/en/` с переводами

3. Заменить все вызовы `get_message()` на `t()`

4. Удалить `locales.py`

**Проверка:**
- Бот работает на русском и английском

---

## Неделя 6: Утилиты

**Цель:** Реализовать заметочник и экспорт.

**Задачи:**

1. Создать `states/utilities/notes.py`

2. Создать `states/utilities/export.py`

3. Создать `integrations/export/` с адаптерами

4. Настроить глобальные команды `/note`, `/export`

**Проверка:**
- `/note текст` сохраняет заметку
- `/export` предлагает выбор формата

---

## Неделя 7: Тест ступени

**Цель:** Реализовать диагностику.

**Задачи:**

1. Создать стейты:
   - `states/assessment/test.py`
   - `states/assessment/result.py`

2. Создать `content/assessment/entry_test.yaml`

3. Добавить команду `/test`

**Проверка:**
- Пользователь может пройти тест и получить результат

---

## Неделя 8: Очистка и стабилизация

**Цель:** Удалить старый код, стабилизировать.

**Задачи:**

1. Удалить `engines/` (старый код)

2. Удалить `locales.py`

3. Обновить документацию

4. Полное тестирование всех сценариев

5. Установить `use_state_machine: true` по умолчанию

---

## Неделя 9+: Новые мастерские

**После стабилизации:**

1. `workshop.exocortex.*` — Экзокортекс
2. `workshop.fpfkids.*` — FPFkids
3. `workshop.practice.*` — Задачник

Для каждой мастерской:
- Создать стейты
- Добавить контент
- Добавить переходы
- Добавить базу знаний для консультанта

---

# Часть 8. Чеклист для каждого стейта

При создании нового стейта:

- [ ] Файл в правильной папке (`states/category/name.py`)
- [ ] Класс наследует `BaseState`
- [ ] Установлен `name` в формате `category.subcategory.name`
- [ ] Установлен `display_name` для отладки
- [ ] Установлен `allow_global` (какие глобальные команды доступны)
- [ ] Реализован `enter()` — что показываем при входе
- [ ] Реализован `handle()` — как обрабатываем сообщения
- [ ] Реализован `exit()` — что передаём следующему стейту
- [ ] Добавлены переходы в `config/transitions.yaml`
- [ ] Добавлены строки в `i18n/*/states.yaml`
- [ ] Зарегистрирован в `states/registry.py`
- [ ] Написаны тесты

---

# Часть 9. Подготовка к будущим интеграциям

## Цифровой двойник (DigitalTwin)

State Machine идеально подготовлен для ЦД:

```python
# core/storage.py — в будущем заменяем реализацию

class StateStorage:
    async def load_state(self, telegram_id: int):
        # Сейчас: PostgreSQL
        # return await self.db.get_user(telegram_id)
        
        # Потом: DigitalTwin через MCP
        # return await self.digital_twin.load(telegram_id)
        pass
    
    async def save_state(self, user):
        # Сейчас: PostgreSQL
        # await self.db.update_user(user)
        
        # Потом: DigitalTwin.sync()
        # await self.digital_twin.sync(user)
        pass
```

## Ory (Identity)

```python
# core/machine.py — добавляем проверку прав

async def transition(self, user, event: str, message: Message = None):
    next_state = self._get_next_state(user.current_state, event)
    
    # Проверка прав (когда появится Ory)
    # if not await self.ory.check_permission(user.identity_id, next_state):
    #     raise PermissionDenied()
    
    # ... остальная логика
```

---

# Часть 10. Команды для Claude Code

## Начало работы

```
Прочитай MIGRATION_PLAN_V2.md полностью.
Начни с Недели 1: создай структуру папок и базовые файлы.
После каждого шага проверяй, что бот запускается.
```

## При создании стейта

```
Создай стейт [название] по шаблону из Части 3.6.
Используй BaseState из states/base.py.
Добавь переходы в config/transitions.yaml.
Добавь локализацию в i18n/ru/states.yaml.
Зарегистрируй в states/registry.py.
```

## При проблемах

```
Если бот не запускается с USE_STATE_MACHINE=true,
установи USE_STATE_MACHINE=false и продолжай работу.
Запиши проблему в TODO.md.
```

---

# Резюме

1. **Архитектура:** State Machine — один файл = один стейт
2. **Категории:** Общие (common), Мастерские (workshop), Лента (feed), Утилиты (utility)
3. **Таблица переходов:** `config/transitions.yaml` — вся логика в одном месте
4. **Консультация едина:** Глобальный процесс `common.consultation` маршрутизирует вопросы к базам знаний
5. **Именование:** Стейты называются по **процессам** (consultation), а не по агентам (consultant)
6. **Миграция:** Feature flags, Strangler Fig, ~8-9 недель
7. **Будущее:** Готовность к ЦД и Ory через абстракцию StateStorage
