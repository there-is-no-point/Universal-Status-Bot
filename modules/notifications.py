import sys
import os

# 1. ЗАЩИТА ОТ ВЫЛЕТОВ
sys.modules['hiredis'] = None

import redis
import json
import threading
import time
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

    # 👇 ОБНОВЛЕННАЯ ФУНКЦИЯ: Привязываем ошибки к worker_name
    def report_error(self, project_name, wallet_address, error_text):
        if not self.running: return
        try:
            # Используем ключи с именем воркера, чтобы разделить логи по устройствам
            # SET для списка уникальных кошельков
            self.writer.sadd(f"failures:{project_name}:{self.worker_name}", wallet_address)

            # HASH для хранения текста ошибки
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

            self.writer.publish("telegram_alerts", json.dumps({
                "type": "log_delivery", "project": "HackQuest", "worker": self.worker_name, "text": text
            }))
        except Exception as e:
            self.send_notification("error", f"Log Error: {e}")

    def send_notification(self, type_, text):
        if not self.running: return
        try:
            self.writer.publish("telegram_alerts", json.dumps({
                "type": type_, "project": "HackQuest", "worker": self.worker_name, "text": text
            }))
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