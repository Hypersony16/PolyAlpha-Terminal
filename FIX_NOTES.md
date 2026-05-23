# Fixed by ChatGPT

Railway crash was caused by missing `bot/jobs.py`.
This ZIP includes a restored compatible jobs.py with:
- alerts_job
- wallet_job
- wallet_trades_job
- live_dashboard_job
- paper_auto_job
- daily_summary_job

Checks performed:
- Python compileall passed
- app.py remains at repo root
- requirements.txt remains at repo root
- bot/jobs.py exists
- no Binance API references
- paper auto remains paper-only
