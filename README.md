# PolyAlpha Terminal Upgrade Pack

This is the important-code-only package to upgrade your existing **PolyScalpBot** into a Polymarket smart-money terminal.

## What it adds

- Smart wallet list
- Wallet scoring
- Consensus scanner
- Your wallet comparison
- Telegram terminal commands
- Trading command placeholder with execution disabled

## Commands

```text
/alpha
/alpha_addwallet 0xWallet label
/alpha_removewallet 0xWallet
/alpha_wallets
/topwallets
/consensus
/mywallet 0xYourWallet
/compare
/terminal
/sell
```

## Install

Copy files into your repo:

```text
bot/smart_money.py
bot/alpha_store.py
bot/alpha_handlers.py
```

Then patch `bot/handlers.py`:

```python
from bot.alpha_handlers import register_alpha_handlers
```

Inside `register_handlers(app)` add:

```python
register_alpha_handlers(app)
```

Deploy to Railway normally through GitHub.

## Important

This version is **read-only**. It does not buy or sell. That is intentional so you can test signals safely first.
