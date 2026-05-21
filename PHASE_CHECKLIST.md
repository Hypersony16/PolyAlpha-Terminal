# PolyScalpBot Binary Polymarket Paper Fix

## Fixed
- Paper trading now simulates Polymarket binary payout correctly
- Correct side pays $1 per share
- Wrong side pays $0
- PnL uses entry price, shares, payout, slippage
- Added expected value in USD
- Added one trade per 15m market window protection
- Prevented repeated churn entries in same market
- Dashboard shows entry price, shares, EV, payout, PnL
- Real execution still disabled

## Still safe
- Paper only
- No private keys
- No live orders
- No real money execution

## Next
- Use real Polymarket CLOB orderbook for exact entry prices
- Paper fill simulator using bid/ask depth
- Compare paper PnL vs market close result
