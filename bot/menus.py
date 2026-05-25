from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(is_admin: bool = False):
    rows = [
        [InlineKeyboardButton("₿ BTC 15m", callback_data="btc"),
         InlineKeyboardButton("📈 Market", callback_data="market")],
        [InlineKeyboardButton("🧠 Strategy", callback_data="strategy"),
         InlineKeyboardButton("📊 Stats", callback_data="accuracy")],
        [InlineKeyboardButton("📈 Analytics", callback_data="analytics")],
        [InlineKeyboardButton("🤖 Paper Auto", callback_data="paper_auto"),
         InlineKeyboardButton("👛 Wallets", callback_data="wallets")],
        [InlineKeyboardButton("🔔 Alerts", callback_data="alerts"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
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
        [InlineKeyboardButton("✅ On", callback_data="alerts_on"),
         InlineKeyboardButton("❌ Off", callback_data="alerts_off")],
        [InlineKeyboardButton("Edge 3%", callback_data="edge_3"),
         InlineKeyboardButton("Edge 5%", callback_data="edge_5"),
         InlineKeyboardButton("Edge 8%", callback_data="edge_8")],
        [InlineKeyboardButton("Quiet", callback_data="notify_quiet"),
         InlineKeyboardButton("Normal", callback_data="notify_normal")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def settings_menu(is_admin: bool = False):
    rows = [
        [InlineKeyboardButton("Quick", callback_data="view_quick"),
         InlineKeyboardButton("Normal", callback_data="view_normal"),
         InlineKeyboardButton("Pro", callback_data="view_pro")],
        [InlineKeyboardButton("Quiet", callback_data="notify_quiet"),
         InlineKeyboardButton("Normal Alerts", callback_data="notify_normal")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ]
    if is_admin:
        rows.insert(-1, [InlineKeyboardButton("🛠 Admin", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def paper_auto_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start", callback_data="paper_auto_start"),
         InlineKeyboardButton("⏸ Stop", callback_data="paper_auto_stop")],
        [InlineKeyboardButton("Real Odds ON", callback_data="paper_real_on"),
         InlineKeyboardButton("Fallback Test", callback_data="paper_real_off")],
        [InlineKeyboardButton("Mode: Hybrid", callback_data="mode_hybrid"),
         InlineKeyboardButton("Scalp", callback_data="mode_scalp"),
         InlineKeyboardButton("Resolve", callback_data="mode_resolution")],
        [InlineKeyboardButton("Max $1", callback_data="paper_max_1"),
         InlineKeyboardButton("Max $2", callback_data="paper_max_2"),
         InlineKeyboardButton("Max $5", callback_data="paper_max_5")],
        [InlineKeyboardButton("📊 Balance", callback_data="paper_auto_balance"),
         InlineKeyboardButton("♻️ Reset $100", callback_data="paper_auto_reset")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])



def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Live On", callback_data="live_on"),
         InlineKeyboardButton("🔴 Live Off", callback_data="live_off")],
        [InlineKeyboardButton("5s", callback_data="dash_5"),
         InlineKeyboardButton("10s", callback_data="dash_10"),
         InlineKeyboardButton("20s", callback_data="dash_20")],
        [InlineKeyboardButton("🧹 Clear Cache", callback_data="clear_cache")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def copy_size_menu(tx_hash: str):
    short = (tx_hash or "x")[:18]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("$1", callback_data=f"copy_size_{short}_1"),
         InlineKeyboardButton("$2", callback_data=f"copy_size_{short}_2")],
        [InlineKeyboardButton("$3", callback_data=f"copy_size_{short}_3"),
         InlineKeyboardButton("$5", callback_data=f"copy_size_{short}_5")],
        [InlineKeyboardButton("Cancel", callback_data=f"ignore_{short}")],
    ])
