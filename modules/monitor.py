import functools
import threading
import sys
import os

# 👇 1. ИМПОРТ НАШЕГО МОДУЛЯ СВЯЗИ
from .notifications import bot_link

# Пытаемся импортировать status_manager
try:
    from .status_manager import status_manager
except ImportError:
    from status_manager import status_manager

# --- ГЛОБАЛЬНЫЕ СЧЕТЧИКИ ---
shared_success_count = 0
shared_error_count = 0
counter_lock = threading.Lock()


def get_progress_string(total_accounts):
    """Формирует строку: 5/10 (✅4 ❌1)"""
    with counter_lock:
        succ = shared_success_count
        err = shared_error_count
        total_done = succ + err
    return f"{total_done}/{total_accounts} (✅{succ} ❌{err})"


def monitor_account(project_name: str):
    """
    Декоратор. Вешается над process_account().
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            # 👇 2. ВНЕДРЕНИЕ: Передаем текущего клиента боту
            # Теперь notifications.py имеет доступ к self.total_coins и др.
            bot_link.register_client(self)

            # 1. ОТПРАВКА СТАТУСА "WORKING"
            progress_str = get_progress_string(self.total_accounts)

            status_manager.update_status(project_name, {
                "status": "Working 🟢",
                "progress": progress_str,
                "current_account": self.address
            })

            try:
                # === ЗАПУСК ОСНОВНОЙ ЛОГИКИ ===
                result = func(self, *args, **kwargs)
                # ==============================

                if result is False:
                    raise Exception("Process returned False")

                # 2. УСПЕХ
                with counter_lock:
                    global shared_success_count
                    shared_success_count += 1

                final_progress = get_progress_string(self.total_accounts)

                status_manager.update_status(project_name, {
                    "status": "Sleeping 💤",
                    "progress": final_progress,
                    "current_account": self.address
                })

                # Старый алерт (если был)
                status_manager.send_alert(
                    f"✅ <b>{project_name}</b> | {self.address}\nStats: {final_progress}",
                    status="Success"
                )

                # 👇 3. НОВЫЙ АЛЕРТ В ТЕЛЕГРАМ БОТА
                bot_link.send_notification("success",
                                           f"Аккаунт {self.address[:6]}... завершен!\nСтатистика: {final_progress}")

                return True

            except Exception as e:
                # 3. ОШИБКА
                with counter_lock:
                    global shared_error_count
                    shared_error_count += 1

                error_progress = get_progress_string(self.total_accounts)

                status_manager.update_status(project_name, {
                    "status": "Error ❌",
                    "progress": error_progress,
                    "current_account": self.address
                })

                status_manager.send_alert(f"❌ <b>{project_name}</b> | {self.address}\nError: {e}", status="Error")

                # 👇 4. НОВЫЙ АЛЕРТ ОБ ОШИБКЕ В ТЕЛЕГРАМ
                bot_link.send_notification("error", f"Критическая ошибка на {self.address[:8]}:\n{str(e)}")

                return False

        return wrapper

    return decorator