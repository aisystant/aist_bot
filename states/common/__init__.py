"""
Общие стейты (common).

Содержит:
- start.py: начало работы, онбординг
- error.py: обработка ошибок
- mode_select.py: выбор режима работы (главное меню)
- profile.py: профиль пользователя (персональные данные)
- settings.py: настройки системы (язык, расписание, подключения)
- consultation.py: консультация (ответ на вопрос пользователя)
"""

from .start import StartState
from .error import ErrorState
from .mode_select import ModeSelectState
from .profile import ProfileState
from .settings import SettingsState
from .consultation import ConsultationState

__all__ = ['StartState', 'ErrorState', 'ModeSelectState', 'ProfileState', 'SettingsState', 'ConsultationState']
