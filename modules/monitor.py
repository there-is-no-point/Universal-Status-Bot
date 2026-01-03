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

# Инициализация инвентаря нулями
shared_inventory = {}


class DummyClient:
    """Пустой класс-заглушка"""
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

            bot_link.register_client(
                self,
                project_name=project_name,
                progress_callback=lambda: get_progress_string(self.total_accounts),
                inventory_callback=get_global_inventory
            )

            progress_str = get_progress_string(self.total_accounts)

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
                    for key, value in current_stats.items():
                        if isinstance(value, (int, float)):
                            shared_inventory[key] = shared_inventory.get(key, 0) + value

                succ, err, total_done = get_progress_data()
                final_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"

                # Проверяем, закончили ли мы работу?
                is_finished = self.total_accounts > 0 and total_done >= self.total_accounts

                if not is_finished:
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

                # --- ЛОГИКА УВЕДОМЛЕНИЙ ---

                # 1. Формируем красивый текст для текущего аккаунта
                msg = f"Аккаунт {self.address[:6]}... завершен!\n"
                msg += f"📊 <b>Stats:</b> {final_progress}\n"
                inventory_lines = []
                for k, v in current_stats.items():
                    inventory_lines.append(f"• {k}: <b>{v}</b>")
                if inventory_lines:
                    msg += "\n🎒 <b>Loot:</b>\n" + "\n".join(inventory_lines)

                # 2. Если это ПОСЛЕДНИЙ аккаунт - шлем ФИНАЛЬНЫЙ ОТЧЕТ
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
                    # Отправляем специальный тип "worker_finished" (Он проходит через бота всегда)
                    bot_link.send_notification("worker_finished", finish_msg, project_override=project_name)

                # 3. Если работа еще идет - смотрим на настройку "Success"
                else:
                    # Читаем настройку: "1" = Detailed, "0" = Summary (тишина)
                    # По умолчанию считаем, что Detailed (1)
                    is_detailed = True
                    try:
                        val = bot_link.writer.get(f"settings:notify:{project_name}:success")
                        if val == "0": is_detailed = False
                    except:
                        pass

                    if is_detailed:
                        bot_link.send_notification("success", msg, project_override=project_name)

                return True

            except Exception as e:
                # === ОШИБКА ===
                with counter_lock:
                    global shared_error_count
                    shared_error_count += 1

                succ, err, total_done = get_progress_data()
                error_progress = f"{total_done}/{self.total_accounts} (✅{succ} ❌{err})"

                # Если закончили (даже с ошибками)
                is_finished = self.total_accounts > 0 and total_done >= self.total_accounts

                if not is_finished:
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

                # Ошибки шлем ВСЕГДА
                bot_link.send_notification("error", f"Критическая ошибка на {self.address[:8]}:\n{str(e)}",
                                           project_override=project_name)

                # Если это был последний аккаунт и он упал - тоже шлем финал
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
                    bot_link.send_notification("worker_finished", finish_msg, project_override=project_name)

                return False

        return wrapper

    return decorator