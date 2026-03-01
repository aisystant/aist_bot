"""
Тесты: инъекция прогресса пользователя в контекст консультации.

Покрывает изменения РП #5:
- collect_user_progress() — Context Pipeline collector
- _user_to_dict() — marathon + activity fields в intern dict
- _build_user_profile() — complexity_level не выводится
- assemble_context() — progress_section в результате
- SUBSCRIPTION_LAUNCH_DATE — paywall отложен на 2099
- Tier prompts — {progress_section} placeholder

Запуск: python3 tests/test_user_progress.py
Совместимость: Python 3.9+ (не зависит от aiogram)
"""

import sys
import os
import asyncio
import importlib
import importlib.util
from datetime import date, datetime
from unittest.mock import MagicMock

# Добавляем путь к проекту
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# =========================================================================
# Изолированный импорт: загрузка файла напрямую без parent __init__.py
# =========================================================================

def _mock_modules():
    """Мокаем тяжёлые зависимости (aiogram, asyncpg, aiohttp)."""
    for mod_name in [
        'aiogram', 'aiogram.types', 'aiogram.enums', 'aiogram.filters',
        'aiogram.fsm', 'aiogram.fsm.context', 'aiogram.fsm.state',
        'aiogram.fsm.storage', 'aiogram.fsm.storage.memory',
        'aiogram.utils', 'aiogram.utils.keyboard',
        'aiogram.dispatcher', 'aiogram.dispatcher.router',
        'aiogram.methods',
        'aiohttp', 'aiohttp.web',
        'asyncpg',
    ]:
        if mod_name not in sys.modules:
            mock = MagicMock()
            if mod_name == 'aiogram.types':
                mock.Message = MagicMock
                mock.CallbackQuery = MagicMock
                mock.InlineKeyboardMarkup = MagicMock
                mock.InlineKeyboardButton = MagicMock
            if mod_name == 'aiogram.enums':
                mock.ChatAction = MagicMock()
                mock.ChatAction.TYPING = 'typing'
            sys.modules[mod_name] = mock


def _load_module(name: str, filepath: str):
    """Загрузить Python-модуль напрямую по пути, без parent __init__.py."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _setup_imports():
    """Настроить изолированные импорты для тестов."""
    _mock_modules()

    import types

    # engines (пакет-заглушка)
    engines_pkg = types.ModuleType('engines')
    engines_pkg.__path__ = [os.path.join(PROJECT_ROOT, 'engines')]
    sys.modules['engines'] = engines_pkg

    # engines.shared (пакет-заглушка)
    shared_pkg = types.ModuleType('engines.shared')
    shared_pkg.__path__ = [os.path.join(PROJECT_ROOT, 'engines', 'shared')]
    sys.modules['engines.shared'] = shared_pkg

    # Мокаем engines.shared подмодули, которые тянут тяжёлые зависимости
    for submod in ['retrieval', 'context', 'structured_lookup', 'personal_detector',
                   'consultation_tools']:
        full = f'engines.shared.{submod}'
        if full not in sys.modules:
            sys.modules[full] = MagicMock()

    # Мокаем пакеты, которые тянут asyncpg/db
    for pkg in ['db', 'db.queries', 'db.queries.qa', 'db.queries.users',
                'db.queries.feed', 'db.queries.subscription', 'db.models',
                'clients', 'clients.claude', 'clients.mcp_knowledge',
                'clients.digital_twin', 'clients.github_oauth',
                'core', 'core.intent', 'core.registry', 'core.self_knowledge',
                'core.access', 'core.feedback_triage',
                'i18n', 'helpers', 'helpers.message_split',
                'config.conversion']:
        if pkg not in sys.modules:
            m = MagicMock()
            if pkg == 'i18n':
                m.t = lambda key, lang='ru': key
            sys.modules[pkg] = m

    # states (пакет-заглушка)
    states_pkg = types.ModuleType('states')
    states_pkg.__path__ = [os.path.join(PROJECT_ROOT, 'states')]
    sys.modules['states'] = states_pkg

    states_base = MagicMock()
    states_base.BaseState = type('BaseState', (), {
        'send': lambda self, *a, **kw: None,
        'bot': MagicMock(),
    })
    sys.modules['states.base'] = states_base

    states_common = types.ModuleType('states.common')
    states_common.__path__ = [os.path.join(PROJECT_ROOT, 'states', 'common')]
    sys.modules['states.common'] = states_common


_setup_imports()

# Загружаем целевые модули
_context_pipeline = _load_module(
    'engines.shared.context_pipeline',
    os.path.join(PROJECT_ROOT, 'engines', 'shared', 'context_pipeline.py'),
)
_question_handler = _load_module(
    'engines.shared.question_handler',
    os.path.join(PROJECT_ROOT, 'engines', 'shared', 'question_handler.py'),
)
_consultation = _load_module(
    'states.common.consultation',
    os.path.join(PROJECT_ROOT, 'states', 'common', 'consultation.py'),
)


def _run(coro):
    """Запуск async функции в sync-тесте."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =========================================================================
