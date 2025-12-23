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

# Подключение к Redis
try:
    # SSL Fix для бота (важно, если бот на Windows)
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
                project = data.get("project")
                text = data.get("text")
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
                                            caption=f"📄 <b>Log File Received</b>\n{header}", parse_mode="HTML")

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
    return "⚪️"


def parse_progress(stats: dict):
    """
    Парсинг прогресса. Приоритет - детальная статистика (✅/❌).
    """
    prog_str = str(stats.get('progress', ''))

    # 1. Пробуем найти детальную статистику (от monitor.py)
    match = re.search(r"(\d+)/(\d+).*?✅\s*(\d+).*?❌\s*(\d+)", prog_str)
    if match:
        return {
            "type": "detailed",
            "total": int(match.group(2)),
            "success": int(match.group(3)),
            "fails": int(match.group(4)),
            "done": int(match.group(3)) + int(match.group(4))
        }

    # 2. Если детальной нет, смотрим на позиции
    pos_current = stats.get('pos_current')
    pos_total = stats.get('pos_total')

    if pos_current and pos_total:
        return {
            "type": "simple",  # Это значит, мы знаем только позицию курсора
            "current": int(pos_current),
            "total": int(pos_total)
        }

    return None


def make_progress_bar(current, total, length=10):
    if total == 0: return f"[{'□' * length}]"
    percent = current / total
    if percent > 1: percent = 1
    filled = int(length * percent)
    return '■' * filled + '□' * (length - filled)


# 👇 НОВАЯ УМНАЯ ФУНКЦИЯ ВРЕМЕНИ
def format_time_data(raw_time):
    """
    Принимает: timestamp (float) ИЛИ старый формат строки (str)
    Возвращает кортеж: (Красивое время, Текст '5 мин назад')
    """
    try:
        now = datetime.now()

        # 1. Если пришло число (новый формат)
        if isinstance(raw_time, (int, float)):
            # fromtimestamp САМ конвертирует в локальное время устройства, где запущен бот
            dt = datetime.fromtimestamp(raw_time)

        # 2. Если пришла строка (старый формат, обратная совместимость)
        else:
            if len(str(raw_time)) <= 8:
                dt = datetime.strptime(raw_time, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)
            else:
                dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")

        # Считаем разницу
        diff_seconds = (now - dt).total_seconds()
        m = int(diff_seconds / 60)

        time_str = dt.strftime("%H:%M:%S")

        ago_str = ""
        if m < 1:
            ago_str = "(сейчас)"
        elif m >= 10:
            ago_str = f"(💀 {m} мин.)"
        else:
            ago_str = f"({m} мин.)"

        return time_str, ago_str

    except Exception:
        # Если пришел мусор, просто отдаем как есть
        return str(raw_time), ""


