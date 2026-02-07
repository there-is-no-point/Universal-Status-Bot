import functools
import threading
import sys
import os
import time

# 👇 ДОБАВИЛ ИМПОРТ КОНФИГА
import config

from .notifications import bot_link
from .stats_map import get_display_stats

# 👇 БЕЗОПАСНЫЙ ИМПОРТ STATUS_MANAGER
try:
    try:
        from .status_manager import status_manager
    except ImportError:
        from status_manager import status_manager
except Exception:
    status_manager = None

# --- ГЛОБАЛЬНЫЕ СЧЕТЧИКИ ---
shared_success_count = 0
shared_error_count = 0
shared_inventory = {}


class DummyClient:
    pass


try:
    _dummy = DummyClient()
    _initial_stats = get_display_stats(_dummy)
    for k, v in _initial_stats.items():
        if isinstance(v, (int, float)):
            shared_inventory[k] = 0
except Exception:
    shared_inventory = {}

counter_lock = threading.Lock()


# === 🔥 НОВАЯ ФУНКЦИЯ: СБРОС СТАТИСТИКИ ===
def reset_global_stats():
    """Сбрасывает все счетчики в ноль (для нового цикла/дня)"""
    with counter_lock:
        global shared_success_count, shared_error_count, shared_inventory
        shared_success_count = 0
        shared_error_count = 0

        # Сбрасываем инвентарь
        try:
            _dummy = DummyClient()
            _initial = get_display_stats(_dummy)
            shared_inventory = {}
            for k, v in _initial.items():
                if isinstance(v, (int, float)):
                    shared_inventory[k] = 0
        except:
            shared_inventory = {}


# ==========================================


def get_progress_data():
    with counter_lock:
        succ = shared_success_count
        err = shared_error_count
        total_done = succ + err
    return succ, err, total_done


def get_progress_string(total_accounts):
    succ, err, total_done = get_progress_data()
    return f"{total_done}/{total_accounts} (✅{succ} ❌{err})"


def get_global_inventory():
    with counter_lock:
        return shared_inventory.copy()


