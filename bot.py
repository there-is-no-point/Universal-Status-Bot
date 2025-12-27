import asyncio
import json
import redis
import re
import io
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
import config

# --- НАСТРОЙКИ ---
bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher()

try:
    r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True, ssl_cert_reqs=None)
    r.ping()
    print("✅ Бот успешно подключен к Redis")
except Exception as e:
    print(f"❌ Ошибка Redis: {e}")
    exit(1)


# === ФОНОВАЯ ЗАДАЧА: СЛУШАТЕЛЬ УВЕДОМЛЕНИЙ ===
async def alert_listener():
    pubsub = r.pubsub()
    pubsub.subscribe("telegram_alerts")
    print("📡 Alert Listener запущен...")

    while True:
        try:
            message = pubsub.get_message(ignore_subscribe_messages=True)
            if message:
                data = json.loads(message['data'])
                msg_type = data.get("type")
                worker = data.get("worker")
                project = data.get("project")  # HackQuest, Uniswap...
                text = data.get("text")

                # 👇 ПРОВЕРКА НАСТРОЕК (ФИЛЬТР)
                # 1. Глобальный мьют
                if r.get("settings:mute_all") == "1":
                    await asyncio.sleep(0.1)
                    continue

                # 2. Мьют конкретного проекта
                if project_name_muted(project):
                    await asyncio.sleep(0.1)
                    continue
                # -----------------------------

                header = f"🤖 <b>{project}</b> | {worker}"

                if msg_type == "error":
                    await bot.send_message(config.TG_USER_ID, f"🔴 <b>ALARM:</b>\n{header}\n\n<pre>{text}</pre>",
                                           parse_mode="HTML")
                elif msg_type == "success":
                    await bot.send_message(config.TG_USER_ID, f"✅ <b>FINISHED:</b>\n{header}\n\n{text}",
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


def project_name_muted(proj_name):
    """Проверяет в Redis, заглушен ли проект"""
    if not proj_name: return False
    return r.get(f"settings:mute:{proj_name}") == "1"


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ UI ---
def get_status_emoji(status_text: str) -> str:
    st = status_text.lower()
    if "working" in st: return "🟢"
    if "error" in st: return "🔴"
    if "done" in st: return "🏁"
    if "sleep" in st: return "💤"
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


# --- ОТРИСОВКА СТРАНИЦ ---
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
    nice_time, time_ago = format_time_data(stats.get('last_updated', 0))
    header_emoji = get_status_emoji(st)

    msg = f"🖥 <b>Worker:</b> {device_name}\n━━━━━━━━━━━━━━━━━━\n"
    msg += f"🔥 <b>STATUS:</b> {header_emoji} {st.upper()}\n"
    msg += f"⏰ <b>Last Signal:</b> {nice_time} <i>{time_ago}</i>\n\n"

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

    exclude = ["status", "current_account", "last_updated", "progress", "error", "pos_current", "pos_total"]
    extras = []
    for k in sorted(stats.keys()):
        if k not in exclude:
            nice = k.replace("_", " ").title() if "_" in k else k
            extras.append(f"• {nice}: <b>{stats[k]}</b>")

    if extras: msg += f"🎒 <b>Inventory:</b>\n" + "\n".join(extras) + "\n"

    if stats.get("error") and "Error" in st:
        msg += f"\n‼️ <b>CRITICAL ERROR:</b>\n<pre>{stats.get('error')}</pre>"

    await safe_edit_text(callback, msg, builder.as_markup())


# --- НОВЫЙ РАЗДЕЛ: НАСТРОЙКИ (SETTINGS) ---
async def render_settings_page(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()

    # 1. Глобальный переключатель
    is_global_muted = r.get("settings:mute_all") == "1"
    glob_emoji = "🔕" if is_global_muted else "🔔"
    glob_text = "All Notifications: OFF" if is_global_muted else "All Notifications: ON"
    builder.row(InlineKeyboardButton(text=f"{glob_emoji} {glob_text}", callback_data="toggle_global_mute"))

    # 2. Список проектов с переключателями
    keys = r.keys("status:*")
    projects = set()
    for k in keys:
        parts = k.split(":")
        if len(parts) > 1: projects.add(parts[1])

    if projects:
        builder.row(InlineKeyboardButton(text="👇 Project Specific 👇", callback_data="ignore"))
        for proj in sorted(projects):
            is_muted = r.get(f"settings:mute:{proj}") == "1"
            p_emoji = "❌" if is_muted else "✅"
            builder.row(InlineKeyboardButton(text=f"{p_emoji} {proj}", callback_data=f"toggle_proj_{proj}"))

    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="refresh_main"))

    text = "⚙️ <b>Notification Settings</b>\n\nНастройте, от кого вы хотите получать уведомления.\n❌ - уведомления выключены\n✅ - уведомления включены"
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data == "toggle_global_mute")
async def toggle_global(callback: CallbackQuery):
    current = r.get("settings:mute_all")
    new_val = "0" if current == "1" else "1"
    r.set("settings:mute_all", new_val)
    await render_settings_page(callback)


