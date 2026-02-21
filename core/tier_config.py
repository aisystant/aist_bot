"""
Progressive UI per Tier — declarative configuration.

Source-of-truth: WP-52-progressive-ui-tiers.md
Architecture ref: DP.ARCH.002 (service tiers)

Each tier defines:
  - keyboard: 2x2 ReplyKeyboard buttons
  - menu_commands: commands in Bot Menu Button (hamburger)
"""


class UITier:
    """UI tier constants (behavioral, not payment)."""
    T1_START = 1
    T2_LEARNING = 2
    T3_PERSONALIZATION = 3
    T4_CREATION = 4
    T5_ADMIN = 5


# ═══════════════════════════════════════════════════════════
# KEYBOARD BUTTON LABELS (per service, per language)
# ═══════════════════════════════════════════════════════════

KB_LABELS = {
    'marathon':   {'ru': '📚 Марафон',   'en': '📚 Marathon',  'es': '📚 Maratón',  'fr': '📚 Marathon', 'zh': '📚 马拉松'},
    'feed':       {'ru': '📖 Лента',     'en': '📖 Feed',      'es': '📖 Feed',     'fr': '📖 Fil',      'zh': '📖 信息流'},
    'test':       {'ru': '🧪 Тест',      'en': '🧪 Test',      'es': '🧪 Test',     'fr': '🧪 Test',     'zh': '🧪 测试'},
    'progress':   {'ru': '📊 Прогресс',  'en': '📊 Progress',  'es': '📊 Progreso', 'fr': '📊 Progrès',  'zh': '📊 进度'},
    'profile':    {'ru': '👤 Профиль',   'en': '👤 Profile',   'es': '👤 Perfil',   'fr': '👤 Profil',   'zh': '👤 档案'},
    'profile_dt': {'ru': '🤖 Профиль',   'en': '🤖 Profile',   'es': '🤖 Perfil',   'fr': '🤖 Profil',   'zh': '🤖 档案'},
    'plans':      {'ru': '📋 Мой план',  'en': '📋 My plan',   'es': '📋 Mi plan',  'fr': '📋 Mon plan', 'zh': '📋 我的计划'},
    'notes':      {'ru': '📝 Заметки',   'en': '📝 Notes',     'es': '📝 Notas',    'fr': '📝 Notes',    'zh': '📝 笔记'},
}

# Service key → slash command name (for routing)
SERVICE_TO_COMMAND = {
    'marathon': 'learn',
    'feed': 'feed',
    'test': 'assessment',
    'progress': 'progress',
    'profile': 'profile',
    'profile_dt': 'profile',
    'plans': 'plan',
    'notes': 'notes',
}


# ═══════════════════════════════════════════════════════════
# TIER KEYBOARD LAYOUTS (2x2 grid)
# [[top-left, top-right], [bottom-left, bottom-right]]
# ═══════════════════════════════════════════════════════════

TIER_KEYBOARD = {
    UITier.T1_START:           [['marathon', 'test'],  ['progress', 'profile']],
    UITier.T2_LEARNING:        [['feed',     'test'],  ['progress', 'profile']],
    UITier.T3_PERSONALIZATION: [['feed',     'test'],  ['progress', 'profile_dt']],
    UITier.T4_CREATION:        [['plans',    'notes'], ['feed',     'profile_dt']],
    UITier.T5_ADMIN:           [['plans',    'notes'], ['feed',     'profile_dt']],
}


# ═══════════════════════════════════════════════════════════
# MENU COMMANDS PER TIER (Bot Menu Button / hamburger)
# ═══════════════════════════════════════════════════════════

TIER_MENU_COMMANDS = {
    UITier.T1_START:           ['learn', 'test', 'progress', 'profile', 'help'],
    UITier.T2_LEARNING:        ['feed', 'test', 'progress', 'learn', 'profile', 'help'],
    UITier.T3_PERSONALIZATION: ['feed', 'test', 'progress', 'twin', 'learn', 'notes', 'help'],
    UITier.T4_CREATION:        ['plan', 'notes', 'rp', 'report', 'feed', 'progress', 'test', 'help'],
    UITier.T5_ADMIN:           ['plan', 'notes', 'rp', 'report', 'feed', 'progress', 'test', 'analytics', 'help'],
}

# Command descriptions per language (for setMyCommands)
COMMAND_DESCRIPTIONS = {
    'learn':     {'ru': 'Марафон — получить урок',     'en': 'Marathon — get a lesson',   'es': 'Maratón — obtener lección',  'fr': 'Marathon — obtenir une leçon', 'zh': '马拉松 — 获取课程'},
    'feed':      {'ru': 'Лента — получить дайджест',   'en': 'Feed — get a digest',       'es': 'Feed — obtener resumen',     'fr': 'Fil — obtenir un résumé',      'zh': '信息流 — 获取摘要'},
    'test':      {'ru': 'Тест систематичности',         'en': 'Systematicity test',        'es': 'Test de sistematicidad',     'fr': 'Test de systématicité',        'zh': '系统性测试'},
    'progress':  {'ru': 'Мой прогресс',                 'en': 'My progress',               'es': 'Mi progreso',                'fr': 'Mon progrès',                  'zh': '我的进度'},
    'profile':   {'ru': 'Мой профиль',                  'en': 'My profile',                'es': 'Mi perfil',                  'fr': 'Mon profil',                   'zh': '我的档案'},
    'twin':      {'ru': 'Профиль развития',             'en': 'Development profile',       'es': 'Perfil de desarrollo',       'fr': 'Profil de développement',      'zh': '发展档案'},
    'notes':     {'ru': 'Заметки',                      'en': 'Notes',                     'es': 'Notas',                      'fr': 'Notes',                        'zh': '笔记'},
    'plan':      {'ru': 'Рабочий план',                 'en': 'Work plan',                 'es': 'Plan de trabajo',            'fr': 'Plan de travail',              'zh': '工作计划'},
    'rp':        {'ru': 'Рабочие продукты',             'en': 'Work products',             'es': 'Productos de trabajo',       'fr': 'Produits de travail',          'zh': '工作产品'},
    'report':    {'ru': 'Отчёт дня',                    'en': 'Day report',                'es': 'Informe del día',            'fr': 'Rapport du jour',              'zh': '日报'},
    'help':      {'ru': 'Помощь',                       'en': 'Help',                      'es': 'Ayuda',                      'fr': 'Aide',                         'zh': '帮助'},
    'analytics': {'ru': 'Аналитика',                    'en': 'Analytics',                 'es': 'Analíticas',                 'fr': 'Analytiques',                  'zh': '分析'},
}


# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def get_kb_texts(service_key: str) -> list[str]:
    """Get all possible keyboard button texts for a service (all languages)."""
    labels = KB_LABELS.get(service_key, {})
    return list(labels.values())


def _build_reply_kb_map() -> dict[str, str]:
    """Build reverse map: button text → command name."""
    result = {}
    for service_key, lang_labels in KB_LABELS.items():
        command = SERVICE_TO_COMMAND.get(service_key)
        if command:
            for label in lang_labels.values():
                result[label] = command
    return result


REPLY_KB_TEXTS_TO_COMMANDS = _build_reply_kb_map()
ALL_KB_TEXTS = frozenset(REPLY_KB_TEXTS_TO_COMMANDS.keys())
