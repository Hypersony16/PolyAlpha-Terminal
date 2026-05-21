import math


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def interval_probability(low: float, high: float, mean: float, sigma: float) -> float:
    return normal_cdf((high - mean) / sigma) - normal_cdf((low - mean) / sigma)


def build_exact_probability_table_from_ensemble(member_highs):
    counts = {}
    total = len(member_highs)

    for h in member_highs:
        t = int(round(h))
        counts[t] = counts.get(t, 0) + 1

    rows = []
    for temp in sorted(counts):
        rows.append({
            "temp": temp,
            "probability": round(counts[temp] / total, 4),
        })
    return rows


def build_exact_probability_table_from_fallback(consensus_high):
    sigma = 1.5
    rows = []
    for temp in range(4, 31):
        prob = interval_probability(temp - 0.5, temp + 0.5, consensus_high, sigma)
        rows.append({
            "temp": temp,
            "probability": round(prob, 4),
        })
    return rows


def build_exact_probability_table(weather_data):
    if weather_data.get("source") == "ensemble" and weather_data.get("member_highs"):
        return build_exact_probability_table_from_ensemble(weather_data["member_highs"])
    return build_exact_probability_table_from_fallback(weather_data["consensus_high"])


def confidence_label(edge: float, ensemble_count: int, model_prob: float) -> str:
    if edge >= 0.08 and ensemble_count >= 25 and model_prob >= 0.18:
        return "High"
    if edge >= 0.04 and ensemble_count >= 15 and model_prob >= 0.10:
        return "Medium"
    return "Low"


def suggested_bet_size_pct(edge: float, confidence: str) -> float:
    if confidence == "High":
        return min(2.0, max(0.75, edge * 20))
    if confidence == "Medium":
        return min(1.0, max(0.40, edge * 12))
    return min(0.5, max(0.20, edge * 8))


def rank_bets(rows, market_prices: dict, ensemble_count: int):
    candidates = []

    for row in rows:
        temp = row["temp"]
        model_prob = row["probability"]
        market_prob = market_prices.get(temp)

        if market_prob is None:
            continue

        edge = model_prob - market_prob
        confidence = confidence_label(edge, ensemble_count, model_prob)
        bankroll_pct = suggested_bet_size_pct(edge, confidence)

        candidates.append({
            "temp": temp,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge": round(edge, 4),
            "confidence": confidence,
            "bankroll_pct": round(bankroll_pct, 2),
        })

    candidates.sort(key=lambda x: x["edge"], reverse=True)
    return candidates


def detect_market_inefficiencies(market_prices: dict):
    """
    Looks for ladder bumps where a temp price is far from the average of neighbors.
    """
    temps = sorted(market_prices.keys())
    findings = []

    for i in range(1, len(temps) - 1):
        left_t = temps[i - 1]
        mid_t = temps[i]
        right_t = temps[i + 1]

        left = market_prices[left_t]
        mid = market_prices[mid_t]
        right = market_prices[right_t]

        neighbor_avg = (left + right) / 2
        diff = mid - neighbor_avg

        if abs(diff) >= 0.08:
            findings.append({
                "temp": mid_t,
                "price": mid,
                "neighbor_avg": round(neighbor_avg, 4),
                "gap": round(diff, 4),
                "direction": "overpriced" if diff > 0 else "underpriced",
            })

    findings.sort(key=lambda x: abs(x["gap"]), reverse=True)
    return findings
