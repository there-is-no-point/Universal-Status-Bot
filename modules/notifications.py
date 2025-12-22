import redis
import json
import threading
import time
import os

# Настройки (возьми из своего конфига)
REDIS_URL = "redis://default:ПАРОЛЬ@ip:port" 
PROJECT_NAME = "HackQuest"
WORKER_NAME = "Linux Notebook" # Или динамически

class BotLink:
    def __init__(self):
        self.r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.pubsub = self.r.pubsub()
        self.running = True
        
    def send_notification(self, type_, message):
        """Отправляет уведомление боту (Error, Info, Success)"""
        payload = {
            "project": PROJECT_NAME,
            "worker": WORKER_NAME,
            "type": type_, # 'error', 'success', 'info'
            "text": message
        }
        # Публикуем в канал 'alerts'
        self.r.publish("telegram_alerts", json.dumps(payload))

    def send_log_file(self):
        """Читает последние 100 строк лога и отправляет боту"""
        log_content = ""
        try:
            # Замени 'app.log' на имя твоего файла логов
            if os.path.exists("app.log"):
                with open("app.log", "r", encoding="utf-8") as f:
                    # Читаем последние 2000 символов (чтобы не забить Redis)
                    f.seek(0, os.SEEK_END)
                    file_size = f.tell()
                    seek_pos = max(0, file_size - 4000) # Последние 4кб
                    f.seek(seek_pos)
                    log_content = f.read()
            else:
                log_content = "❌ Файл логов не найден!"
        except Exception as e:
            log_content = f"❌ Ошибка чтения лога: {str(e)}"

        payload = {
            "project": PROJECT_NAME,
            "worker": WORKER_NAME,
            "type": "log_delivery",
            "text": log_content
        }
        self.r.publish("telegram_alerts", json.dumps(payload))

    def listen_for_commands(self):
        """Фоновый слушатель команд ОТ бота"""
        # Подписываемся на канал, уникальный для этого воркера
        my_channel = f"cmd:{PROJECT_NAME}:{WORKER_NAME}"
        self.pubsub.subscribe(my_channel)
        
        print(f"📡 Слушаю команды в канале: {my_channel}")

        while self.running:
            message = self.pubsub.get_message()
            if message and message['type'] == 'message':
                data = message['data']
                if data == "get_log":
                    print("📥 Получен запрос на скачивание логов...")
                    self.send_log_file()
            time.sleep(1)

    def start_listener(self):
        """Запуск слушателя в отдельном потоке"""
        t = threading.Thread(target=self.listen_for_commands, daemon=True)
        t.start()

# Глобальный экземпляр
bot_link = BotLink()