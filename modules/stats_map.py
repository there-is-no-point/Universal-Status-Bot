# modules/stats_map.py

def get_display_stats(client) -> dict:
    """
    Эта функция знает, какие данные достать из клиента для показа в боте.
    Для каждого проекта меняем только этот файл.
    """
    return {
        "💰 Coins": getattr(client, 'total_coins', 0),
        "🎓 Exp": getattr(client, 'total_exp', 0),
        "🦆 Duck Lvl": getattr(client, 'duck_level', 0),
        "📚 Courses": getattr(client, 'courses_completed', 0),
        # Пример для другого проекта:
        # "⚡ Energy": getattr(client, 'energy', 0),
    }