import asyncio
import json
import redis
import re
import io
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
import config

# --- НАСТРОЙКИ ---
bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher()

# ⏱ ДЕФОЛТНЫЙ ЛИМИТ ТИШИНЫ (Если воркер не прислал свой)
# Используется как запасной вариант для старых версий софта
DEFAULT_OFFLINE_TIMEOUT = 900  # 15 минут

# 🛡 БУФЕР БЕЗОПАСНОСТИ (в секундах)
# Добавляем это время к таймеру воркера, чтобы избежать ложных срабатываний
# из-за задержек сети или рассинхрона.
SAFETY_BUFFER = 300  # 5 минут

try:
    r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True, ssl_cert_reqs=None)
    r.ping()
    print("✅ Бот успешно подключен к Redis")
except Exception as e:
    print(f"❌ Ошибка Redis: {e}")
    exit(1)


# === 🛡 ЛОГИКА ПРОВЕРКИ УВЕДОМЛЕНИЙ ===
def is_notification_enabled(project_name: str, msg_type: str) -> bool:
    if msg_type == "worker_finished" or msg_type == "log_delivery":
        if r.get("settings:mute_all") == "1": return False
        if r.get(f"settings:mute:{project_name}") == "1": return False
        return True

    if "log" in msg_type:
        check_type = "log"
    elif "error" in msg_type:
        check_type = "error"
    elif "success" in msg_type:
        check_type = "success"
    else:
        check_type = "info"

    proj_setting = r.get(f"settings:notify:{project_name}:{check_type}")
    if proj_setting is not None: return proj_setting == "1"

    global_setting = r.get(f"settings:notify:GLOBAL:{check_type}")
    if global_setting is not None: return global_setting == "1"

    return True


# === ФОНОВАЯ ЗАДАЧА: СЛУШАТЕЛЬ ===
async def alert_listener():
    pubsub = r.pubsub()
    pubsub.subscribe("telegram_alerts")
    print("📡 Alert Listener запущен...")

    while True:
        try:
            message = pubsub.get_message(ignore_subscribe_messages=True)
            if message:
                data = json.loads(message['data'])
                msg_type = data.get("type", "info")
                worker = data.get("worker")
                project = data.get("project")
                text = data.get("text")

                if is_notification_enabled(project, msg_type):
                    header = f"🤖 <b>{project}</b> | {worker}"

                    if msg_type == "error":
                        await bot.send_message(config.TG_USER_ID, f"🔴 <b>ALARM:</b>\n{header}\n\n{text}",
                                               parse_mode="HTML")
                    elif msg_type == "success":
                        await bot.send_message(config.TG_USER_ID, f"✅ <b>FINISHED:</b>\n{header}\n\n{text}",
                                               parse_mode="HTML")
                    elif msg_type == "worker_finished":
                        await bot.send_message(config.TG_USER_ID, f"🏁 <b>JOB COMPLETED:</b>\n{header}\n\n{text}",
                                               parse_mode="HTML")
                    elif msg_type == "log_delivery":
                        file_obj = io.BytesIO(text.encode('utf-8'))
                        file_obj.name = f"log_{worker}_{datetime.now().strftime('%H-%M')}.txt"
                        input_file = BufferedInputFile(file_obj.getvalue(), filename=file_obj.name)
                        await bot.send_document(config.TG_USER_ID, document=input_file,
                                                caption=f"📄 <b>Log Received</b>\n{header}", parse_mode="HTML")

            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Listener Error: {e}")
            await asyncio.sleep(5)


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ UI ---
def get_status_emoji(status_text: str) -> str:
    st = status_text.lower()
    if "working" in st: return "🟢"
    if "error" in st: return "🔴"
    if "done" in st: return "🏁"
    if "sleep" in st: return "💤"
    if "stop" in st: return "🛑"
    return "⚪️"


