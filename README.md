# 🤖 Universal Status Monitor (Redis + Aiogram)

Это система централизованного мониторинга для фермы аккаунтов.
Она позволяет следить за статусом, прогрессом и инвентарем (балансы, уровни) ваших софтов с разных серверов через одного Telegram-бота.

## 🏗 Архитектура

1.  **StatusBot (Dashboard)** — Это "экран". Он запускается один раз (на ноутбуке или VDS). Читает данные из Redis и показывает меню.
2.  **Workers (Твои софты)** — Это "рабочие". Они (HackQuest, Uniswap и т.д.) крутятся на серверах и молча отправляют статистику в Redis.
3.  **Redis (Облако)** — Общая память, связывающая всех воедино.

---

## 📝 Шаг 0: Получение ключей (Telegram & Redis)

Прежде чем запускать код, нужно получить 3 параметра.

### 1. Токен Бота (TG_BOT_TOKEN)
1.  Открой в Telegram бота **@BotFather**.
2.  Отправь команду `/newbot`.
3.  Введи имя бота (любое, например `My Farm Monitor`).
4.  Введи юзернейм (на английском, должен заканчиваться на `bot`, например `My_Super_Farm_bot`).
5.  Скопируй полученный **HTTP API Token**.
6.  **ВАЖНО:** Найди своего созданного бота в поиске и нажми **ЗАПУСТИТЬ (/start)**, иначе он не сможет писать тебе.

### 2. Твой ID (TG_USER_ID)
Бот должен знать, кому именно отвечать (чтобы чужие не смотрели твою статистику).
1.  Открой бота **@userinfobot**.
2.  Нажми `/start`.
3.  Скопируй цифры из строки `Id: 123456789`.

### 3. База данных (REDIS_URL)
Мы используем бесплатное облако Upstash (хватит на сотни аккаунтов).
1.  Зайди на сайт [Upstash.com](https://upstash.com/) и зарегистрируйся.
2.  Выбери тип БД **"Redis"**.
3.  Нажми **Create Database**.
4.  Name: `FarmStats`, Region: любой (например, Europe).
5.  Когда база создастся, прокрути немного вниз до раздела **"Connect"**.
6.  В подразделе Language выбираешь **Python**.
7.  Над этим подразделом выбираешь **TCP**.
8.  Скопируй длинную ссылку. Она должна выглядеть так:
    `rediss://default:пароль@адрес.upstash.io:6379`

---

## 🚀 Установка и запуск Бота

Этот шаг делается **один раз** для создания пульта управления.

1.  **Клонируйте репозиторий:**
    ```bash
    git clone [https://github.com/there-is-no-point/Universal-Status-Bot.git](https://github.com/there-is-no-point/Universal-Status-Bot.git)
    cd Universal-Status-Bot
    ```
    
2.  **Создайте виртуальное окружение:**

    Windows:
    ```PowerShell
    python -m venv venv
    .\venv\Scripts\activate
    ```
    Linux / macOS:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Установите зависимости**
    ```bash
    pip install -r requirements.txt
    ```
    
4.  **Настройте `config.py` в папке бота:**
    
    В папке бота вы найдете файл `config_example.py`.
    
    Переименуйте его в `config.py` (или скопируйте с новым именем).
    
    Откройте `config.py` и вставьте свои данные:
    
    ```python
    TG_BOT_TOKEN = "ТВОЙ_ТОКЕН_ОТ_BOTFATHER"
    TG_USER_ID = 123456789
    REDIS_URL = "rediss://default:..."       # Ссылка из Upstash
    ```

5.  **Запустите бота:**
    ```bash
    python bot.py
    ```

---

## 🔌 Интеграция в новый проект

Чтобы подключить любой ваш скрипт к боту, выполните эти 5 шагов.

### 1. Установка библиотек
В окружении, где работает софт, должен быть Redis:
```bash
pip install redis requests
```

### 2. Копирование модулей
Скопируйте следующие **4 файла** из папки бота в папку `modules/` вашего проекта:
* `notifications.py` (Связь с Redis, получение команд)
* `status_manager.py` (Отправка статусов)
* `monitor.py` (Декоратор для прогресса)
* `stats_map.py` (Настройка инвентаря)

### 3. Настройка `config.py` проекта
Добавьте в конфиг софта:

```python
# --- STATUS MONITORING ---
USE_TG_BOT = True           # Отправлять ли критические уведомления в ТГ
TG_BOT_TOKEN = "..."        # Тот же токен, что у бота
TG_USER_ID = 123456789      # Твой ID

# --- REDIS & DEVICE ---
REDIS_URL = "rediss://default:..."  # Та же ссылка, что и у бота!
WORKER_NAME = "Server 1"            # Уникальное имя этого ПК
DEVICE_NAME = WORKER_NAME           # (Дублируем для совместимости)
```

### 4. Защита от крашей и Логи (`main.py`)
**КРИТИЧЕСКИ ВАЖНО:** Чтобы софт не падал с ошибкой памяти (`Segmentation fault`), добавьте этот код в **самое начало** `main.py` (первой строкой!).

```python
import sys
import logging
from logging.handlers import RotatingFileHandler

# [ВАЖНО] 1. Хак для защиты от вылетов (Вставлять ДО импорта остальных модулей)
sys.modules['hiredis'] = None 

# [ВАЖНО] 2. Настройка логов (Бот читает файл "app.log")
def setup_file_logging():
    log_file = "app.log"
    # 5 МБ макс, храним 1 копию, utf-8
    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=1, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(file_handler)

setup_file_logging()

# ... далее ваши обычные импорты ...
import config
# from modules.client import MyClient
```

### 5. Интеграция в Клиент

#### А. Настройка отображения (`stats_map.py`)
Откройте `modules/stats_map.py` и пропишите, какие данные вы хотите видеть в боте:

```python
def get_display_stats(client) -> dict:
    return {
        "💰 Balance": getattr(client, 'balance', 0),
        "🔢 Tx Count": getattr(client, 'tx_count', 0),
        # Любые ваши поля...
    }
```

#### Б. Код клиента (`client.py`)
В файл логики (класса клиента) добавьте:

1.  **Импорты:**
    ```python
    from .notifications import bot_link 
    from .stats_map import get_display_stats
    from .monitor import monitor_account
    ```

2.  **Регистрацию (в `__init__`):**
    ```python
    def __init__(self, ...):
        # ...
        # Регистрируем себя в боте
        bot_link.register_client(self, stats_callback=get_display_stats)
    ```

3.  **Декоратор (над методом запуска):**
    ```python
    # Название проекта в скобках
    @monitor_account("MyProject") 
    def process_account(self) -> bool:
        # Ваш код...
        return True
    ```

---

## ❓ FAQ / Решение проблем

* **Ошибка `Segmentation fault` / `free(): corrupted`**: 
  Вы забыли добавить `sys.modules['hiredis'] = None` в самое начало `main.py`.

* **Бот пишет "❌ Данные потеряны"**: 
  Проверьте, что `REDIS_URL` одинаковый везде, а `WORKER_NAME` в конфиге совпадает с именем кнопки в боте.

* **Логи не приходят**: 
  Убедитесь, что скрипт создает файл `app.log` в корневой папке.

* **Кодировка логов**: 
  Бот автоматически исправляет ошибки кодировки, убедитесь, что вы используете последнюю версию `notifications.py` из инструкции.
