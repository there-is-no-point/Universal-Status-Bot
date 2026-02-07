import sys
import os

# 1. ЗАЩИТА ОТ ВЫЛЕТОВ
sys.modules['hiredis'] = None

import redis
import json
import threading
import time
import requests
from datetime import datetime

# ==========================================
# ⚙️ ГЛОБАЛЬНЫЕ НАСТРОЙКИ (Воркера)
# ==========================================

DEBUG_MODE = False

# Включаем "Умный пульс" для долгих задач
ENABLE_HEARTBEAT = True

# Настройка времени для ЭТОГО КОНКРЕТНОГО проекта
HEARTBEAT_THRESHOLD = 3600

# ==========================================


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
        self.project_name = "UnknownProject"

        # === 🔥 ЛОГИКА ИМЕНИ ВОРКЕРА (ИНТЕРАКТИВНАЯ) ===
        default_name = getattr(config, 'WORKER_NAME', "Unknown_Worker")
        self.worker_name = default_name

        # 1. Если запуск через аргументы (для профи/батников) - приоритет высший
        if "--worker" in sys.argv:
            try:
                idx = sys.argv.index("--worker")
                if idx + 1 < len(sys.argv):
                    self.worker_name = sys.argv[idx + 1].strip()
            except:
                pass

        # 2. Если аргументов нет - спрашиваем у пользователя (для удобства)
        else:
            print(f"\n🤖 ---------------------------------------------------")
            print(f"👋 Привет! Стандартное имя воркера: [{default_name}]")
            print(f"💡 Если это второе окно (дейлики), введи приписку (например: Daily)")
            try:
                # Ждем ввода. Если нажать Enter, suffix будет пустым.
                suffix = input("👉 Введите суффикс (или просто Enter для запуска): ").strip()
                if suffix:
                    # Если пользователь ввел "Daily", имя станет "Server_Daily"
                    if not suffix.startswith("_") and not suffix.startswith("-"):
                        suffix = "_" + suffix
                    self.worker_name = f"{default_name}{suffix}"
                    print(f"✅ Окей! Работаем под именем: [{self.worker_name}]")
                else:
                    print(f"🚀 Запускаем основу: [{self.worker_name}]")
            except Exception:
                pass
            print(f"---------------------------------------------------\n")
        # ===============================================

        self.last_action_time = time.time()

        if self.redis_url:
            try:
                self.writer = redis.Redis.from_url(self.redis_url, decode_responses=True, ssl_cert_reqs=None)
                self.reader = redis.Redis.from_url(self.redis_url, decode_responses=True, ssl_cert_reqs=None)
                self.pubsub = self.reader.pubsub()
                self.running = True
            except Exception:
                pass

        self._initialized = True

    def register_client(self, client_instance, project_name=None, stats_callback=None, progress_callback=None,
                        inventory_callback=None):
        self.active_client = client_instance

        if project_name:
            self.project_name = project_name

        if stats_callback: self.stats_callback = stats_callback
        if progress_callback: self.progress_callback = progress_callback
        if inventory_callback: self.inventory_callback = inventory_callback

        if self.running and self.project_name != "UnknownProject":
            self.start_background_tasks()

    def _mark_activity(self):
        self.last_action_time = time.time()

    def add_temp_error(self, project_name, wallet_address, log_string):
        if not self.running: return
        self._mark_activity()
        key = f"temp_errors:{project_name}:{wallet_address}"
        self.writer.rpush(key, log_string)
        self.writer.expire(key, 86400)

    def clear_temp_errors(self, project_name, wallet_address):
        if not self.running: return
        self._mark_activity()
        key = f"temp_errors:{project_name}:{wallet_address}"
        self.writer.delete(key)

    def flush_temp_errors(self, project_name, wallet_address, fallback_error=None):
        if not self.running: return "No Redis", []
        self._mark_activity()

        temp_key = f"temp_errors:{project_name}:{wallet_address}"
        logs = self.writer.lrange(temp_key, 0, -1)
        self.writer.delete(temp_key)

        if not logs and fallback_error:
            timestamp = datetime.now().strftime("%H:%M:%S")
            logs.append(f"{timestamp} | ERROR | System | {fallback_error}")

        self.writer.sadd(f"failures:{project_name}:{self.worker_name}", wallet_address)

        self.writer.hset(
            f"fail_logs:{project_name}:{self.worker_name}",
            wallet_address,
            json.dumps(logs, ensure_ascii=False)
        )

        if logs:
            last_log = logs[-1]
            parts = last_log.split(" | ")
            # Format: TIMESTAMP | LEVEL | MODULE | [POS] | MSG
            # Old Format: TIMESTAMP | LEVEL | MODULE | MSG
            
            module = "Unknown"
            msg = last_log

            if len(parts) >= 5: # New format with position
                module = parts[2].strip()
                # Part 3 is position, Part 4 is message
                msg = parts[4].strip()
                short_msg = f"<b>{module}:</b> {msg}"
            elif len(parts) >= 4: # Old format or format without position
                module = parts[2].strip()
                msg = parts[3].strip()
                short_msg = f"<b>{module}:</b> {msg}"
            else:
                short_msg = last_log
        else:
            short_msg = str(fallback_error)

        return short_msg

    # === СБОР СТАТИСТИКИ ===
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

        status = "Working 🟢"

        data = {
            "status": status,
            "current_account": getattr(c, 'address', 'Unknown'),
            "last_updated": time.time(),
            "heartbeat_threshold": HEARTBEAT_THRESHOLD,
            "pos_current": getattr(c, 'position', 0),
            "pos_total": getattr(c, 'total_accounts', 0),
            "progress": progress_str
        }
        data.update(extra_stats)
        return data

    # 🔥 ПУБЛИЧНАЯ ФУНКЦИЯ ОТПРАВКИ ЛОГА
    # (Бывшая _send_log, теперь доступна извне)
    def upload_log(self):
        if not self.running: return
        self._mark_activity()
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(base_dir, "app.log")

            text = ""
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors='replace') as f:
                    f.seek(0, os.SEEK_END)
                    seek_pos = max(0, f.tell() - 30000)
                    f.seek(seek_pos)
                    text = f"📂 ...Last 30KB of app.log:\n\n" + f.read()
            else:
                text = f"❌ Log file not found at: {log_path}"

            self.writer.publish("telegram_alerts", json.dumps({
                "type": "log_delivery",
                "project": self.project_name,
                "worker": self.worker_name,
                "text": text
            }))
        except Exception as e:
            self.send_notification("error", f"Log Error: {e}")

    def send_notification(self, type_, text, project_override=None):
        if not self.running: return
        self._mark_activity()

        proj = project_override if project_override else self.project_name
        try:
            if self.writer.get("settings:mute_all") == "1": return
            if self.writer.get(f"settings:mute:{proj}") == "1": return

            payload = {
                "type": type_, "project": proj, "worker": self.worker_name, "text": text
            }
            json_data = json.dumps(payload)
            listeners_count = self.writer.publish("telegram_alerts", json_data)

            if listeners_count == 0:
                self._fallback_send_direct(type_, proj, text)
        except Exception as e:
            if DEBUG_MODE: print(f"Send error: {e}")

    def _fallback_send_direct(self, type_, project, text):
        try:
            token = getattr(config, 'TG_BOT_TOKEN', None)
            uid = getattr(config, 'TG_USER_ID', None)
            if not token or not uid: return
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
        channel = f"cmd:{self.project_name}:{self.worker_name}"
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
                        # 👇 ОБНОВЛЕНО: Используем публичный метод
                        threading.Thread(target=self.upload_log).start()
                    elif data == "update_status":
                        stats = self._extract_stats()
                        if stats:
                            self.writer.hset(f"status:{self.project_name}", self.worker_name, json.dumps(stats))
                            self._mark_activity()
            except:
                time.sleep(1)
            time.sleep(0.1)

    def _heartbeat_loop(self):
        while self.running:
            if not ENABLE_HEARTBEAT:
                time.sleep(10)
                continue

            try:
                now = time.time()
                silence_duration = now - self.last_action_time

                if silence_duration >= HEARTBEAT_THRESHOLD:
                    if self.project_name != "UnknownProject" and self.active_client:
                        stats = self._extract_stats()
                        if stats:
                            self.writer.hset(f"status:{self.project_name}", self.worker_name, json.dumps(stats))
                            self._mark_activity()
            except Exception:
                pass
            time.sleep(30)

    def start_background_tasks(self):
        for t in threading.enumerate():
            if t.name == "BotListener":
                return

        t1 = threading.Thread(target=self._listener_loop, daemon=True, name="BotListener")
        t1.start()

        t2 = threading.Thread(target=self._heartbeat_loop, daemon=True, name="BotHeartbeat")
        t2.start()


bot_link = BotLink()