def parse_progress(stats: dict):
    prog_str = str(stats.get('progress', ''))
    match = re.search(r"(\d+)/(\d+).*?✅\s*(\d+).*?❌\s*(\d+)", prog_str)
    if match:
        return {
            "type": "detailed", "total": int(match.group(2)),
            "success": int(match.group(3)), "fails": int(match.group(4)),
            "done": int(match.group(3)) + int(match.group(4))
        }
    pos_current = stats.get('pos_current')
    pos_total = stats.get('pos_total')
    if pos_current and pos_total:
        return {"type": "simple", "current": int(pos_current), "total": int(pos_total)}
    return None


def make_progress_bar(current, total, length=10):
    if total == 0: return f"[{'□' * length}]"
    percent = current / total
    if percent > 1: percent = 1
    filled = int(length * percent)
    return '■' * filled + '□' * (length - filled)


def format_time_data(raw_time):
    try:
        now = datetime.now()
        if isinstance(raw_time, (int, float)):
            dt = datetime.fromtimestamp(raw_time)
        else:
            if len(str(raw_time)) <= 8:
                dt = datetime.strptime(raw_time, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)
            else:
                dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
        diff_seconds = (now - dt).total_seconds()
        m = int(diff_seconds / 60)
        time_str = dt.strftime("%H:%M:%S")
        ago_str = "(сейчас)" if m < 1 else f"({m} мин.)"
        return time_str, ago_str
    except:
        return str(raw_time), ""


async def safe_edit_text(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()
    except TelegramBadRequest:
        await callback.answer()


# ==========================================
# 👇 ГЛАВНОЕ МЕНЮ
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if str(message.from_user.id) != str(config.TG_USER_ID): return
    await show_start_menu(message)


async def show_start_menu(message_or_call):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📂 Проекты", callback_data="menu_projects"))
    builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings"))
    builder.row(InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu_about"))

    text = "🤖 <b>Universal Status Bot</b>\nДобро пожаловать в центр управления."

    if isinstance(message_or_call, types.Message):
        await message_or_call.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    elif isinstance(message_or_call, types.CallbackQuery):
        await safe_edit_text(message_or_call, text, builder.as_markup())


@dp.callback_query(F.data == "menu_start")
async def back_to_start_handler(callback: CallbackQuery):
    await show_start_menu(callback)


# ==========================================
# 👇 МЕНЮ ПРОЕКТОВ
# ==========================================
@dp.callback_query(F.data == "menu_projects")
async def show_projects_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    keys = r.keys("status:*")

    stats_list = []

    if not keys:
        text = "📂 <b>Активные проекты</b>\n\n(Список пуст)"
        builder.row(InlineKeyboardButton(text="♻️ Обновить", callback_data="menu_projects"))
        builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_start"))
        await safe_edit_text(callback, text, builder.as_markup())
        return

    now = time.time()

    for key in keys:
        parts = key.split(":")
        if len(parts) < 2: continue
        proj_name = parts[1]

        active = 0
        errors = 0
        sleep = 0
        total_scale_accs = 0
        max_ts = 0.0

        try:
            workers_data = r.hgetall(key)
            for _, w_json in workers_data.items():
                w_stats = json.loads(w_json)
                st = str(w_stats.get("status", "")).lower()
                ts = float(w_stats.get("last_updated", 0))
                if ts > max_ts: max_ts = ts

                # Читаем настройку тайм-аута от самого воркера
                w_heartbeat = int(w_stats.get("heartbeat_threshold", DEFAULT_OFFLINE_TIMEOUT))
                # Рассчитываем динамический лимит: Время воркера + Буфер 5 мин
                dynamic_limit = w_heartbeat + SAFETY_BUFFER

                acc_count = int(w_stats.get("pos_total", 0))
                total_scale_accs += acc_count

                # 🔥 ПРОВЕРКА НА ОФФЛАЙН (ДИНАМИЧЕСКАЯ)
                is_working_state = "working" in st or "active" in st
                time_diff = now - ts

                if is_working_state and time_diff > dynamic_limit:
                    errors += 1
                elif "error" in st or "fail" in st:
                    errors += 1
                elif is_working_state:
                    active += 1
                else:
                    sleep += 1
        except:
            continue

        stats_list.append({
            "name": proj_name,
            "active": active, "errors": errors, "sleep": sleep,
            "last_ts": max_ts,
            "scale": total_scale_accs
        })

    sort_mode = r.get("settings:sort_proj") or "scale"

    if sort_mode == "scale":
        stats_list.sort(key=lambda x: x["scale"], reverse=True)
    elif sort_mode == "latest":
        stats_list.sort(key=lambda x: x["last_ts"], reverse=True)
    else:
        stats_list.sort(key=lambda x: x["name"])

    text = f"📂 <b>Проекты</b> (Sort: {sort_mode.title()})\nВыберите проект:"

    for item in stats_list:
        btn_text = f"🔹 {item['name']} (🟢{item['active']} | 💤{item['sleep']} | 🔴{item['errors']})"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"proj_{item['name']}"))

    builder.row(InlineKeyboardButton(text="♻️ Обновить", callback_data="menu_projects"))
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_start"))

    await safe_edit_text(callback, text, builder.as_markup())


