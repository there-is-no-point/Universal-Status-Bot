import logging
from logging.handlers import RotatingFileHandler
import os


def install_file_logger():
    # 1. Получаем корневой логгер (через него проходят все сообщения)
    root_logger = logging.getLogger()

    # 2. Проверяем, не подключен ли уже файл (чтобы не дублировать)
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and "app.log" in handler.baseFilename:
            return

    # 3. Настраиваем файл: 5 МБ макс, 1 резервная копия, UTF-8
    log_file = "app.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=1,
        encoding="utf-8"
    )

    # 4. Формат записи: ВРЕМЯ | УРОВЕНЬ | ИМЯ МОДУЛЯ | СООБЩЕНИЕ
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    file_handler.setFormatter(formatter)

    # 5. Цепляем к системе
    root_logger.addHandler(file_handler)


# Запускаем установку сразу при импорте этого файла
install_file_logger()