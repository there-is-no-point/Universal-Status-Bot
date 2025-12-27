import functools
import threading
import sys
import os

from .notifications import bot_link
from .stats_map import get_display_stats

try:
    from .status_manager import status_manager
except ImportError:
    from status_manager import status_manager

# --- ГЛОБАЛЬНЫЕ СЧЕТЧИКИ ---
shared_success_count = 0
shared_error_count = 0
shared_inventory = {}
counter_lock = threading.Lock()


def get_progress_string(total_accounts):
    with counter_lock:
        succ = shared_success_count
        err = shared_error_count
        total_done = succ + err
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
                progress_callback=lambda: get_progress_string(self.total_accounts),
                inventory_callback=get_global_inventory
            )

            progress_str = get_progress_string(self.total_accounts)

            # Шлем "Working" статус (тихо, только в Redis)
            status_manager.update_status(project_name, {
                "status": "Working 🟢",
                "progress": progress_str,
                "current_account": self.address
            })

            try:
                result = func(self, *args, **kwargs)

                if result is False:
                    raise Exception("Process returned False")

                # === УСПЕХ ===
                current_stats = get_display_stats(self)

                with counter_lock:
                    global shared_success_count
                    shared_success_count += 1

                    # Суммируем в общий инвентарь
                    for key, value in current_stats.items():
                        if isinstance(value, (int, float)):
                            shared_inventory[key] = shared_inventory.get(key, 0) + value

                final_progress = get_progress_string(self.total_accounts)

                # Обновляем статус в Redis (тихо)
                status_data = {
                    "status": "Sleeping 💤",
                    "progress": final_progress,
                    "current_account": self.address
                }
                status_data.update(current_stats)
                status_manager.update_status(project_name, status_data)

                # 👇 ФОРМИРУЕМ ОДНО КРАСИВОЕ СООБЩЕНИЕ
                # 1. Берем прогресс
                msg = f"Аккаунт {self.address[:6]}... завершен!\n"
                msg += f"📊 <b>Stats:</b> {final_progress}\n"

                # 2. Добавляем инвентарь (Монеты, опыт и т.д.)
                inventory_lines = []
                for k, v in current_stats.items():
                    inventory_lines.append(f"• {k}: <b>{v}</b>")

                if inventory_lines:
                    msg += "\n🎒 <b>Loot:</b>\n" + "\n".join(inventory_lines)

                # 3. Отправляем ЕДИНСТВЕННОЕ уведомление
                bot_link.send_notification("success", msg)

                return True

            except Exception as e:
                # === ОШИБКА ===
                with counter_lock:
                    global shared_error_count
                    shared_error_count += 1

                bot_link.report_error(project_name, self.address, str(e))
                error_progress = get_progress_string(self.total_accounts)

                status_manager.update_status(project_name, {
                    "status": "Error ❌",
                    "progress": error_progress,
                    "current_account": self.address
                })

                # Уведомление об ошибке тоже одно
                bot_link.send_notification("error", f"Критическая ошибка на {self.address[:8]}:\n{str(e)}")

                return False

        return wrapper

    return decorator