# ==========================================
# 👇 СПИСОК ВОРКЕРОВ
# ==========================================
@dp.callback_query(F.data.startswith("proj_"))
async def show_devices(callback: CallbackQuery):
    project_name = callback.data.split("_")[1]
    devices_data = r.hgetall(f"status:{project_name}")
    builder = InlineKeyboardBuilder()

    dev_list = []
    now = time.time()

    if devices_data:
        for dev_name, json_str in devices_data.items():
            try:
                stats = json.loads(json_str)
                st = str(stats.get("status", "Unknown")).lower()
                ts = float(stats.get("last_updated", 0))

                # Читаем настройку тайм-аута от воркера
                w_heartbeat = int(stats.get("heartbeat_threshold", DEFAULT_OFFLINE_TIMEOUT))
                dynamic_limit = w_heartbeat + SAFETY_BUFFER

                time_diff = now - ts
                is_working_state = "working" in st or "active" in st

                # 🔥 Логика Offline
                if is_working_state and time_diff > dynamic_limit:
                    st = "offline 🔇"
                    emoji = "🔴"
                    is_active = False
                    is_error = True
                else:
                    is_active = is_working_state
                    is_error = "error" in st or "fail" in st
                    emoji = get_status_emoji(st)

                dev_list.append({
                    "name": dev_name, "emoji": emoji, "status_raw": st,
                    "ts": ts, "is_error": is_error, "is_active": is_active
                })
            except:
                continue

    sort_mode = r.get("settings:sort_dev") or "priority"
    if sort_mode == "priority":
        dev_list.sort(key=lambda x: (x["is_error"], x["is_active"], x["name"]), reverse=True)
    elif sort_mode == "latest":
        dev_list.sort(key=lambda x: x["ts"], reverse=True)
    else:
        dev_list.sort(key=lambda x: x["name"])

    for item in dev_list:
        btn_txt = f"{item['emoji']} {item['name']} | {item['status_raw'].title()}"
        builder.row(InlineKeyboardButton(text=btn_txt, callback_data=f"dev_{project_name}|{item['name']}"))

    active = sum(1 for x in dev_list if x['is_active'])
    errors = sum(1 for x in dev_list if x['is_error'])
    sleep = len(dev_list) - active - errors

    text = f"📂 <b>Project: {project_name}</b>\n🟢 Active: {active} | 💤 Sleep: {sleep} | 🔴 Problems: {errors}"
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_projects"))
    await safe_edit_text(callback, text, builder.as_markup())


# ==========================================
# 👇 ДЕТАЛЬНАЯ СТРАНИЦА
# ==========================================
@dp.callback_query(F.data.startswith("dev_"))
async def show_stats_handler(callback: CallbackQuery):
    _, payload = callback.data.split("_", 1)
    project, device = payload.split("|")
    await render_device_page(callback, project, device)


