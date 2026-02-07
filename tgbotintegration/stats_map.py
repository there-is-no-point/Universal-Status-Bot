# tgbotintegration/stats_map.py

def get_display_stats(client) -> dict:
    """
    Маппинг данных из AccountManager для отображения в боте.
    """
    return {
        "🕊 Twitter": getattr(client, 'twitter_username', "—"),
        "💎 Points": getattr(client, 'points', 0),
        "👾 Agents New": getattr(client, 'agents_created', 0),
        "✏️ Edited": getattr(client, 'agents_edited', 0),
        "🔔 Events": getattr(client, 'events_reacted', 0),
        "🔥 Reactions": getattr(client, 'total_reactions', 0),
    }