@dp.callback_query(F.data.startswith("toggle_proj_"))
async def toggle_project(callback: CallbackQuery):
    proj = callback.data.split("_", 2)[2]
    key = f"settings:mute:{proj}"
    current = r.get(key)
    new_val = "0" if current == "1" else "1"
    r.set(key, new_val)
    await render_settings_page(callback)


@dp.callback_query(F.data == "open_settings")
async def open_settings_handler(callback: CallbackQuery):
    await render_settings_page(callback)


# ------------------------------------------


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if str(message.from_user.id) != str(config.TG_USER_ID): return
    await show_main_menu(message)


async def show_main_menu(message_or_call):
    builder = InlineKeyboardBuilder()
    keys = r.keys("status:*")
    unique_projects = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) > 1: unique_projects.add(parts[1])

    text = "🛰 <b>Control Center</b>\nВыберите проект:"
    if not unique_projects:
        text += "\n(Нет активных проектов)"
    else:
        for proj in sorted(unique_projects):
            builder.row(InlineKeyboardButton(text=f"📂 {proj}", callback_data=f"proj_{proj}"))

    # Кнопки низа
    builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="open_settings"))
    builder.row(InlineKeyboardButton(text="♻️ Обновить список", callback_data="refresh_main"))

    if isinstance(message_or_call, types.Message):
        await message_or_call.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    elif isinstance(message_or_call, types.CallbackQuery):
        await safe_edit_text(message_or_call, text, builder.as_markup())


# ОСТАЛЬНЫЕ ХЕНДЛЕРЫ (Fails, Logs, etc.)
@dp.callback_query(F.data.startswith("proj_"))
async def show_devices(callback: CallbackQuery):
    project_name = callback.data.split("_")[1]
    devices_data = r.hgetall(f"status:{project_name}")
    builder = InlineKeyboardBuilder()
    active = 0;
    errors = 0;
    sleeping = 0
    device_buttons = []

    if devices_data:
        for dev_name, json_str in devices_data.items():
            try:
                stats = json.loads(json_str)
                st = stats.get("status", "Unknown")
                emoji = get_status_emoji(st)
                if "error" in st.lower():
                    errors += 1
                elif "working" in st.lower():
                    active += 1
                else:
                    sleeping += 1
                device_buttons.append((dev_name, f"{emoji} {dev_name} | {st}"))
            except:
                continue

    device_buttons.sort(key=lambda x: x[0])
    for _, txt in device_buttons:
        builder.row(InlineKeyboardButton(text=txt, callback_data=f"dev_{project_name}|{_}"))

    text = f"📂 <b>Project: {project_name}</b>\n🟢 Active: {active} | 💤 Sleep: {sleeping} | 🔴 Errors: {errors}"
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="refresh_main"))
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data.startswith("dev_"))
async def show_stats_handler(callback: CallbackQuery):
    _, payload = callback.data.split("_", 1)
    project, device = payload.split("|")
    await render_device_page(callback, project, device)


# --- FAILS MENU ---
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
        cb_data = f"err_{project_name}|{device_name}|{wallet[-10:]}"
        builder.row(InlineKeyboardButton(text=f"❌ {wallet[:6]}...{wallet[-4:]}", callback_data=cb_data))

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
        target = "Лог не найден"
        full_w = wallet_part
        for w, err in all_logs.items():
            if wallet_part in w:
                full_w = w;
                target = err;
                break

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data=f"fails_{project_name}|{device_name}"))

        text = f"👤 <b>Wallet:</b> <code>{full_w}</code>\n\n❌ <b>Log:</b>\n<pre>{target}</pre>"
        if len(text) > 4000:
            fobj = io.BytesIO(target.encode('utf-8'))
            fobj.name = f"err.txt"
            await callback.message.answer_document(BufferedInputFile(fobj.getvalue(), filename=fobj.name))
        else:
            await safe_edit_text(callback, text, builder.as_markup())
    except:
        await callback.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("dl_all_"))
async def dl_all_handler(callback: CallbackQuery):
    _, payload = callback.data.split("_", 1)
    project_name, device_name = payload.split("|")
    logs = r.hgetall(f"fail_logs:{project_name}:{device_name}")
    if not logs:
        await callback.answer("Пусто", show_alert=True);
        return

    txt = "\n".join([f"Wallet: {k}\nError: {v}\n{'-' * 30}" for k, v in logs.items()])
    fobj = io.BytesIO(txt.encode('utf-8'))
    fobj.name = f"ERRORS_{project_name}.txt"
    await callback.message.answer_document(BufferedInputFile(fobj.getvalue(), filename=fobj.name),
                                           caption="📜 Full Report")
    await callback.answer()


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
    await show_main_menu(callback)


async def main():
    print("🚀 StatusBot запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(alert_listener())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())