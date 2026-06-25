"""Telegram menus for PolyScalpBot + PolyAlpha Terminal.
v2.3: cleaner main menu with clear section separation.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(is_admin: bool = False):
    """Clean top-level menu with clear section separation."""
    rows = [
        # PolyAlpha section
        [InlineKeyboardButton("🧠 PolyAlpha Terminal", callback_data="alpha")],
        [InlineKeyboardButton("🖥 Dashboard", callback_data="alpha_terminal"),
         InlineKeyboardButton("🔥 Consensus", callback_data="alpha_consensus")],
        [InlineKeyboardButton("🏆 Top Wallets", callback_data="alpha_topwallets"),
         InlineKeyboardButton("🐋 Whales", callback_data="alpha_whales")],
        # BTC section
        [InlineKeyboardButton("── BTC 15m ──", callback_data="btc")],
        [InlineKeyboardButton("₿ BTC Market", callback_data="market"),
         InlineKeyboardButton("🧠 Strategy", callback_data="strategy")],
        [InlineKeyboardButton("📊 Stats", callback_data="accuracy"),
         InlineKeyboardButton("📈 Analytics", callback_data="analytics")],
        # Paper trading
        [InlineKeyboardButton("── Paper Trading ──", callback_data="paper_auto")],
        [InlineKeyboardButton("🤖 Paper Auto", callback_data="paper_auto"),
         InlineKeyboardButton("👛 BTC Wallets", callback_data="wallets")],
        # Settings
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("🔔 Alerts", callback_data="alerts")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="home")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Admin", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def btc_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="btc"),
         InlineKeyboardButton("📈 Market", callback_data="market")],
        [InlineKeyboardButton("🧠 Strategy", callback_data="strategy"),
         InlineKeyboardButton("📊 Stats", callback_data="accuracy")],
        [InlineKeyboardButton("📈 Analytics", callback_data="analytics")],
        [InlineKeyboardButton("🧠 PolyAlpha", callback_data="alpha")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def wallet_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List", callback_data="wallets"),
         InlineKeyboardButton("➕ Add", callback_data="wallet_add_hint")],
        [InlineKeyboardButton("⭐ Own Wallet", callback_data="own_wallet_hint"),
         InlineKeyboardButton("🏷 Rename", callback_data="wallet_name_hint")],
        [InlineKeyboardButton("🗑 Remove", callback_data="wallet_remove_hint")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def alerts_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Toggle Alerts", callback_data="alerts_toggle")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Dashboard Refresh", callback_data="settings_refresh")],
        [InlineKeyboardButton("📊 Live Dashboards", callback_data="settings_live")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Active Users", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def paper_auto_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Enable", callback_data="paper_enable"),
         InlineKeyboardButton("⏹ Disable", callback_data="paper_disable")],
        [InlineKeyboardButton("📊 Summary", callback_data="paper_summary"),
         InlineKeyboardButton("🔄 Reset", callback_data="paper_reset")],
        [InlineKeyboardButton("🎯 Strategy", callback_data="strategy"),
         InlineKeyboardButton("📈 Performance", callback_data="performance")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def copy_size_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("$1", callback_data="copy_size_1"),
         InlineKeyboardButton("$2", callback_data="copy_size_2"),
         InlineKeyboardButton("$5", callback_data="copy_size_5")],
        [InlineKeyboardButton("$10", callback_data="copy_size_10"),
         InlineKeyboardButton("$25", callback_data="copy_size_25")],
        [InlineKeyboardButton("⬅️ Back", callback_data="wallets")],
    ])
