import json
import threading
import requests
import redis
from datetime import datetime
import sys
import os

# --- НАСТРОЙКИ ОТЛАДКИ ---
# Поставь True, если бот не запускается или не видит конфиг
DEBUG_MODE = False
# -------------------------

# 1. Windows Fix: Принудительно добавляем корень проекта в пути
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if DEBUG_MODE:
    print("\n" + "=" * 30)
    print("📊 [DEBUG] STATUS MANAGER STARTUP")
    print(f"📂 Module path: {os.path.dirname(os.path.abspath(__file__))}")

try:
    import config

    if DEBUG_MODE:
        print(f"✅ Config imported: {config}")
        print(f"🔎 DEVICE_NAME: {getattr(config, 'DEVICE_NAME', '❌ MISSING')}")
        print(f"🔎 REDIS_URL: {'✅ FOUND' if getattr(config, 'REDIS_URL', None) else '❌ MISSING'}")
except ImportError as e:
    print(f"❌ [StatusManager] CRITICAL: Config import failed! {e}")
    config = None
except Exception as e:
    print(f"❌ [StatusManager] CRITICAL: Unexpected config error: {e}")
    config = None

if DEBUG_MODE:
    print("=" * 30 + "\n")


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
                # ssl_cert_reqs=None решает проблему SSL на Windows
                self._redis = redis.Redis.from_url(
                    config.REDIS_URL,
                    decode_responses=True,
                    ssl_cert_reqs=None
                )
                # Быстрая проверка соединения
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
        """
        if not self._redis: return

        try:
            # Если имя не задано, используем Unknown
            device_name = getattr(config, 'DEVICE_NAME', 'Unknown_Device')

            data["last_updated"] = datetime.now().strftime("%H:%M:%S")
            data_str = json.dumps(data, ensure_ascii=False)

            self._redis.hset(f"status:{project_name}", device_name, data_str)
            self._redis.expire(f"status:{project_name}", 86400)  # TTL 24h

            if DEBUG_MODE:
                print(f"📤 [DEBUG] Status sent for {device_name}")

        except Exception as e:
            if DEBUG_MODE:
                print(f"❌ [StatusManager] Redis Write Error: {e}")

    def send_alert(self, text: str, status: str = "Info"):
        """
        Отправляет критические уведомления в TG (если включено).
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