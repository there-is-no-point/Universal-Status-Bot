import json
import threading
import requests
import redis
import time
from datetime import datetime
import sys
import os

# --- НАСТРОЙКИ ОТЛАДКИ ---
DEBUG_MODE = False
# -------------------------

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if DEBUG_MODE:
    print("\n" + "=" * 30)
    print("📊 [DEBUG] STATUS MANAGER STARTUP")

try:
    import config
except ImportError as e:
    print(f"❌ [StatusManager] CRITICAL: Config import failed! {e}")
    config = None

# 🔥 ВАЖНО: Импортируем bot_link, чтобы узнавать динамическое имя (--worker)
# Используем try-except, чтобы избежать циклических импортов, если они возникнут
try:
    from .notifications import bot_link
except ImportError:
    bot_link = None

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
                self._redis = redis.Redis.from_url(
                    config.REDIS_URL,
                    decode_responses=True,
                    ssl_cert_reqs=None
                )
                self._redis.ping()
                if DEBUG_MODE:
                    print(f"✅ [StatusManager] Redis Connected!")
            else:
                if DEBUG_MODE:
                    print("⚠️ [StatusManager] REDIS_URL missing. Skipping.")
        except Exception as e:
            print(f"⚠️ [StatusManager] Redis Connection Failed: {e}")
            self._redis = None

    def update_status(self, project_name: str, data: dict):
        """
        Отправляет статус в Redis.
        Имя воркера берется динамически, если задан аргумент --worker.
        """
        if not self._redis: return

        try:
            # 👇 ЛОГИКА ОПРЕДЕЛЕНИЯ ИМЕНИ
            # 1. Сначала пробуем узнать имя у bot_link (оно там правильное, с учетом флагов запуска)
            if bot_link and hasattr(bot_link, 'worker_name'):
                device_name = bot_link.worker_name
            else:
                # 2. Если не вышло - берем стандартное из конфига
                device_name = getattr(config, 'DEVICE_NAME', getattr(config, 'WORKER_NAME', 'Unknown_Device'))

            # Добавляем время последнего обновления
            data["last_updated"] = time.time()

            data_str = json.dumps(data, ensure_ascii=False)

            # Пишем в Redis под правильным (динамическим) именем
            self._redis.hset(f"status:{project_name}", device_name, data_str)
            self._redis.expire(f"status:{project_name}", 86400)

            if DEBUG_MODE:
                print(f"📤 [DEBUG] Status sent for {device_name}")

        except Exception as e:
            if DEBUG_MODE:
                print(f"❌ [StatusManager] Redis Write Error: {e}")

    def send_alert(self, text: str, status: str = "Info"):
        if not getattr(config, 'USE_TG_BOT', False): return

        # Тут тоже пытаемся взять правильное имя для заголовка
        if bot_link and hasattr(bot_link, 'worker_name'):
            device = bot_link.worker_name
        else:
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