async def render_device_page(callback: CallbackQuery, project_name: str, device_name: str):
    json_str = r.hget(f"status:{project_name}", device_name)
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(text="📥 Get Log", callback_data=f"cmd_log_{project_name}|{device_name}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"force_update_{project_name}|{device_name}")
    )
    fail_count = r.scard(f"failures:{project_name}:{device_name}")
    btn_text = f"📄 Failed Wallets ({fail_count})" if fail_count > 0 else "📄 Failed Wallets"
    builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"fails_{project_name}|{device_name}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"proj_{project_name}"))

    if not json_str:
        await safe_edit_text(callback, "❌ Данные потеряны", reply_markup=builder.as_markup())
        return

    stats = json.loads(json_str)
    st = stats.get('status', 'Unknown')
    acc = stats.get('current_account', 'N/A')

    last_ts = float(stats.get('last_updated', 0))
    nice_time, time_ago = format_time_data(last_ts)

    # Читаем настройку тайм-аута от воркера
    w_heartbeat = int(stats.get("heartbeat_threshold", DEFAULT_OFFLINE_TIMEOUT))
    dynamic_limit = w_heartbeat + SAFETY_BUFFER

    # 🔥 ПРОВЕРКА НА OFFLINE
    now = time.time()
    is_silent = (now - last_ts) > dynamic_limit
    is_working_state = "working" in st.lower() or "active" in st.lower()

    if is_working_state and is_silent:
        header_emoji = "🔇"
        status_line = f"🔥 <b>STATUS:</b> {header_emoji} <b>OFFLINE / CRASHED</b>"

        minutes_silent = int((now - last_ts) / 60)
        minutes_limit = int(dynamic_limit / 60)

        warning_add = f"\n⚠️ <b>Внимание:</b> Сигнала нет уже {minutes_silent} мин.\n(Лимит ожидания: {minutes_limit} мин)\nСкорее всего процесс был убит."
    else:
        header_emoji = get_status_emoji(st)
        status_line = f"🔥 <b>STATUS:</b> {header_emoji} {st.upper()}"
        warning_add = ""

    msg = f"🖥 <b>Worker:</b> {device_name}\n━━━━━━━━━━━━━━━━━━\n"
    msg += f"{status_line}\n"
    msg += f"⏰ <b>Last Signal:</b> {nice_time} <i>{time_ago}</i>{warning_add}\n\n"

    if acc and len(acc) > 15 and " " not in acc:
        msg += f"👤 <b>Active:</b> <code>{acc[:6]}...{acc[-4:]}</code>\n\n"
    elif acc != "N/A":
        msg += f"👤 <b>Active:</b> <code>{acc}</code>\n\n"

    parsed = parse_progress(stats)
    if parsed and parsed['type'] == 'detailed':
        bar = make_progress_bar(parsed['done'], parsed['total'])
        percent = int((parsed['done'] / parsed['total']) * 100) if parsed['total'] else 0
        msg += f"📊 <b>PROGRESS:</b>\n<code>[{bar}] {percent}%</code>\n"
        msg += f"📦 Total: {parsed['total']} | ✅ {parsed['success']} | ❌ {parsed['fails']}\n\n"
    elif parsed:
        bar = make_progress_bar(0, parsed['total'])
        msg += f"📊 <b>PROGRESS:</b>\n<code>[{bar}] 0%</code>\n📦 Total: {parsed['total']}\n\n"

    exclude = ["status", "current_account", "last_updated", "progress", "error", "pos_current", "pos_total",
               "heartbeat_threshold"]
    extras = []
    for k in sorted(stats.keys()):
        if k not in exclude:
            nice = k.replace("_", " ").title() if "_" in k else k
            extras.append(f"• {nice}: <b>{stats[k]}</b>")
    if extras: msg += f"🎒 <b>Inventory:</b>\n" + "\n".join(extras) + "\n"

    if stats.get("error") and "Error" in st:
        msg += f"\n‼️ <b>CRITICAL ERROR:</b>\n<pre>{stats.get('error')}</pre>"

    await safe_edit_text(callback, msg, builder.as_markup())


