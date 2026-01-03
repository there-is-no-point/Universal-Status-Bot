import functools
import threading
import sys
import os
import time

from .notifications import bot_link
from .stats_map import get_display_stats

try:
    from .status_manager import status_manager
except ImportError:
    from status_manager import status_manager

# --- ГЛОБАЛЬНЫЕ СЧЕТЧИКИ ---
shared_success_count = 0
shared_error_count = 0

# 👇 ИЗМЕНЕНИЕ: Инициализация инвентаря нулями при старте
shared_inventory = {}


class DummyClient:
    """Пустой класс-заглушка, чтобы узнать ключи из stats_map"""
    pass


try:
    # 1. Создаем "пустого" клиента
    _dummy = DummyClient()
    # 2. Получаем шаблон статистики (вернет 0 для getattr(..., 0))
    _initial_stats = get_display_stats(_dummy)

    # 3. Заполняем глобальный инвентарь нулями
    for k, v in _initial_stats.items():
        # Берем только числа (игнорируем строки типа Twitter Username)
        if isinstance(v, (int, float)):
            shared_inventory[k] = 0
except Exception:
    # Если stats_map написан сложно и упал на заглушке - оставляем пустым
    shared_inventory = {}

counter_lock = threading.Lock()


def get_progress_data():
    """Возвращает кортеж (успех, ошибки, всего_сделано)"""
    with counter_lock:
        succ = shared_success_count
        err = shared_error_count
        total_done = succ + err
    return succ, err, total_done


def get_progress_string(total_accounts):
    succ, err, total_done = get_progress_data()
    return f"{total_done}/{total_accounts} (✅{succ} ❌{err})"


def get_global_inventory():
    """Возвращает копию словаря с СУММАРНЫМ лутом всех аккаунтов."""
    with counter_lock:
        return shared_inventory.copy()


def monitor_account(project_name: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):

            # 1. ПЕРЕДАЕМ ИМЯ ПРОЕКТА ПРИ РЕГИСТРАЦИИ
            bot_link.register_client(
                self,
                project_name=project_name,
                progress_callback=lambda: get_progress_string(self.total_accounts),
                inventory_callback=get_global_inventory
            )

            progress_str = get_progress_string(self.total_accounts)

            # Статус Working (теперь уже с нулями в инвентаре!)
            start_stats = {
                "status": "Working 🟢",
                "progress": progress_str,
                "current_account": self.address,
                "last_updated": time.time()
            }
            start_stats.update(get_global_inventory())
            status_manager.update_status(project_name, start_stats)

            try:
                result = func(self, *args, **kwargs)

                if result is False:
                    raise Exception("Process returned False")

                # === УСПЕХ ===
                current_stats = get_display_stats(self)

                with counter_lock:
                    global shared_success_count
                    shared_success_count += 1

                    # Суммируем лут
                    for key, value in current_stats.items():
                        if isinstance(value, (int, float)):
                            shared_inventory[key] = shared_inventory.get(key, 0) + value

                succ, err, total_done = get_progress_data()
                final_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"

                # Логика Sleeping/Working
                if self.total_accounts > 0 and total_done < self.total_accounts:
                    final_status = "Working 🟢"
                else:
                    final_status = "Sleeping 💤"

                end_stats = {
                    "status": final_status,
                    "progress": final_progress,
                    "current_account": self.address,
                    "last_updated": time.time()
                }
                end_stats.update(get_global_inventory())
                status_manager.update_status(project_name, end_stats)

                # Уведомление
                msg = f"Аккаунт {self.address[:6]}... завершен!\n"
                msg += f"📊 <b>Stats:</b> {final_progress}\n"

                inventory_lines = []
                for k, v in current_stats.items():
                    inventory_lines.append(f"• {k}: <b>{v}</b>")

                if inventory_lines:
                    msg += "\n🎒 <b>Loot:</b>\n" + "\n".join(inventory_lines)

                bot_link.send_notification("success", msg, project_override=project_name)

                return True

            except Exception as e:
                # === ОШИБКА ===
                with counter_lock:
                    global shared_error_count
                    shared_error_count += 1

                succ, err, total_done = get_progress_data()
                error_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"

                if self.total_accounts > 0 and total_done < self.total_accounts:
                    final_status = "Working 🟢"
                else:
                    final_status = "Errors 🔴"

                bot_link.report_error(project_name, self.address, str(e))

                error_stats = {
                    "status": final_status,
                    "progress": error_progress,
                    "current_account": self.address,
                    "last_updated": time.time()
                }
                error_stats.update(get_global_inventory())

                status_manager.update_status(project_name, error_stats)

                bot_link.send_notification("error", f"Критическая ошибка на {self.address[:8]}:\n{str(e)}",
                                           project_override=project_name)

                return False

        return wrapper

    return decorator