async def safe_edit_text(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer("Обновлено!")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("🤷‍♂️ Без изменений")
        else:
            raise e


# --- ЛОГИКА ОТРИСОВКИ ---
async def render_device_page(callback: CallbackQuery, project_name: str, device_name: str):
    json_str = r.hget(f"status:{project_name}", device_name)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📥 Get Log", callback_data=f"cmd_log_{project_name}|{device_name}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"force_update_{project_name}|{device_name}")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"proj_{project_name}"))

    if not json_str:
        await safe_edit_text(callback, "❌ Данные потеряны", reply_markup=builder.as_markup())
        return

    stats = json.loads(json_str)

    st = stats.get('status', 'Unknown')
    acc = stats.get('current_account', 'N/A')

    # 👇 ИСПОЛЬЗУЕМ НОВУЮ ЛОГИКУ ВРЕМЕНИ
    raw_last_upd = stats.get('last_updated', 0)
    nice_time, time_ago = format_time_data(raw_last_upd)

    header_emoji = get_status_emoji(st)

    msg = f"🖥 <b>Worker:</b> {device_name}\n━━━━━━━━━━━━━━━━━━\n"
    msg += f"🔥 <b>STATUS:</b> {header_emoji} {st.upper()}\n"
    msg += f"⏰ <b>Last Signal:</b> {nice_time} <i>{time_ago}</i>\n\n"

    # IDENTITY
    if acc and len(acc) > 15 and " " not in acc:
        short_acc = f"{acc[:6]}...{acc[-4:]}"
        msg += f"👤 <b>Last Active:</b> <code>{short_acc}</code>\n\n"
    elif acc != "N/A" and acc != "Unknown":
        msg += f"👤 <b>Activity:</b> <code>{acc}</code>\n\n"

    # --- ПРОГРЕСС БАР ---
    parsed = parse_progress(stats)

    if parsed and parsed['type'] == 'detailed':
        # Честный бар: ✅ + ❌
        total = parsed['total']
        done = parsed['done']
        percent = int((done / total) * 100) if total > 0 else 0
        bar = make_progress_bar(done, total)

        msg += f"📊 <b>PROGRESS:</b>\n<code>[{bar}] {percent}%</code>\n"
        msg += f"📦 Total: {total} | ✅ {parsed['success']} | ❌ {parsed['fails']}\n\n"

    elif parsed and parsed['type'] == 'simple':
        total = parsed['total']
        bar = make_progress_bar(0, total)
        msg += f"📊 <b>PROGRESS:</b>\n<code>[{bar}] 0%</code>\n"
        msg += f"📦 Total: {total} | ✅ ? | ❌ ?\n\n"

    else:
        # Совсем нет данных
        msg += f"📊 <b>Progress:</b> N/A\n\n"

    # ИНВЕНТАРЬ
    exclude = ["status", "current_account", "last_updated", "progress", "error", "pos_current", "pos_total"]
    extras = []
    for k in sorted(stats.keys()):
        if k not in exclude:
            v = stats[k]
            if "_" in k and " " not in k:
                nice_key = k.replace("_", " ").title()
            else:
                nice_key = k
            extras.append(f"• {nice_key}: <b>{v}</b>")

    if extras:
        msg += f"🎒 <b>Inventory:</b>\n" + "\n".join(extras) + "\n"

    error_msg = stats.get("error")
    if error_msg and "Error" in st:
        msg += f"\n‼️ <b>CRITICAL ERROR:</b>\n<pre>{error_msg}</pre>"

    await safe_edit_text(callback, msg, builder.as_markup())


# --- ХЕНДЛЕРЫ ---
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
        builder.row(InlineKeyboardButton(text="♻️ Обновить", callback_data="refresh_main"))
    else:
        for proj in sorted(unique_projects):
            builder.row(InlineKeyboardButton(text=f"📂 {proj}", callback_data=f"proj_{proj}"))
        builder.row(InlineKeyboardButton(text="♻️ Обновить список", callback_data="refresh_main"))

    if isinstance(message_or_call, types.Message):
        await message_or_call.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    elif isinstance(message_or_call, types.CallbackQuery):
        await safe_edit_text(message_or_call, text, builder.as_markup())


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
        for _, txt in device_buttons: builder.row(
            InlineKeyboardButton(text=txt, callback_data=f"dev_{project_name}|{_}"))

    text = f"📂 <b>Project: {project_name}</b>\n🟢 Active: {active} | 💤 Sleep: {sleeping} | 🔴 Errors: {errors}"
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="refresh_main"))
    await safe_edit_text(callback, text, builder.as_markup())


@dp.callback_query(F.data.startswith("dev_"))
async def show_stats(callback: CallbackQuery):
    _, payload = callback.data.split("_", 1)
    project_name, device_name = payload.split("|")
    await render_device_page(callback, project_name, device_name)


@dp.callback_query(F.data.startswith("force_update_"))
async def force_update_handler(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    project_name, device_name = payload.split("|")
    channel = f"cmd:{project_name}:{device_name}"
    try:
        r.publish(channel, "update_status")
        await callback.answer("⏳ Запрашиваю свежие данные...", show_alert=False)
        await asyncio.sleep(1.0)
    except Exception as e:
        print(f"Error publishing update cmd: {e}")
    await render_device_page(callback, project_name, device_name)


@dp.callback_query(F.data.startswith("cmd_log_"))
async def request_logs(callback: CallbackQuery):
    _, _, payload = callback.data.split("_", 2)
    project_name, device_name = payload.split("|")
    channel = f"cmd:{project_name}:{device_name}"
    r.publish(channel, "get_log")
    await callback.answer(f"📨 Запрос логов отправлен...", show_alert=True)


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