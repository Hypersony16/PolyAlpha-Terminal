import httpx


async def fetch_wallet_total_value(wallet: str):
    url = "https://data-api.polymarket.com/value"
    params = {"user": wallet}

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, list) or not data:
        raise ValueError("No wallet value returned")

    return float(data[0]["value"])
