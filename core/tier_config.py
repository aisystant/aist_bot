"""
Progressive UI per Tier — declarative configuration.

Source-of-truth: WP-52-progressive-ui-tiers.md
Architecture ref: DP.ARCH.002 (service tiers)

Tier model (payment-first):
  T1: free (no subscription)
  T2: subscription active
  T3: T2 + DT connected
  T4: T3 + GitHub connected
  T5: admin (DEVELOPER_CHAT_ID) — menu set in bot.py, NOT here

Each tier defines:
  - keyboard: 2x2 ReplyKeyboard buttons
  - menu_commands: commands in Bot Menu Button (hamburger)
"""


class UITier:
    """UI tier constants."""
    T1_START = 1
    T2_LEARNING = 2
    T3_PERSONALIZATION = 3
    T4_CREATION = 4
    T5_ADMIN = 5


# Tier display names for user-facing messages (greeting, etc.)
TIER_DISPLAY = {
    UITier.T1_START:           "T1 — Start",
    UITier.T2_LEARNING:        "T2 — Learning",
    UITier.T3_PERSONALIZATION: "T3 — Personalization",
    UITier.T4_CREATION:        "T4 — Creation",
    UITier.T5_ADMIN:           "T5 — Admin",
}


# ═══════════════════════════════════════════════════════════
# KEYBOARD BUTTON LABELS (per service, per language)
# ═══════════════════════════════════════════════════════════

KB_LABELS = {
    'marathon':   {'ru': '📚 Марафон',    'en': '📚 Marathon',   'es': '📚 Maratón',    'fr': '📚 Marathon',   'zh': '📚 马拉松'},
    'feed':       {'ru': '📖 Лента',      'en': '📖 Feed',       'es': '📖 Feed',       'fr': '📖 Fil',        'zh': '📖 信息流'},
    'training':   {'ru': '🧠 Тренировка', 'en': '🧠 Training',   'es': '🧠 Entrena',    'fr': '🧠 Entraîn.',   'zh': '🧠 训练'},
    'test':       {'ru': '🧪 Тест',       'en': '🧪 Test',       'es': '🧪 Test',       'fr': '🧪 Test',       'zh': '🧪 测试'},
    'progress':   {'ru': '📊 Прогресс',   'en': '📊 Progress',   'es': '📊 Progreso',   'fr': '📊 Progrès',    'zh': '📊 进度'},
    'profile':    {'ru': '👤 Профиль',    'en': '👤 Profile',    'es': '👤 Perfil',     'fr': '👤 Profil',     'zh': '👤 档案'},
    'profile_dt': {'ru': '🤖 Профиль',    'en': '🤖 Profile',    'es': '🤖 Perfil',     'fr': '🤖 Profil',     'zh': '🤖 档案'},
    'dt':         {'ru': '🧬 ЦД',         'en': '🧬 DT',         'es': '🧬 GD',         'fr': '🧬 JN',         'zh': '🧬 数字孪生'},
    'club':       {'ru': '🏛 Клуб',       'en': '🏛 Club',       'es': '🏛 Club',       'fr': '🏛 Club',       'zh': '🏛 俱乐部'},
    'plans':      {'ru': '📋 Мой план',   'en': '📋 My plan',    'es': '📋 Mi plan',    'fr': '📋 Mon plan',   'zh': '📋 我的计划'},
    'notes':      {'ru': '📝 Заметки',    'en': '📝 Notes',      'es': '📝 Notas',      'fr': '📝 Notes',      'zh': '📝 笔记'},
    'mydata':     {'ru': '📁 Мои данные',  'en': '📁 My data',    'es': '📁 Mis datos',  'fr': '📁 Mes données', 'zh': '📁 我的数据'},
    'settings':   {'ru': '⚙️ Настройки',  'en': '⚙️ Settings',   'es': '⚙️ Ajustes',    'fr': '⚙️ Paramètres', 'zh': '⚙️ 设置'},
}

# Service key → slash command name (for routing)
SERVICE_TO_COMMAND = {
    'marathon': 'learn',
    'feed': 'feed',
    'training': 'train',
    'test': 'assessment',
    'progress': 'progress',
    'profile': 'profile',
    'profile_dt': 'profile',
    'dt': 'twin',
    'club': 'club',
    'plans': 'plan',
    'notes': 'notes',
    'mydata': 'mydata',
    'settings': 'settings',
}


# ═══════════════════════════════════════════════════════════
# TIER KEYBOARD LAYOUTS (2x2 grid)
# [[top-left, top-right], [bottom-left, bottom-right]]
# ═══════════════════════════════════════════════════════════