# 1. collect_user_progress — пустой пользователь
# =========================================================================

def test_progress_brand_new_user_empty():
    """Совсем новый пользователь (нет данных) → пустая строка."""
    key, value = _run(_context_pipeline.collect_user_progress(intern={}, lang='ru'))
    assert key == "progress_section"
    assert value == "", f"Ожидали пустую строку, получили: {value!r}"
    print("✅ brand new user → пустая строка")


# =========================================================================
# 1b. collect_user_progress — пользователь с активностью, без марафона
# =========================================================================

def test_progress_activity_without_marathon():
    """Есть активность, но marathon='not_started' → показать активность."""
    intern = {
        'marathon_status': 'not_started',
        'active_days_total': 12,
        'active_days_streak': 3,
        'longest_streak': 7,
        'mode': 'feed',
        'created_at': datetime(2026, 1, 15),
    }
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert key == "progress_section"
    assert "ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ" in value
    assert "Всего активных дней: 12" in value
    assert "Текущая серия: 3" in value
    assert "Рекорд серии: 7" in value
    assert "Режим: Лента" in value
    assert "2026-01-15" in value
    # Марафон НЕ должен упоминаться
    assert "Марафон" not in value
    print("✅ activity без марафона → показывает активность")


def test_progress_mode_shown():
    """Режим отображается."""
    intern = {'mode': 'marathon', 'active_days_total': 1}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Режим: Марафон" in value
    print("✅ mode → отображается")


def test_progress_mode_both():
    """Режим 'both' → 'Марафон + Лента'."""
    intern = {'mode': 'both', 'active_days_total': 1}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Марафон + Лента" in value
    print("✅ mode='both' → 'Марафон + Лента'")


# =========================================================================
# 1c. collect_user_progress — марафон
# =========================================================================

def test_progress_active_marathon():
    """marathon_status='active' → полный прогресс с марафоном."""
    intern = {
        'marathon_status': 'active',
        'current_topic_index': 3,
        'completed_topics': [0, 1, 2],
        'active_days_streak': 5,
        'active_days_total': 10,
        'marathon_start_date': '2026-02-15',
        'mode': 'marathon',
    }
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert key == "progress_section"
    assert "ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ" in value
    assert "Марафон: Активен" in value
    assert "Текущая тема: #4" in value  # index 3 → #4
    assert "Пройдено тем: 3" in value
    assert "Текущая серия: 5" in value
    assert "Всего активных дней: 10" in value
    assert "Дата начала марафона: 2026-02-15" in value
    print("✅ active → полный прогресс")


def test_progress_paused():
    """marathon_status='paused' → 'На паузе'."""
    intern = {'marathon_status': 'paused', 'current_topic_index': 1, 'active_days_total': 3}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "На паузе" in value
    print("✅ paused → 'На паузе'")


