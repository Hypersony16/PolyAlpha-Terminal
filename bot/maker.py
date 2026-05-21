import random

def maker_snapshot(model: dict, market: dict | None):
    market_prob = float(model.get("market_prob", 0.5))
    spread = 0.02 + random.uniform(0.0, 0.02)

    yes_bid = max(0.01, round(market_prob - spread/2, 3))
    no_bid = max(0.01, round((1 - market_prob) - spread/2, 3))

    combined = round(yes_bid + no_bid, 3)
    merge_edge = round((1 - combined) * 100, 2)

    if combined <= 0.97:
        verdict = "ENTER"
        risk = "Low"
    elif combined <= 0.985:
        verdict = "WATCH"
        risk = "Medium"
    else:
        verdict = "AVOID"
        risk = "High"

    return {
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "combined": combined,
        "merge_edge": merge_edge,
        "risk": risk,
        "verdict": verdict,
    }
