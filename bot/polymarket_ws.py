

"""
Optional Polymarket CLOB WebSocket scaffold.

Polymarket docs: public market websocket subscribes by asset IDs, not by market slug.
Current bot still uses 1s CLOB/Gamma polling because it is safer on Railway.
To activate true WS later:
1. get token IDs from market payload
2. subscribe to wss://ws-subscriptions-clob.polymarket.com/ws/market
3. update bot.market_tick.set_latest_tick() from price_change/book events

Kept separate so failed WS connections cannot crash the bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

try:
    import websockets
except Exception:
    websockets = None

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def market_ws_loop(asset_ids: Iterable[str], on_event):
    if websockets is None:
        logging.warning("websockets package not installed")
        return

    ids = [str(x) for x in asset_ids if x]
    if not ids:
        return

    while True:
        try:
            async with websockets.connect(WS_MARKET_URL, ping_interval=None) as ws:
                await ws.send(json.dumps({"assets_ids": ids, "type": "market"}))
                while True:
                    msg = await ws.recv()
                    if msg == "PONG":
                        continue
                    try:
                        data = json.loads(msg)
                    except Exception:
                        data = {"raw": msg}
                    await on_event(data)
        except Exception as e:
            logging.warning(f"market_ws_loop reconnecting after error: {e}")
            await asyncio.sleep(3)