def test_progress_completed():
    """marathon_status='completed' → 'Завершён'."""
    intern = {'marathon_status': 'completed', 'current_topic_index': 13, 'active_days_total': 14}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Завершён" in value
    print("✅ completed → 'Завершён'")


def test_progress_en_header():
    """lang='en' → USER PROGRESS header."""
    intern = {'active_days_total': 1, 'mode': 'feed'}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='en'))
    assert "USER PROGRESS" in value
    print("✅ en → USER PROGRESS header")


def test_progress_completed_topics_json_string():
    """completed_topics как JSON-строка (из БД) → корректно парсится."""
    intern = {
        'marathon_status': 'active',
        'current_topic_index': 5,
        'completed_topics': '[0, 1, 2, 3, 4]',
    }
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Пройдено тем: 5" in value
    print("✅ completed_topics JSON string → парсится")


def test_progress_zero_streak_hidden():
    """active_days_streak=0 → не показывать серию."""
    intern = {'active_days_total': 5, 'active_days_streak': 0, 'mode': 'feed'}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Текущая серия" not in value
    print("✅ streak=0 → скрыта")


def test_progress_longest_equals_streak_hidden():
    """longest_streak == active_days_streak → рекорд не дублируется."""
    intern = {'active_days_total': 5, 'active_days_streak': 5, 'longest_streak': 5}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Рекорд серии" not in value
    print("✅ longest==streak → рекорд скрыт")


def test_progress_feed_active():
    """feed_status='active' → показывает 'Лента: Активна'."""
    intern = {'feed_status': 'active', 'active_days_total': 3, 'mode': 'feed'}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "Лента: Активна" in value
    print("✅ feed_status='active' → 'Лента: Активна'")


def test_progress_created_at_string():
    """created_at как строка → показывается как есть."""
    intern = {'active_days_total': 1, 'created_at': '2026-01-01'}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "В боте с: 2026-01-01" in value
    print("✅ created_at string → показывается")


def test_progress_created_at_datetime():
    """created_at как datetime → форматируется в YYYY-MM-DD."""
    intern = {'active_days_total': 1, 'created_at': datetime(2026, 2, 10, 14, 30)}
    key, value = _run(_context_pipeline.collect_user_progress(intern=intern, lang='ru'))
    assert "В боте с: 2026-02-10" in value
    print("✅ created_at datetime → форматируется")


# =========================================================================
# 2. _user_to_dict — marathon + activity fields
# =========================================================================

def test_user_to_dict_all_progress_fields():
    """_user_to_dict() включает все progress fields."""
    ConsultationState = _consultation.ConsultationState

    class FakeUser:
        chat_id = 123
        name = "Test"
        language = "ru"
        mode = "marathon"
        occupation = "dev"
        completed_topics = [0, 1]
        current_topic_index = 2
        complexity_level = 1
        interests = ["системное мышление"]
        goals = "Научиться"
        assessment_state = None
        marathon_status = "active"
        marathon_start_date = "2026-02-01"
        active_days_streak = 7
        active_days_total = 20
        longest_streak = 10
        last_active_date = "2026-02-28"
        feed_status = "not_started"
        created_at = datetime(2026, 1, 1)
        onboarding_completed = True

    state = ConsultationState.__new__(ConsultationState)
    d = state._user_to_dict(FakeUser())

    assert d['marathon_status'] == 'active'
    assert d['marathon_start_date'] == '2026-02-01'
    assert d['active_days_streak'] == 7
    assert d['active_days_total'] == 20
    assert d['longest_streak'] == 10
    assert d['last_active_date'] == '2026-02-28'
    assert d['feed_status'] == 'not_started'
    assert d['created_at'] == datetime(2026, 1, 1)
    assert d['onboarding_completed'] is True
    print("✅ _user_to_dict() включает все progress fields")