TIER_KEYBOARD = {
    UITier.T1_START:           [['marathon', 'test'],     ['progress', 'settings']],
    UITier.T2_LEARNING:        [['feed',     'test'],     ['training', 'settings']],
    UITier.T3_PERSONALIZATION: [['training', 'feed'],     ['progress', 'settings']],
    UITier.T4_CREATION:        [['plans',    'club'],     ['training', 'settings']],
    UITier.T5_ADMIN:           [['plans',    'club'],     ['training', 'settings']],
}


# ═══════════════════════════════════════════════════════════
# MENU COMMANDS PER TIER (Bot Menu Button / hamburger)
# T5 menu is set separately in bot.py (dev-specific commands)
# ═══════════════════════════════════════════════════════════

TIER_MENU_COMMANDS = {
    UITier.T1_START:           ['learn', 'test', 'progress', 'profile', 'mode', 'settings', 'help'],
    UITier.T2_LEARNING:        ['feed', 'train', 'learn', 'test', 'progress', 'profile', 'mode', 'settings', 'help'],
    UITier.T3_PERSONALIZATION: ['feed', 'train', 'learn', 'test', 'progress', 'mydata', 'mode', 'settings', 'help'],
    UITier.T4_CREATION:        ['plan', 'club', 'train', 'feed', 'progress', 'test', 'profile', 'mode', 'settings', 'start', 'help'],
    # T5: not here — set in bot.py as dev commands
}

# Command descriptions per language (for setMyCommands)
COMMAND_DESCRIPTIONS = {
    'learn':     {'ru': 'Марафон — получить урок',     'en': 'Marathon — get a lesson',   'es': 'Maratón — obtener lección',  'fr': 'Marathon — obtenir une leçon', 'zh': '马拉松 — 获取课程'},
    'feed':      {'ru': 'Лента — получить дайджест',   'en': 'Feed — get a digest',       'es': 'Feed — obtener resumen',     'fr': 'Fil — obtenir un résumé',      'zh': '信息流 — 获取摘要'},
    'train':     {'ru': 'Тренировка принципов',         'en': 'Principles training',       'es': 'Entrenamiento de principios','fr': 'Entraînement des principes',   'zh': '原则训练'},
    'test':      {'ru': 'Тест систематичности',         'en': 'Systematicity test',        'es': 'Test de sistematicidad',     'fr': 'Test de systématicité',        'zh': '系统性测试'},
    'progress':  {'ru': 'Мой прогресс',                 'en': 'My progress',               'es': 'Mi progreso',                'fr': 'Mon progrès',                  'zh': '我的进度'},
    'profile':   {'ru': 'Мой профиль',                  'en': 'My profile',                'es': 'Mi perfil',                  'fr': 'Mon profil',                   'zh': '我的档案'},
    'twin':      {'ru': 'Цифровой двойник',             'en': 'Digital twin',              'es': 'Gemelo digital',             'fr': 'Jumeau numérique',             'zh': '数字孪生'},
    'club':      {'ru': 'Клуб — публикация',            'en': 'Club — publishing',         'es': 'Club — publicación',         'fr': 'Club — publication',           'zh': '俱乐部 — 发布'},
    'notes':     {'ru': 'Заметки',                      'en': 'Notes',                     'es': 'Notas',                      'fr': 'Notes',                        'zh': '笔记'},
    'plan':      {'ru': 'Рабочий план',                 'en': 'Work plan',                 'es': 'Plan de trabajo',            'fr': 'Plan de travail',              'zh': '工作计划'},
    'rp':        {'ru': 'Рабочие продукты',             'en': 'Work products',             'es': 'Productos de trabajo',       'fr': 'Produits de travail',          'zh': '工作产品'},
    'report':    {'ru': 'Отчёт дня',                    'en': 'Day report',                'es': 'Informe del día',            'fr': 'Rapport du jour',              'zh': '日报'},
    'mydata':    {'ru': 'Мои данные',                    'en': 'My data',                   'es': 'Mis datos',                  'fr': 'Mes données',                  'zh': '我的数据'},
    'mode':      {'ru': 'Клавиатура',                    'en': 'Keyboard',                  'es': 'Teclado',                    'fr': 'Clavier',                      'zh': '键盘'},
    'settings':  {'ru': 'Настройки и подписка',           'en': 'Settings & subscription',   'es': 'Ajustes y suscripción',      'fr': 'Paramètres et abonnement',     'zh': '设置与订阅'},
    'start':     {'ru': 'Перезапуск бота',                'en': 'Restart the bot',            'es': 'Reiniciar el bot',           'fr': 'Redémarrer le bot',            'zh': '重启机器人'},
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