# === МЕНЮ НАСТРОЕК (БЕЗ ИЗМЕНЕНИЙ) ===
@dp.callback_query(F.data == "menu_settings")
async def render_settings_root(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings_notify_list"))
    builder.row(InlineKeyboardButton(text="🗂 Сортировка", callback_data="settings_sorting_menu"))
    builder.row(InlineKeyboardButton(text="🗑 Управление данными", callback_data="settings_data"))
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_start"))
    text = "⚙️ <b>Настройки</b>\n\nВыберите категорию:"
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data == "settings_notify_list")
async def settings_notify_list(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 Глобальные (шаблон)", callback_data="notify_edit_GLOBAL"))
    keys = r.keys("status:*")
    projects = set()
    for k in keys:
        parts = k.split(":")
        if len(parts) > 1: projects.add(parts[1])
    if projects:
        for proj in sorted(projects):
            builder.row(InlineKeyboardButton(text=f"🔹 {proj}", callback_data=f"notify_edit_{proj}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"))
    text = "🔔 <b>Настройка уведомлений</b>\nГлобальный шаблон (сверху) при изменении обновляет настройки всех проектов."
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data.startswith("notify_edit_"))
async def notify_edit_handler(callback: CallbackQuery, target_override=None):
    if target_override:
        target = target_override
    else:
        target = callback.data.replace("notify_edit_", "")
    builder = InlineKeyboardBuilder()

    def get_state(t):
        val = r.get(f"settings:notify:{target}:{t}")
        if val is None: return True if target == "GLOBAL" else None
        return val == "1"

    success_state = get_state("success")
    if success_state is None:
        btn_text, action = "🔗 Success: Detailed", "0"
    elif success_state:
        btn_text, action = "✅ Success: Detailed", "0"
    else:
        btn_text, action = "📉 Success: Summary Only", "1"
    builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"notify_set_{target}|success|{action}"))

    for t_code, t_name in [("error", "❌ Errors"), ("log", "📄 Logs")]:
        state = get_state(t_code)
        if state is None:
            icon, act = "🔗", "1"
        elif state:
            icon, act = "🔔", "0"
        else:
            icon, act = "🔕", "1"
        builder.row(InlineKeyboardButton(text=f"{icon} {t_name}", callback_data=f"notify_set_{target}|{t_code}|{act}"))

    if target != "GLOBAL": builder.row(
        InlineKeyboardButton(text="🔄 Сбросить на шаблон", callback_data=f"notify_reset_{target}"))
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="settings_notify_list"))
    target_display = "Глобальные настройки" if target == "GLOBAL" else f"Проект: {target}"
    desc = "⚙️ <b>Режимы:</b>\n✅ <b>Detailed:</b> Всё.\n📉 <b>Summary:</b> Только финал.\n❌ <b>Errors:</b> Всегда."
    await safe_edit_text(callback, f"⚙️ <b>{target_display}</b>\n\n{desc}", builder.as_markup())


