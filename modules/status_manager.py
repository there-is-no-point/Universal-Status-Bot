import json
import threading
import requests
import redis
from datetime import datetime
import sys
import os

# Пытаемся импортировать конфиг из корня проекта
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class StatusManager:
    _instance = None
    _redis = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(StatusManager, cls).__new__(cls)
            cls._instance._init_redis()
        return cls._instance

    def _init_redis(self):
        try:
            if hasattr(config, 'REDIS_URL') and config.REDIS_URL:
                self._redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
            else:
                print("⚠️ [StatusManager] REDIS_URL не найден в config.py")
        except Exception as e:
            print(f"⚠️ [StatusManager] Ошибка Redis: {e}")

    def update_status(self, project_name: str, data: dict):
        """
        Отправляет данные в Облако.
        project_name: Имя проекта (например "HackQuest")
        data: Любые данные (статус, монеты, ошибки)
        """
        if not self._redis: return

        try:
            # Берем имя устройства из конфига или ставим дефолтное
            device_name = getattr(config, 'DEVICE_NAME', 'Unknown_Device')

            data["last_updated"] = datetime.now().strftime("%H:%M:%S")
            data_str = json.dumps(data, ensure_ascii=False)

            # Пишем в Redis: ключ=status:HackQuest, поле=Server_1
            self._redis.hset(f"status:{project_name}", device_name, data_str)
            # Живет 24 часа
            self._redis.expire(f"status:{project_name}", 86400)

        except Exception:
            pass  # Тихо игнорируем ошибки, чтобы не ломать софт

    def send_alert(self, text: str, status: str = "Info"):
        """
        Прямая отправка в Telegram (для критических ошибок/финиша).
        Требует TG_BOT_TOKEN в конфиге ПРОЕКТА.
        """
        if not getattr(config, 'USE_TG_BOT', False): return

        device = getattr(config, 'DEVICE_NAME', 'Unknown')
        emoji = "✅" if status == "Success" else "❌" if status == "Error" else "⚠️"
        msg = f"{emoji} <b>{status}</b> [{device}]\n\n{text}"

        def _send():
            try:
                token = getattr(config, 'TG_BOT_TOKEN', '')
                uid = getattr(config, 'TG_USER_ID', '')
                if token and uid:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    requests.post(url, json={"chat_id": uid, "text": msg, "parse_mode": "HTML"})
            except:
                pass

        threading.Thread(target=_send, daemon=True).start()


status_manager = StatusManager()