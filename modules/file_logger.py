import logging
from logging.handlers import RotatingFileHandler
import os


class SmartFormatter(logging.Formatter):
    """
    Умный форматировщик:
    1. Проверяет, есть ли в логе переменная 'address', 'wallet' или 'account'.
    2. Если есть — добавляет её отдельным столбцом.
    3. Если нет — пишет лог в стандартном формате.
    """

    def format(self, record):
        # 1. Форматируем само сообщение
        record.message = record.getMessage()

        # 2. ГАРАНТИРОВАННО создаем время (asctime), даже если Python думает, что оно не нужно
        if not hasattr(record, 'asctime'):
            record.asctime = self.formatTime(record, self.datefmt)

        # 3. Ищем адрес кошелька в переменных
        wallet = getattr(record, 'address', None) or \
                 getattr(record, 'wallet', None) or \
                 getattr(record, 'account', None)

        # 4. Собираем итоговую строку
        if wallet:
            # ✅ Формат с кошельком (4-й столбец)
            s = f"{record.asctime} | {record.levelname} | {record.name} | {wallet} | {record.message}"
        else:
            # ❌ Обычный формат
            s = f"{record.asctime} | {record.levelname} | {record.name} | {record.message}"

        # 5. Обработка ошибок (Traceback), если они есть
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + record.exc_text
        return s


def install_file_logger():
    # 1. Получаем корневой логгер
    root_logger = logging.getLogger()

    # 2. Проверяем, не подключен ли уже файл
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and "app.log" in handler.baseFilename:
            return

    # 3. Настраиваем файл
    log_file = "app.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=1,
        encoding="utf-8"
    )

    # 4. ПОДКЛЮЧАЕМ НАШ УМНЫЙ ФОРМАТИРОВЩИК
    # Передаем и fmt, и datefmt, чтобы всё работало корректно
    formatter = SmartFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    )

    file_handler.setFormatter(formatter)

    # 5. Цепляем к системе
    root_logger.addHandler(file_handler)


# Запускаем установку сразу при импорте
install_file_logger()