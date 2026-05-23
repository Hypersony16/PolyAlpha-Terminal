# No Trade Fix

Problem:
Auto paper could run for hours with no trades because filters were too strict and there was no visible reason.

Changes:
- Added /paper_auto "Last check" diagnostic
- Stores exact skip reason from paper_auto engine
- Relaxed paper-test thresholds:
  - MIN_EDGE 5% -> 2%
  - MIN_EV 4% -> 0.5%
  - confidence requirement Medium -> Low
- Allows fallback_50 paper-test only if signal is stronger:
  - model probability >= 62%
  - edge >= 10%
- Still blocks Early/Danger phases
- Still max 1 trade per 15m window
- Still paper-only

Use:
Open /paper_auto after deployment and check "Last check".
It will say exactly why no trade happened.