@dp.callback_query(F.data.startswith("notify_set_"))
async def notify_set_action(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    target, t_code, val = payload.split("|")
    r.set(f"settings:notify:{target}:{t_code}", val)
    if target == "GLOBAL":
        keys = r.keys("status:*")
        projs = set(k.split(":")[1] for k in keys if len(k.split(":")) > 1)
        for proj in projs: r.set(f"settings:notify:{proj}:{t_code}", val)
    await notify_edit_handler(callback, target_override=target)


@dp.callback_query(F.data.startswith("notify_reset_"))
async def notify_reset_action(callback: CallbackQuery):
    target = callback.data.replace("notify_reset_", "")
    for t in ["success", "error", "log"]: r.delete(f"settings:notify:{target}:{t}")
    await notify_edit_handler(callback, target_override=target)


@dp.callback_query(F.data == "settings_sorting_menu")
async def settings_sorting_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📂 Сортировка ПРОЕКТОВ", callback_data="sort_menu_proj"))
    builder.row(InlineKeyboardButton(text="🖥 Сортировка ВОРКЕРОВ", callback_data="sort_menu_dev"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"))
    await safe_edit_text(callback, "🗂 <b>Меню сортировки</b>", builder.as_markup())


@dp.callback_query(F.data.startswith("sort_menu_"))
async def render_sort_options(callback: CallbackQuery, target_override=None):
    if target_override:
        target = target_override
    else:
        target = callback.data.split("_")[2]
    builder = InlineKeyboardBuilder()
    current = r.get(f"settings:sort_{target}")
    if not current: current = "scale" if target == "proj" else "priority"
    modes = [("scale", "📊 По масштабу"), ("latest", "🕒 По свежести"), ("az", "🔤 По имени")] if target == "proj" else [
        ("priority", "⚡️ Умный приоритет"), ("latest", "🕒 По свежести"), ("az", "🔤 По имени")]
    for code, label in modes:
        prefix = "✅ " if current == code else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_sort_{target}|{code}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="settings_sorting_menu"))
    await safe_edit_text(callback, f"🗂 Сортировка <b>{target}</b>", builder.as_markup())


@dp.callback_query(F.data.startswith("set_sort_"))
async def save_sort_mode(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    target, mode = payload.split("|")
    r.set(f"settings:sort_{target}", mode)
    await callback.answer("Сохранено!")
    await render_sort_options(callback, target_override=target)


@dp.callback_query(F.data == "settings_data")
async def render_data_page(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💾 Бэкап базы (JSON)", callback_data="data_backup"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить воркеров (Вручную)", callback_data="data_prune_select_proj"))
    builder.row(InlineKeyboardButton(text="🧹 Сбросить ошибки", callback_data="data_clear_errors_menu"))
    builder.row(InlineKeyboardButton(text="💣 Полный сброс", callback_data="data_factory_reset_confirm"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings"))
    text = "🗑 <b>Управление данными</b>\nВыберите действие:"
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data == "data_backup")
async def data_backup_handler(callback: CallbackQuery):
    await callback.answer("⏳ Собираю данные...", show_alert=False)
    all_data = {}
    for pattern in ["status:*", "failures:*", "fail_logs:*", "settings:*"]:
        keys = r.keys(pattern)
        for k in keys:
            if r.type(k) == 'string':
                all_data[k] = r.get(k)
            elif r.type(k) == 'hash':
                all_data[k] = r.hgetall(k)
            elif r.type(k) == 'set':
                all_data[k] = list(r.smembers(k))
    file_content = json.dumps(all_data, indent=4, ensure_ascii=False)
    fobj = io.BytesIO(file_content.encode('utf-8'))
    fobj.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    await callback.message.answer_document(BufferedInputFile(fobj.getvalue(), filename=fobj.name),
                                           caption="💾 <b>Полный бэкап</b>")
    await callback.answer()


@dp.callback_query(F.data == "data_prune_select_proj")
async def data_prune_select_proj(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    keys = r.keys("status:*")
    projs = set(k.split(":")[1] for k in keys if len(k.split(":")) > 1)
    if not projs:
        await callback.answer("Нет данных", show_alert=True)
        return
    for p in sorted(projs): builder.row(InlineKeyboardButton(text=f"📂 {p}", callback_data=f"data_prune_list_{p}"))
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="settings_data"))
    await safe_edit_text(callback, "🗑 <b>Удаление воркеров</b>\nВ каком проекте чистим?", builder.as_markup())


@dp.callback_query(F.data.startswith("data_prune_list_"))
async def data_prune_list_worker(callback: CallbackQuery):
    proj = callback.data.replace("data_prune_list_", "")
    builder = InlineKeyboardBuilder()
    workers = r.hgetall(f"status:{proj}")
    if not workers:
        await callback.answer("В проекте нет воркеров", show_alert=True)
        return
    now = time.time()
    sorted_workers = []
    for w_name, w_json in workers.items():
        try:
            stats = json.loads(w_json)
            last_ts = float(stats.get("last_updated", 0))
            diff = now - last_ts
            hours = int(diff / 3600)
            sorted_workers.append((w_name, hours))
        except:
            continue
    sorted_workers.sort(key=lambda x: x[1], reverse=True)
    for name, hrs in sorted_workers:
        ago_text = f"{hrs}ч" if hrs < 240 else ">10д"
        builder.row(InlineKeyboardButton(text=f"❌ {name} ({ago_text})", callback_data=f"data_do_del_{proj}|{name}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="data_prune_select_proj"))
    await safe_edit_text(callback, f"🗑 <b>{proj}</b>\nНажмите, чтобы удалить воркера.", builder.as_markup())


@dp.callback_query(F.data.startswith("data_do_del_"))
async def data_do_del_worker(callback: CallbackQuery):
    payload = callback.data.replace("data_do_del_", "")
    if "|" in payload:
        proj, name = payload.split("|", 1)
        r.hdel(f"status:{proj}", name)
        await callback.answer(f"Воркер {name} удален!", show_alert=True)

        class FakeCallback:
            def __init__(self, original, new_data):
                self.original = original
                self.data = new_data
                self.message = original.message
                self.answer = original.answer

            def __getattr__(self, name): return getattr(self.original, name)

        await data_prune_list_worker(FakeCallback(callback, f"data_prune_list_{proj}"))
    else:
        await callback.answer("Ошибка структуры данных", show_alert=True)


@dp.callback_query(F.data == "data_clear_errors_menu")
async def data_clear_errors_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 Сбросить ВЕЗДЕ", callback_data="data_clear_errors_all"))
    keys = r.keys("status:*")
    projs = set(k.split(":")[1] for k in keys if len(k.split(":")) > 1)
    if projs:
        builder.row(InlineKeyboardButton(text="👇 Выбрать проект 👇", callback_data="ignore"))
        for p in sorted(projs): builder.row(InlineKeyboardButton(text=f"🔸 {p}", callback_data=f"data_clear_errors_{p}"))
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="settings_data"))
    await safe_edit_text(callback, "🧹 <b>Сброс ошибок</b>\nЭто удалит логи ошибок.\nГде чистим?", builder.as_markup())


@dp.callback_query(F.data.startswith("data_clear_errors_"))
async def data_clear_errors_action(callback: CallbackQuery):
    target = callback.data.replace("data_clear_errors_", "")
    if target == "all":
        keys_list = r.keys("failures:*")
        keys_logs = r.keys("fail_logs:*")
        keys_temp = r.keys("temp_errors:*")
        count = len(keys_list) + len(keys_logs) + len(keys_temp)
        if keys_list: r.delete(*keys_list)
        if keys_logs: r.delete(*keys_logs)
        if keys_temp: r.delete(*keys_temp)
        msg = f"Очищено ({count})."
    else:
        keys_list = r.keys(f"failures:{target}:*")
        keys_logs = r.keys(f"fail_logs:{target}:*")
        keys_temp = r.keys(f"temp_errors:{target}:*")
        count = len(keys_list) + len(keys_logs) + len(keys_temp)
        if keys_list: r.delete(*keys_list)
        if keys_logs: r.delete(*keys_logs)
        if keys_temp: r.delete(*keys_temp)
        msg = f"Очищен {target}."
    await callback.answer(msg, show_alert=True)
    await render_data_page(callback)


@dp.callback_query(F.data == "data_factory_reset_confirm")
async def data_factory_reset_confirm(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ ДА, УДАЛИТЬ ВСЁ", callback_data="data_factory_reset_do"))
    builder.row(InlineKeyboardButton(text="🔙 НЕТ, ОТМЕНА", callback_data="settings_data"))
    await safe_edit_text(callback, "⚠️ <b>Вы уверены?</b>\nЭто удалит ВСЕ статусы, логи и настройки.",
                         builder.as_markup())


@dp.callback_query(F.data == "data_factory_reset_do")
async def data_factory_reset_do(callback: CallbackQuery):
    for pattern in ["status:*", "failures:*", "fail_logs:*", "settings:*", "temp_errors:*"]:
        keys = r.keys(pattern)
        if keys: r.delete(*keys)
    await callback.answer("♻️ Бот полностью сброшен.", show_alert=True)
    await show_start_menu(callback)


@dp.callback_query(F.data == "menu_about")
async def show_about(callback: CallbackQuery):
    text = "ℹ️ <b>О боте</b>\n\n<b>Universal Status Bot</b>\nЦентрализованная система мониторинга.\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🐙 GitHub Repository",
                                     url="https://github.com/there-is-no-point/Universal-Status-Bot"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_start"))
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data.startswith("fails_"))
async def show_fails_menu(callback: CallbackQuery):
    _, payload = callback.data.split("_", 1)
    project_name, device_name = payload.split("|")
    wallets = sorted(list(r.smembers(f"failures:{project_name}:{device_name}")))
    builder = InlineKeyboardBuilder()
    if not wallets:
        await callback.answer("✅ Ошибок нет!", show_alert=True)
        return
    visible_wallets = wallets[-30:]
    for wallet in visible_wallets:
        builder.row(InlineKeyboardButton(text=f"❌ {wallet[:6]}...{wallet[-4:]}",
                                         callback_data=f"err_{project_name}|{device_name}|{wallet[-10:]}"))
    builder.row(InlineKeyboardButton(text="📥 Скачать полный отчёт (.txt)",
                                     callback_data=f"dl_all_{project_name}|{device_name}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"dev_{project_name}|{device_name}"))
    await safe_edit_text(callback, f"🚫 <b>Failed Wallets:</b> {len(wallets)}", builder.as_markup())


@dp.callback_query(F.data.startswith("err_"))
async def show_specific_error(callback: CallbackQuery):
    try:
        _, payload = callback.data.split("_", 1)
        project_name, device_name, wallet_part = payload.split("|")
        all_logs = r.hgetall(f"fail_logs:{project_name}:{device_name}")
        target_logs = "Лог не найден"
        full_w = wallet_part
        for w, raw_data in all_logs.items():
            if wallet_part in w:
                full_w = w
                try:
                    parsed_logs = json.loads(raw_data)
                    target_logs = "\n".join(parsed_logs) if isinstance(parsed_logs, list) else str(parsed_logs)
                except:
                    target_logs = str(raw_data)
                break
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data=f"fails_{project_name}|{device_name}"))
        text = f"👤 <b>Wallet:</b> <code>{full_w}</code>\n\n❌ <b>Log History:</b>\n<pre>{target_logs}</pre>"
        if len(text) > 4000:
            fobj = io.BytesIO(target_logs.encode('utf-8'))
            fobj.name = f"error_{full_w[:8]}.txt"
            await callback.message.answer_document(BufferedInputFile(fobj.getvalue(), filename=fobj.name),
                                                   caption="📜 Full Log History")
        else:
            await safe_edit_text(callback, text, builder.as_markup())
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("dl_all_"))
async def dl_all_handler(callback: CallbackQuery):
    payload = callback.data.replace("dl_all_", "")
    try:
        project_name, device_name = payload.split("|")
    except ValueError:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return
    logs = r.hgetall(f"fail_logs:{project_name}:{device_name}")
    if not logs:
        await callback.answer("Пусто (Logs not found in Redis)", show_alert=True)
        return
    lines = [f"FULL ERROR REPORT | {project_name} | {device_name}", "=" * 60]
    for wallet, raw_val in sorted(logs.items()):
        lines.append(f"WALLET: {wallet}")
        lines.append("-" * 30)
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list):
                for l in parsed: lines.append(str(l))
            else:
                lines.append(str(parsed))
        except:
            lines.append(str(raw_val))
        lines.append("=" * 60);
        lines.append("")
    txt = "\n".join(lines)
    fobj = io.BytesIO(txt.encode('utf-8'))
    fobj.name = f"ERRORS_{project_name}_{datetime.now().strftime('%H%M')}.txt"
    try:
        await callback.message.answer_document(BufferedInputFile(fobj.getvalue(), filename=fobj.name),
                                               caption="📜 Full History Report")
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка отправки: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("force_update_"))
async def force_update_handler(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    p, d = payload.split("|")
    r.publish(f"cmd:{p}:{d}", "update_status")
    await callback.answer("⏳ Обновляю...")
    await asyncio.sleep(1)
    await render_device_page(callback, p, d)


@dp.callback_query(F.data.startswith("cmd_log_"))
async def request_logs(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    p, d = payload.split("|")
    r.publish(f"cmd:{p}:{d}", "get_log")
    await callback.answer("📨 Запрос логов...")


@dp.callback_query(F.data == "refresh_main")
async def refresh_main_handler(callback: CallbackQuery):
    await show_start_menu(callback)


async def main():
    print("🚀 StatusBot запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(alert_listener())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())