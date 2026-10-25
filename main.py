from datetime import datetime, timezone
from typing import Any
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="BTC Signal AI", version="0.1.0")
OKX_BASE = "https://www.okx.com"
INSTRUMENT = "BTC-USDT"

async def get_candles() -> pd.DataFrame:
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": INSTRUMENT, "bar": "1m", "limit": "300"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить данные OKX: {exc}")
    if payload.get("code") != "0" or not payload.get("data"):
        raise HTTPException(status_code=502, detail=f"Ответ OKX: {payload.get('msg', 'нет данных')}")
    # OKX candle format: [ts, open, high, low, close, volume, ... , confirm]
    rows = []
    for item in payload["data"]:
        rows.append({
            "ts": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "confirm": str(item[8]) if len(item) > 8 else "0",
        })
    df = pd.DataFrame(rows).sort_values("ts").drop_duplicates("ts")
    # Exclude the currently forming candle to avoid using an unstable close.
    df = df[df["confirm"] == "1"].copy()
    if len(df) < 80:
        raise HTTPException(status_code=502, detail="Недостаточно закрытых свечей для расчёта.")
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.reset_index(drop=True)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    close = x["close"]
    x["ema9"] = close.ewm(span=9, adjust=False).mean()
    x["ema21"] = close.ewm(span=21, adjust=False).mean()
    x["ema50"] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    x["rsi"] = 100 - (100 / (1 + rs))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    prev_close = close.shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    x["atr"] = tr.rolling(14).mean()
    x["momentum3"] = close.pct_change(3) * 100
    x["momentum10"] = close.pct_change(10) * 100
    x["volume_avg20"] = x["volume"].rolling(20).mean()
    return x

def resample_candles(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    x = df.set_index("time")
    rule = f"{minutes}min"
    out = x.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna().reset_index()
    # Only keep fully elapsed bars.
    now = pd.Timestamp.now(tz="UTC")
    out = out[out["time"] + pd.Timedelta(minutes=minutes) <= now]
    return out

def make_signal(df: pd.DataFrame, horizon: int) -> dict[str, Any]:
    # Blend short-term 1m momentum with the requested horizon's candle trend.
    micro = add_indicators(df)
    horizon_df = resample_candles(df, horizon)
    if len(horizon_df) >= 15:
        macro = add_indicators(horizon_df)
        h = macro.iloc[-1]
    else:
        macro = add_indicators(df)
        h = macro.iloc[-1]
    m = micro.iloc[-1]
    score = 0.0
    reasons = []

    if m["ema9"] > m["ema21"]:
        score += 1.0; reasons.append("EMA 9 выше EMA 21 на 1m")
    else:
        score -= 1.0; reasons.append("EMA 9 ниже EMA 21 на 1m")

    if pd.notna(m["macd"]) and pd.notna(m["macd_signal"]):
        if m["macd"] > m["macd_signal"]:
            score += 0.8; reasons.append("MACD выше сигнальной линии")
        else:
            score -= 0.8; reasons.append("MACD ниже сигнальной линии")

    rsi = float(m["rsi"]) if pd.notna(m["rsi"]) else 50.0
    if 52 <= rsi <= 68:
        score += 0.6; reasons.append("RSI подтверждает бычий импульс")
    elif 32 <= rsi <= 48:
        score -= 0.6; reasons.append("RSI подтверждает медвежий импульс")
    elif rsi > 75:
        score -= 0.4; reasons.append("RSI высокий: риск перегрева")
    elif rsi < 25:
        score += 0.4; reasons.append("RSI низкий: возможен отскок")

    if pd.notna(m["momentum3"]):
        if m["momentum3"] > 0.04:
            score += 0.5; reasons.append("Положительный импульс за 3 минуты")
        elif m["momentum3"] < -0.04:
            score -= 0.5; reasons.append("Отрицательный импульс за 3 минуты")

    if pd.notna(h["ema9"]) and pd.notna(h["ema21"]):
        if h["ema9"] > h["ema21"]:
            score += 0.8; reasons.append(f"Тренд на {horizon}m вверх")
        else:
            score -= 0.8; reasons.append(f"Тренд на {horizon}m вниз")

    if pd.notna(m["volume_avg20"]) and m["volume_avg20"] > 0 and m["volume"] > m["volume_avg20"] * 1.5:
        reasons.append("Объём последней свечи выше среднего")

    # Confidence is a heuristic score, not a calibrated win probability.
    strength = min(95, max(50, round(50 + abs(score) * 9)))
    if score >= 1.2:
        direction = "UP"
    elif score <= -1.2:
        direction = "DOWN"
    else:
        direction = "WAIT"
        strength = min(strength, 59)

    atr = float(m["atr"]) if pd.notna(m["atr"]) else None
    return {
        "horizon_minutes": horizon,
        "direction": direction,
        "score": round(score, 2),
        "model_strength": strength,
        "rsi14": round(rsi, 2),
        "ema9": round(float(m["ema9"]), 2),
        "ema21": round(float(m["ema21"]), 2),
        "macd": round(float(m["macd"]), 4) if pd.notna(m["macd"]) else None,
        "momentum3_pct": round(float(m["momentum3"]), 4) if pd.notna(m["momentum3"]) else None,
        "atr14": round(atr, 2) if atr is not None else None,
        "reasons": reasons,
        "method_note": "Эвристический балл; это не подтверждённая вероятность выигрыша."
    }

@app.get("/api/analysis")
async def analysis():
    df = await get_candles()
    latest = df.iloc[-1]
    return {
        "instrument": INSTRUMENT,
        "price": round(float(latest["close"]), 2),
        "candle_time": latest["time"].isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "signals": [make_signal(df, 5), make_signal(df, 15)],
        "source": "OKX public market data",
        "status": "ok"
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def home():
    return FileResponse("static/index.html")
