import logging
from logging.handlers import RotatingFileHandler
import os
from datetime import datetime

# ==========================================
# 🧠 ADAPTIVE CONFIGURATION / АДАПТИВНЫЕ НАСТРОЙКИ
# ==========================================
# Модуль сам ищет эти поля в объекте лога (record).
# Если хотите добавить свои поля для поиска, просто допишите их сюда.
ADAPTIVE_CONFIG = {
    # Поля, которые считаются "Идентификатором" (Кошелек, Юзер и т.д.)
    "IDENTITY_FIELDS": [
        "address", "wallet", "wallet_address", "account", "user", 
        "user_id", "login", "email", "phone"
    ],
    
    # Поля, которые считаются "Позицией" (496/500, Step 1/5 и т.д.)
    "POSITION_FIELDS": [
        "position", "pos", "step", "progress", "count", "iter"
    ],
    
    # Имена логгеров, которые мы игнорируем, пытаясь найти реальное имя модуля
    "GENERIC_NAMES": ["root", "logger", "ConcreteBot", "bot", "main"],
    
    # Формат по умолчанию (если адаптивность не сработала)
    "DATE_FORMAT": "%Y-%m-%d %H:%M:%S.%f",
    "NO_IDENTITY_TEXT": "NoAddress"
}
# ==========================================

# Импортируем bot_link, чтобы отправлять в Redis
try:
    from .notifications import bot_link
except ImportError:
    bot_link = None


class AdaptiveFormatter(logging.Formatter):
    """
    Адаптивный форматировщик. 
    Сам находит кошелек и позицию в полях лога и форматирует их в красивую таблицу.
    """

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime(ADAPTIVE_CONFIG["DATE_FORMAT"])[:-3]

    def _find_attribute(self, record, field_list):
        """Ищет первое непустое значение из списка полей."""
        for field in field_list:
            val = getattr(record, field, None)
            if val: return val
        # Проверяем также extra словарь, если он есть
        if hasattr(record, '__dict__'):
            for field in field_list:
                val = record.__dict__.get(field)
                if val: return val
        return None

    def _get_identity(self, record):
        val = self._find_attribute(record, ADAPTIVE_CONFIG["IDENTITY_FIELDS"])
        if val: 
            return str(val)
        return ADAPTIVE_CONFIG["NO_IDENTITY_TEXT"].center(42)

    def _get_position(self, record):
        val = self._find_attribute(record, ADAPTIVE_CONFIG["POSITION_FIELDS"])
        if val: 
            return str(val)
        return ""

    def _get_module_name(self, record):
        # 1. Сначала ищем явно переданное имя модуля
        name = getattr(record, 'module_name', None)
        if name: return name

        # 2. Иначе берем имя логгера
        name = record.name
        
        # 3. Пытаемся очистить от общих слов
        full_name_check = name
        is_generic = False
        for gen in ADAPTIVE_CONFIG["GENERIC_NAMES"]:
            if gen in full_name_check:
                is_generic = True
                break
        
        if is_generic or "Bot" in full_name_check:
             if hasattr(record, 'module') and record.module:
                name = record.module
        
        return name

    def format(self, record):
        record.message = record.getMessage()
        timestamp = self.formatTime(record)
        
        # Авто-обнаружение полей
        wallet = self._get_identity(record)
        position = self._get_position(record)
        module_name = self._get_module_name(record)
        
        # Жесткий табличный формат, который вы просили
        # DATE TIME | LEVEL | MODULE | POS | WALLET | MSG
        s = f"{timestamp} | {record.levelname:<7} | {module_name:<20} | {position:<8} | {wallet} | {record.message}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + record.exc_text
        return s


class RedisErrorHandler(logging.Handler):
    """
    Адаптивный перехватчик ошибок для Redis.
    Использует ту же логику авто-обнаружения, что и форматировщик.
    """

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            if not bot_link: return
            try:
                # 1. Ищем identity для ключа Redis
                wallet = None
                for field in ADAPTIVE_CONFIG["IDENTITY_FIELDS"]:
                    val = getattr(record, field, None)
                    if val:
                        wallet = val
                        break
                
                # Если не нашли - не отправляем (или можно отправлять с NoAddress, но обычно это спам)
                if not wallet:
                    return

                # 2. Подготавливаем данные (дублируем логику AdaptiveFormatter для консистентности)
                dt = datetime.fromtimestamp(record.created)
                timestamp = dt.strftime(ADAPTIVE_CONFIG["DATE_FORMAT"])[:-3]

                # Модуль
                source_name = getattr(record, 'module_name', None)
                if not source_name:
                    source_name = record.name
                    for gen in ADAPTIVE_CONFIG["GENERIC_NAMES"]:
                        if gen in source_name or "Bot" in source_name:
                             if hasattr(record, 'module') and record.module:
                                source_name = record.module
                                break
                
                # Позиция
                position = None
                for field in ADAPTIVE_CONFIG["POSITION_FIELDS"]:
                    val = getattr(record, field, None)
                    if val:
                        position = str(val)
                        break
                if not position: position = ""
                
                # Формируем строку лога
                log_entry = f"{timestamp} | {record.levelname:<7} | {source_name:<20} | {position:<8} | {record.getMessage()}"

                # Отправляем
                bot_link.add_temp_error(bot_link.project_name, wallet, log_entry)

            except Exception:
                self.handleError(record)


def install_file_logger():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and "app.log" in handler.baseFilename:
            return

    # 1. Файловый логгер
    log_file = "app.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )

    # Используем наш новый адаптивный форматтер
    formatter = AdaptiveFormatter(fmt=None, datefmt=None)
    
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 2. Redis Handler
    redis_handler = RedisErrorHandler()
    root_logger.addHandler(redis_handler)


install_file_logger()