def monitor_account(project_name: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):

            # === 🛑 ГЛАВНАЯ ПРОВЕРКА: ЕСЛИ БОТ ВЫКЛЮЧЕН ===
            if not getattr(config, 'USE_TG_BOT', False):
                return func(self, *args, **kwargs)
            # ===============================================

            # === 🔥 ЛОГИКА АВТО-СБРОСА (SELF-CLEANING) ===
            current_pos = getattr(self, 'position', 0)
            _, _, current_total_done = get_progress_data()

            # 1. Если это ПЕРВЫЙ аккаунт в списке -> Значит новый запуск/день
            is_start_of_cycle = (current_pos == 1)

            # 2. Если мы сделали >= 100% и продолжаем -> Значит новый круг
            is_overflow = (self.total_accounts > 0 and current_total_done >= self.total_accounts)

            if is_start_of_cycle or is_overflow:
                if current_total_done > 0:
                    reset_global_stats()
            # ===============================================

            bot_link.register_client(
                self,
                project_name=project_name,
                progress_callback=lambda: get_progress_string(self.total_accounts),
                inventory_callback=get_global_inventory
            )

            progress_str = get_progress_string(self.total_accounts)

            if status_manager:
                try:
                    start_stats = {
                        "status": "Working 🟢",
                        "progress": progress_str,
                        "current_account": self.address,
                        "last_updated": time.time()
                    }
                    start_stats.update(get_global_inventory())
                    status_manager.update_status(project_name, start_stats)
                except Exception:
                    pass

            try:
                result = func(self, *args, **kwargs)

                if result is False:
                    raise Exception("Process returned False")

                # === УСПЕХ ===

                try:
                    bot_link.clear_temp_errors(project_name, self.address)
                except:
                    pass

                current_stats = get_display_stats(self)

                with counter_lock:
                    global shared_success_count
                    shared_success_count += 1
                    for key, value in current_stats.items():
                        if isinstance(value, (int, float)):
                            shared_inventory[key] = shared_inventory.get(key, 0) + value

                succ, err, total_done = get_progress_data()
                final_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"

                is_finished = self.total_accounts > 0 and total_done >= self.total_accounts
                final_status = "Working 🟢" if not is_finished else "Sleeping 💤"

                if status_manager:
                    try:
                        end_stats = {
                            "status": final_status,
                            "progress": final_progress,
                            "current_account": self.address,
                            "last_updated": time.time()
                        }
                        end_stats.update(get_global_inventory())
                        status_manager.update_status(project_name, end_stats)
                    except:
                        pass

                msg = f"Аккаунт {self.address[:6]}... завершен!\n"
                msg += f"📊 <b>Stats:</b> {final_progress}\n"
                inventory_lines = []
                for k, v in current_stats.items():
                    inventory_lines.append(f"• {k}: <b>{v}</b>")
                if inventory_lines:
                    msg += "\n🎒 <b>Loot:</b>\n" + "\n".join(inventory_lines)

                is_detailed = True
                try:
                    if hasattr(bot_link, 'writer') and bot_link.writer:
                        val = bot_link.writer.get(f"settings:notify:{project_name}:success")
                        if val == "0": is_detailed = False
                except:
                    pass

                if is_detailed:
                    bot_link.send_notification("success", msg, project_override=project_name)

                if is_finished:
                    total_inv_lines = []
                    gl_inv = get_global_inventory()
                    for k, v in gl_inv.items():
                        total_inv_lines.append(f"• {k}: <b>{v}</b>")

                    finish_msg = (
                            f"🎉 <b>WORKER FINISHED!</b>\n"
                            f"Все аккаунты отработаны.\n\n"
                            f"📊 <b>Final Result:</b> {final_progress}\n"
                            f"🎒 <b>Total Loot:</b>\n" + "\n".join(total_inv_lines)
                    )
                    time.sleep(0.5)
                    bot_link.send_notification("worker_finished", finish_msg, project_override=project_name)

                    # 🔥 АВТО-ОТПРАВКА ЛОГА ПРИ УСПЕХЕ (ЕСЛИ ВКЛЮЧЕНО В КОНФИГЕ)
                    # По умолчанию: False (не спамить)
                    if getattr(config, 'SEND_LOG_ON_SUCCESS', False):
                        bot_link.upload_log()
                        time.sleep(1)

                    reset_global_stats()

                return True

            except Exception as e:
                # === ОШИБКА ===
                with counter_lock:
                    global shared_error_count
                    shared_error_count += 1

                succ, err, total_done = get_progress_data()
                error_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"
                is_finished = self.total_accounts > 0 and total_done >= self.total_accounts
                final_status = "Working 🟢" if not is_finished else "Errors 🔴"

                try:
                    error_summary = bot_link.flush_temp_errors(project_name, self.address, fallback_error=str(e))
                except:
                    error_summary = str(e)

                if status_manager:
                    try:
                        error_stats = {
                            "status": final_status,
                            "progress": error_progress,
                            "current_account": self.address,
                            "last_updated": time.time(),
                            "error": error_summary
                        }
                        error_stats.update(get_global_inventory())
                        status_manager.update_status(project_name, error_stats)
                    except:
                        pass

                bot_link.send_notification("error", f"❌ <b>FAILED:</b> {self.address[:8]}...\n\n{error_summary}",
                                           project_override=project_name)

                if is_finished:
                    total_inv_lines = []
                    gl_inv = get_global_inventory()
                    for k, v in gl_inv.items():
                        total_inv_lines.append(f"• {k}: <b>{v}</b>")

                    finish_msg = (
                            f"🏁 <b>WORKER STOPPED (With Errors)</b>\n"
                            f"Проход завершен.\n\n"
                            f"📊 <b>Final Result:</b> {error_progress}\n"
                            f"🎒 <b>Total Loot:</b>\n" + "\n".join(total_inv_lines)
                    )
                    time.sleep(0.5)
                    bot_link.send_notification("worker_finished", finish_msg, project_override=project_name)

                    # 🔥 АВТО-ОТПРАВКА ЛОГА ПРИ ОШИБКЕ (ЕСЛИ ВКЛЮЧЕНО В КОНФИГЕ)
                    # По умолчанию: True (нужно видеть ошибки)
                    if getattr(config, 'SEND_LOG_ON_ERROR', True):
                        bot_link.upload_log()
                        time.sleep(1)

                    reset_global_stats()

                return False

        return wrapper

    return decorator