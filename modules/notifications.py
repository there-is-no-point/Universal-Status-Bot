import sys
import os

# 1. ЗАЩИТА ОТ ВЫЛЕТОВ
sys.modules['hiredis'] = None

import redis
import json
import threading
import time
import requests  # 👈 Добавляем для прямой отправки
from datetime import datetime

# --- НАСТРОЙКИ ОТЛАДКИ ---
DEBUG_MODE = False
# -------------------------

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
except ImportError:
    config = None


class BotLink:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BotLink, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return

        self.redis_url = getattr(config, 'REDIS_URL', None)
        self.active_client = None
        self.stats_callback = None
        self.progress_callback = None
        self.inventory_callback = None
        self.running = False
        self.worker_name = getattr(config, 'WORKER_NAME', "Unknown_Worker")
        # Имя проекта для настроек (пытаемся угадать или берем из конфига, если есть)
        self.project_name = "UnknownProject"

        if self.redis_url:
            try:
                self.writer = redis.Redis.from_url(self.redis_url, decode_responses=True, ssl_cert_reqs=None)
                self.reader = redis.Redis.from_url(self.redis_url, decode_responses=True, ssl_cert_reqs=None)
                self.pubsub = self.reader.pubsub()

                self.running = True
                self.start_listener()
            except Exception:
                pass

        self._initialized = True

    def register_client(self, client_instance, stats_callback=None, progress_callback=None, inventory_callback=None):
        self.active_client = client_instance
        if stats_callback: self.stats_callback = stats_callback
        if progress_callback: self.progress_callback = progress_callback
        if inventory_callback: self.inventory_callback = inventory_callback

        # Пытаемся определить имя проекта для фильтрации уведомлений
        # Обычно мы узнаем его только в момент отправки, но сохраним на будущее
        pass

    def report_error(self, project_name, wallet_address, error_text):
        if not self.running: return
        try:
            self.writer.sadd(f"failures:{project_name}:{self.worker_name}", wallet_address)
            timestamp = datetime.now().strftime("%H:%M:%S")
            full_error = f"[{timestamp}] {error_text}"
            self.writer.hset(f"fail_logs:{project_name}:{self.worker_name}", wallet_address, full_error)
        except:
            pass

    def _extract_stats(self):
        if not self.active_client: return None
        c = self.active_client

        extra_stats = {}
        if self.inventory_callback:
            try:
                extra_stats = self.inventory_callback()
            except:
                pass
        elif self.stats_callback:
            try:
                extra_stats = self.stats_callback(c)
            except:
                pass

        progress_str = ""
        if self.progress_callback:
            try:
                progress_str = self.progress_callback()
            except:
                pass

        data = {
            "status": "Working 🟢",
            "current_account": getattr(c, 'address', 'Unknown'),
            "last_updated": time.time(),
            "pos_current": getattr(c, 'position', 0),
            "pos_total": getattr(c, 'total_accounts', 0),
            "progress": progress_str
        }
        data.update(extra_stats)
        return data

    def _send_log(self):
        if not self.running: return
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(base_dir, "app.log")

            text = ""
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors='replace') as f:
                    f.seek(0, os.SEEK_END)
                    seek_pos = max(0, f.tell() - 16384)
                    f.seek(seek_pos)
                    text = f"📂 ...Last 16KB of app.log:\n\n" + f.read()
            else:
                text = f"❌ Log file not found at: {log_path}"

            # Логи отправляем ТОЛЬКО через Redis (слишком сложно слать файлы напрямую без бота)
            self.writer.publish("telegram_alerts", json.dumps({
                "type": "log_delivery", "project": "HackQuest", "worker": self.worker_name, "text": text
            }))
        except Exception as e:
            self.send_notification("error", f"Log Error: {e}")

    # 👇 ГЛАВНОЕ ИЗМЕНЕНИЕ ЗДЕСЬ
    def send_notification(self, type_, text, project_override=None):
        if not self.running: return

        # Определяем имя проекта (обычно зашито жестко как "HackQuest" в вызовах старых,
        # но лучше передавать динамически. Пока оставим HackQuest по умолчанию)
        proj = project_override if project_override else "HackQuest"

        try:
            # 1. ПРОВЕРКА НАСТРОЕК (ФИЛЬТР) ПРЯМО НА ИСТОЧНИКЕ
            # Если глобальный мьют включен - выходим
            if self.writer.get("settings:mute_all") == "1":
                return
            # Если проект заглушен - выходим
            if self.writer.get(f"settings:mute:{proj}") == "1":
                return

            # 2. Формируем пакет
            payload = {
                "type": type_, "project": proj, "worker": self.worker_name, "text": text
            }
            json_data = json.dumps(payload)

            # 3. ОТПРАВЛЯЕМ В REDIS И СМОТРИМ, КТО УСЛЫШАЛ
            listeners_count = self.writer.publish("telegram_alerts", json_data)

            # 4. ЕСЛИ listeners_count == 0, ЗНАЧИТ БОТ ВЫКЛЮЧЕН
            # Включаем аварийную прямую отправку
            if listeners_count == 0:
                self._fallback_send_direct(type_, proj, text)

        except Exception as e:
            if DEBUG_MODE: print(f"Send error: {e}")

    def _fallback_send_direct(self, type_, project, text):
        """Отправляет сообщение напрямую в Telegram, если bot.py мертв"""
        try:
            token = getattr(config, 'TG_BOT_TOKEN', None)
            uid = getattr(config, 'TG_USER_ID', None)
            if not token or not uid: return

            # Формируем такой же красивый текст, как делал bot.py
            header = f"🤖 <b>{project}</b> | {self.worker_name}"

            if type_ == "error":
                msg = f"🔴 <b>ALARM (Direct):</b>\n{header}\n\n<pre>{text}</pre>"
            elif type_ == "success":
                msg = f"✅ <b>FINISHED (Direct):</b>\n{header}\n\n{text}"
            else:
                msg = f"ℹ️ <b>INFO (Direct):</b>\n{header}\n\n{text}"

            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": uid, "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
        except:
            pass

    def _listener_loop(self):
        channel = f"cmd:HackQuest:{self.worker_name}"
        try:
            self.pubsub.subscribe(channel)
        except:
            return

        while self.running:
            try:
                msg = self.pubsub.get_message(timeout=1)
                if msg and msg['type'] == 'message':
                    data = msg['data']
                    if data == "get_log":
                        threading.Thread(target=self._send_log).start()
                    elif data == "update_status":
                        stats = self._extract_stats()
                        if stats:
                            self.writer.hset("status:HackQuest", self.worker_name, json.dumps(stats))
            except:
                time.sleep(1)
            time.sleep(0.1)

    def start_listener(self):
        t = threading.Thread(target=self._listener_loop, daemon=True)
        t.start()


bot_link = BotLink()