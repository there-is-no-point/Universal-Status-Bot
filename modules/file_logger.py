import logging
from logging.handlers import RotatingFileHandler
import os
# Импортируем bot_link, чтобы отправлять в Redis
from .notifications import bot_link


class SmartFormatter(logging.Formatter):
    """
    Умный форматировщик
    """

    def format(self, record):
        record.message = record.getMessage()
        if not hasattr(record, 'asctime'):
            record.asctime = self.formatTime(record, self.datefmt)

        wallet = getattr(record, 'address', None) or \
                 getattr(record, 'wallet', None) or \
                 getattr(record, 'account', None)

        if wallet:
            s = f"{record.asctime} | {record.levelname} | {record.name} | {wallet} | {record.message}"
        else:
            s = f"{record.asctime} | {record.levelname} | {record.name} | {record.message}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + record.exc_text
        return s


# === НОВЫЙ КЛАСС: Redis Spy ===
class RedisErrorHandler(logging.Handler):
    """
    Перехватывает ошибки и отправляет их в буфер Redis через bot_link
    """

    def emit(self, record):
        # Реагируем только на ERROR и CRITICAL
        if record.levelno >= logging.ERROR:
            try:
                # Пытаемся найти адрес кошелька
                wallet = getattr(record, 'address', None) or \
                         getattr(record, 'wallet', None) or \
                         getattr(record, 'account', None)

                # Если кошелька нет, мы не знаем куда писать ошибку (пропускаем или пишем в Global)
                if not wallet:
                    return

                    # Формируем строку как в логе
                if not hasattr(record, 'asctime'):
                    record.asctime = self.formatTime(record, "%H:%M:%S")  # Короткое время

                # Формат: TIME | LEVEL | MODULE | MESSAGE
                log_entry = f"{record.asctime} | {record.levelname} | {record.name} | {record.getMessage()}"

                # Отправляем в буфер
                # (Проект мы берем из bot_link, так как logger не знает о проекте)
                bot_link.add_temp_error(bot_link.project_name, wallet, log_entry)

            except Exception:
                self.handleError(record)


def install_file_logger():
    root_logger = logging.getLogger()

    # Проверка чтобы не дублировать
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and "app.log" in handler.baseFilename:
            return

    # 1. Файловый логгер (как было)
    log_file = "app.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )

    formatter = SmartFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 2. 🔥 ПОДКЛЮЧАЕМ НАШ ШПИОН (Redis Handler)
    redis_handler = RedisErrorHandler()
    # Ему не нужен форматтер, он сам форматирует внутри emit
    root_logger.addHandler(redis_handler)


install_file_logger()