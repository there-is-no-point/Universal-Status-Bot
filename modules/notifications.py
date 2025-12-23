import sys

# 👇 ГЛАВНЫЙ ХАК: Отключаем hiredis (C-ускоритель) до любых импортов.
sys.modules['hiredis'] = None

import redis
import json
import threading
import time
import os
from datetime import datetime

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
        self.stats_callback = None  # 👈 Сюда мы положим функцию из stats_map.py
        self.running = False
        self.worker_name = getattr(config, 'WORKER_NAME', "Linux Notebook")

        if self.redis_url:
            try:
                # 1. WRITER
                self.writer = redis.Redis.from_url(self.redis_url, decode_responses=True)
                # 2. READER
                self.reader = redis.Redis.from_url(self.redis_url, decode_responses=True)
                self.pubsub = self.reader.pubsub()

                self.running = True
                self.start_listener()
                print(f"✅ [BotLink] Redis подключен (Worker: {self.worker_name})")
            except Exception as e:
                print(f"❌ [BotLink] Ошибка подключения: {e}")

        self._initialized = True

    # 👇 ОБНОВЛЕННЫЙ МЕТОД РЕГИСТРАЦИИ
    def register_client(self, client_instance, stats_callback=None):
        """
        client_instance: Сам объект класса (self)
        stats_callback: Функция из stats_map.py, которая превращает клиента в словарь
        """
        self.active_client = client_instance
        self.stats_callback = stats_callback

    def _extract_stats(self):
        """Универсальный сборщик статистики"""
        if not self.active_client: return None
        c = self.active_client

        # 1. Используем переданную функцию-маппер (из stats_map.py)
        extra_stats = {}
        if self.stats_callback:
            try:
                extra_stats = self.stats_callback(c)
            except Exception as e:
                print(f"⚠️ Ошибка в stats_map: {e}")

        # 2. Формируем базовый пакет
        pos = getattr(c, 'position', 0)
        total = getattr(c, 'total_accounts', 0)

        data = {
            "status": "Working 🟢",
            "current_account": getattr(c, 'address', 'Unknown'),
            "last_updated": datetime.now().strftime("%H:%M:%S"),
            "pos_current": pos,
            "pos_total": total,
        }

        # 3. Подмешиваем кастомные поля
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
                text = f"❌ Файл app.log не найден."

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
        except: pass

    def _listener_loop(self):
        channel = f"cmd:HackQuest:{self.worker_name}"
        try:
            self.pubsub.subscribe(channel)
            print(f"📡 [BotLink] Слушаю канал: {channel}")
        except: return

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
            except: time.sleep(1)
            time.sleep(0.1)

    def start_listener(self):
        t = threading.Thread(target=self._listener_loop, daemon=True)
        t.start()

bot_link = BotLink()