def test_user_to_dict_defaults():
    """_user_to_dict() для user без progress attrs → корректные defaults."""
    ConsultationState = _consultation.ConsultationState

    class MinimalUser:
        chat_id = 456
        name = "Min"
        language = "en"
        mode = "feed"

    state = ConsultationState.__new__(ConsultationState)
    d = state._user_to_dict(MinimalUser())

    assert d['marathon_status'] == 'not_started'
    assert d['marathon_start_date'] is None
    assert d['active_days_streak'] == 0
    assert d['active_days_total'] == 0
    assert d['longest_streak'] == 0
    assert d['last_active_date'] is None
    assert d['feed_status'] == 'not_started'
    assert d['created_at'] is None
    assert d['onboarding_completed'] is False
    print("✅ _user_to_dict() defaults для progress fields")


def test_user_to_dict_passthrough():
    """_user_to_dict() для dict → возвращает как есть."""
    ConsultationState = _consultation.ConsultationState

    d = {'chat_id': 789, 'marathon_status': 'completed'}
    state = ConsultationState.__new__(ConsultationState)
    result = state._user_to_dict(d)
    assert result is d
    print("✅ _user_to_dict() dict passthrough")


# =========================================================================
# 3. _build_user_profile — без complexity_level
# =========================================================================

def test_profile_no_complexity_level():
    """_build_user_profile() НЕ содержит complexity_level / 'Уровень'."""
    build = _question_handler._build_user_profile
    intern = {
        'complexity_level': 3,
        'interests': ['системное мышление', 'стратегия'],
        'goals': 'Стать архитектором',
    }
    profile = build(intern, 'ru')
    assert "Уровень" not in profile, f"complexity_level просочился: {profile}"
    assert "complexity" not in profile.lower()
    assert "Интересы" in profile
    assert "Цели" in profile
    print("✅ _build_user_profile() без complexity_level")


def test_profile_no_study_duration():
    """_build_user_profile() НЕ содержит study_duration."""
    build = _question_handler._build_user_profile
    intern = {'study_duration': 5, 'interests': ['собранность']}
    profile = build(intern, 'ru')
    assert "study_duration" not in profile.lower()
    assert "Длительность" not in profile
    print("✅ _build_user_profile() без study_duration")


def test_profile_empty_for_new_user():
    """_build_user_profile() → пустая строка для нового пользователя."""
    build = _question_handler._build_user_profile
    assert build({}, 'ru') == ""
    print("✅ _build_user_profile() пустая для нового пользователя")


def test_profile_includes_assessment():
    """_build_user_profile() показывает assessment_state."""
    build = _question_handler._build_user_profile
    intern = {'assessment_state': 'chaos', 'goals': 'Разобраться'}
    profile = build(intern, 'ru')
    assert "Хаос" in profile
    assert "Цели" in profile
    print("✅ _build_user_profile() включает assessment_state")


def test_profile_includes_role():
    """_build_user_profile() показывает роль."""
    build = _question_handler._build_user_profile
    profile = build({'role': 'Инженер'}, 'ru')
    assert "Роль: Инженер" in profile
    print("✅ _build_user_profile() включает role")


def test_profile_en_header():
    """_build_user_profile() lang='en' → USER PROFILE header."""
    build = _question_handler._build_user_profile
    profile = build({'interests': ['AI']}, 'en')
    assert "USER PROFILE" in profile
    print("✅ _build_user_profile() en → USER PROFILE header")


# =========================================================================
# 4. TIER_PIPELINE — collect_user_progress в каждом тире
# =========================================================================

def test_tier_pipeline_includes_progress():
    """collect_user_progress есть в TIER_PIPELINE для всех тиров (1-4)."""
    TIER_PIPELINE = _context_pipeline.TIER_PIPELINE
    collect_fn = _context_pipeline.collect_user_progress
    for tier in [1, 2, 3, 4]:
        assert collect_fn in TIER_PIPELINE[tier], f"collect_user_progress отсутствует в T{tier}"
    print("✅ collect_user_progress в TIER_PIPELINE для T1-T4")


