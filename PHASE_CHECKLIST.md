# PolyScalpBot BTC-Only Upgrade Checklist

## ✅ Added now
- [x] Removed weather markets from user-facing market section
- [x] BTC 15m-only command center
- [x] Live BTC price model
- [x] Polymarket BTC 15m market discovery
- [x] UP/DOWN probability model
- [x] Momentum + remaining volatility model
- [x] Normal CDF probability engine
- [x] Kelly-lite bankroll sizing
- [x] Market-making scanner foundation
- [x] YES+NO combined bid / merge edge display when available
- [x] Copy-trade preview buttons on wallet trade alerts
- [x] Low-latency jobs: BTC alerts 12s, wallet trades 4s, dashboards 4s
- [x] Cleaner Telegram UI
- [x] Safer fallbacks if market discovery fails
- [x] Global error handler

## ⚠️ Still preview/simulation only
- [ ] Real order execution
- [ ] Private key / CLOB auth setup
- [ ] Actual maker orders
- [ ] CTF merge execution
- [ ] WebSocket orderbook
- [ ] Historical ML training

## Next recommended phase
1. Add CLOB orderbook endpoint integration
2. Add maker-rebate simulator only
3. Add per-market fill-risk score
4. Add real execution only after simulation is profitable

- [x] Maker-rebate simulator foundation
- [x] Fill-risk engine
- [x] Verdict engine (ENTER/WATCH/AVOID)


## ✅ Added in analytics phase
- [x] 1h prediction accuracy
- [x] 10h prediction accuracy
- [x] 1d prediction accuracy
- [x] high-confidence accuracy split
- [x] paper trade logging
- [x] latency profiler
- [x] historical volatility regime label
- [x] system analytics panel


## ✅ Added in auto paper trading phase
- [x] $100 virtual paper balance
- [x] fully automatic paper trading toggle
- [x] simulated entries/exits on BTC 15m model
- [x] fee/slippage assumptions
- [x] max position size controls
- [x] paper PnL tracking
- [x] paper win rate
- [x] reset/start/stop commands
- [x] low-latency paper job loop
- [x] no real execution / no private keys