def test_tier_pipeline_tiers_defined():
    """TIER_PIPELINE определён для тиров 1-4."""
    TIER_PIPELINE = _context_pipeline.TIER_PIPELINE
    for tier in [1, 2, 3, 4]:
        assert tier in TIER_PIPELINE
        assert len(TIER_PIPELINE[tier]) >= 2
    print("✅ TIER_PIPELINE определён для T1-T4")


# =========================================================================
# 5. assemble_context — progress_section в результате
# =========================================================================

def test_assemble_context_has_progress_key():
    """assemble_context() возвращает dict с ключом 'progress_section'."""
    intern = {'language': 'ru'}
    sections = _run(_context_pipeline.assemble_context(tier=1, intern=intern, lang='ru'))
    assert 'progress_section' in sections
    print("✅ assemble_context() включает progress_section key")


def test_assemble_context_progress_populated():
    """assemble_context() → progress_section непустой для active user."""
    intern = {
        'marathon_status': 'active',
        'current_topic_index': 5,
        'completed_topics': [0, 1, 2, 3, 4],
        'active_days_streak': 3,
        'active_days_total': 10,
        'language': 'ru',
        'mode': 'marathon',
    }
    sections = _run(_context_pipeline.assemble_context(tier=1, intern=intern, lang='ru'))
    assert sections['progress_section'] != ""
    assert "Активен" in sections['progress_section']
    print("✅ assemble_context() progress populated для active user")


def test_assemble_context_progress_for_feed_user():
    """assemble_context() → progress_section непустой для feed user без марафона."""
    intern = {
        'marathon_status': 'not_started',
        'active_days_total': 5,
        'mode': 'feed',
        'feed_status': 'active',
        'language': 'ru',
    }
    sections = _run(_context_pipeline.assemble_context(tier=1, intern=intern, lang='ru'))
    assert sections['progress_section'] != "", "progress_section пустой для feed user с активностью"
    assert "Лента" in sections['progress_section']
    print("✅ assemble_context() progress для feed user без марафона")


def test_assemble_context_all_default_keys():
    """assemble_context() возвращает все стандартные ключи."""
    sections = _run(_context_pipeline.assemble_context(tier=1, intern={'language': 'ru'}, lang='ru'))
    expected_keys = {
        'user_profile', 'bot_section', 'standard_section',
        'personal_section', 'dynamic_sections', 'progress_section',
    }
    assert expected_keys.issubset(set(sections.keys())), (
        f"Отсутствуют ключи: {expected_keys - set(sections.keys())}"
    )
    print("✅ assemble_context() все стандартные ключи")


# =========================================================================
# 6. SUBSCRIPTION_LAUNCH_DATE — paywall отложен
# =========================================================================

def test_subscription_launch_date_in_future():
    """SUBSCRIPTION_LAUNCH_DATE в далёком будущем (paywall отложен)."""
    from config.settings import SUBSCRIPTION_LAUNCH_DATE
    assert SUBSCRIPTION_LAUNCH_DATE > date(2026, 12, 31), (
        f"SUBSCRIPTION_LAUNCH_DATE={SUBSCRIPTION_LAUNCH_DATE} слишком скоро! "
        "Stars = донаты, подписка через Aisystant."
    )
    print(f"✅ SUBSCRIPTION_LAUNCH_DATE = {SUBSCRIPTION_LAUNCH_DATE} (далеко в будущем)")


# =========================================================================
# 7. Tier prompts — {progress_section} placeholder
# =========================================================================

def test_tier_prompts_have_progress_placeholder():
    """Все tier prompts (t1, t2, t3) содержат {progress_section}."""
    prompts_dir = os.path.join(PROJECT_ROOT, 'config', 'prompts')
    for filename in ['t1_expert.md', 't2_mentor.md', 't3_cothinker.md']:
        path = os.path.join(prompts_dir, filename)
        with open(path, 'r') as f:
            content = f.read()
        assert '{progress_section}' in content, f"{filename} не содержит {{progress_section}}"
    print("✅ Все tier prompts содержат {progress_section}")


def test_tier_prompts_have_progress_rule():
    """Все tier prompts содержат правило про прогресс пользователя."""
    prompts_dir = os.path.join(PROJECT_ROOT, 'config', 'prompts')
    for filename in ['t1_expert.md', 't2_mentor.md', 't3_cothinker.md']:
        path = os.path.join(prompts_dir, filename)
        with open(path, 'r') as f:
            content = f.read()
        assert 'ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ' in content, (
            f"{filename} не содержит правило про ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ"
        )
    print("✅ Все tier prompts содержат правило про прогресс")


def test_tier_prompts_progress_after_user_profile():
    """{progress_section} идёт ПОСЛЕ {user_profile} в промптах."""
    prompts_dir = os.path.join(PROJECT_ROOT, 'config', 'prompts')
    for filename in ['t1_expert.md', 't2_mentor.md', 't3_cothinker.md']:
        path = os.path.join(prompts_dir, filename)
        with open(path, 'r') as f:
            content = f.read()
        idx_profile = content.index('{user_profile}')
        idx_progress = content.index('{progress_section}')
        assert idx_progress > idx_profile, (
            f"{filename}: progress_section ({idx_progress}) до user_profile ({idx_profile})"
        )
    print("✅ {progress_section} после {user_profile} во всех промптах")


# =========================================================================
# Runner
# =========================================================================

if __name__ == "__main__":
    print("\n🧪 Тесты: инъекция прогресса пользователя (РП #5)\n")
    print("=" * 60)

    tests = [
        # 1. collect_user_progress — пустой/новый
        test_progress_brand_new_user_empty,
        # 1b. collect_user_progress — активность без марафона
        test_progress_activity_without_marathon,
        test_progress_mode_shown,
        test_progress_mode_both,
        # 1c. collect_user_progress — марафон
        test_progress_active_marathon,
        test_progress_paused,
        test_progress_completed,
        test_progress_en_header,
        test_progress_completed_topics_json_string,
        test_progress_zero_streak_hidden,
        test_progress_longest_equals_streak_hidden,
        test_progress_feed_active,
        test_progress_created_at_string,
        test_progress_created_at_datetime,
        # 2. _user_to_dict
        test_user_to_dict_all_progress_fields,
        test_user_to_dict_defaults,
        test_user_to_dict_passthrough,
        # 3. _build_user_profile
        test_profile_no_complexity_level,
        test_profile_no_study_duration,
        test_profile_empty_for_new_user,
        test_profile_includes_assessment,
        test_profile_includes_role,
        test_profile_en_header,
        # 4. TIER_PIPELINE
        test_tier_pipeline_includes_progress,
        test_tier_pipeline_tiers_defined,
        # 5. assemble_context
        test_assemble_context_has_progress_key,
        test_assemble_context_progress_populated,
        test_assemble_context_progress_for_feed_user,
        test_assemble_context_all_default_keys,
        # 6. SUBSCRIPTION_LAUNCH_DATE
        test_subscription_launch_date_in_future,
        # 7. Tier prompts
        test_tier_prompts_have_progress_placeholder,
        test_tier_prompts_have_progress_rule,
        test_tier_prompts_progress_after_user_profile,
    ]

    passed = 0
    failed = 0
    skipped = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except ImportError as e:
            print(f"⏭️ {test_fn.__name__}: пропущен ({e})")
            skipped += 1
        except AssertionError as e:
            print(f"❌ {test_fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test_fn.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print("=" * 60)
    icon = '✅' if failed == 0 else '❌'
    print(f"\n{icon} Результат: {passed} пройдено, {skipped} пропущено, {failed} провалено\n")
    if failed > 0:
        sys.exit(1)
