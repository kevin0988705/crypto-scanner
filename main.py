"""
Binance Futures Proxy + Scanner Backend
Deploy on Railway: https://railway.app
"""

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import os

app = FastAPI(title="Crypto Breakout Scanner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

FAPI = "https://fapi.binance.com"

DEFAULT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","LINKUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","ATOMUSDT","NEARUSDT","APTUSDT",
    "ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","TIAUSDT",
]

# 簡單快取，避免每次掃描都重新抓 top symbols（60秒內重用）
_top_symbols_cache = {"data": None, "ts": 0}

async def get_top_symbols_by_volume(client: httpx.AsyncClient, limit: int = 100):
    """從幣安合約 24hr ticker 取得交易額（quoteVolume）排名前 N 的 USDT 永續合約"""
    import time
    now = time.time()
    if _top_symbols_cache["data"] and (now - _top_symbols_cache["ts"] < 60):
        return _top_symbols_cache["data"][:limit]

    res = await client.get(f"{FAPI}/fapi/v1/ticker/24hr")
    res.raise_for_status()
    data = res.json()

    # 只要 USDT 永續合約，排除槓桿代幣與非標準格式
    usdt_pairs = [
        d for d in data
        if d.get("symbol", "").endswith("USDT")
        and not any(x in d["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR"])
    ]
    usdt_pairs.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    symbols = [d["symbol"] for d in usdt_pairs]

    _top_symbols_cache["data"] = symbols
    _top_symbols_cache["ts"] = now
    return symbols[:limit]

# ── helpers ───────────────────────────────────────────────────────────────────
def sma(arr, n):
    if len(arr) < n:
        return None
    return sum(arr[-n:]) / n

def bollinger_width(closes, n=20):
    if len(closes) < n:
        return None
    sl = closes[-n:]
    m  = sum(sl) / n
    sd = (sum((x - m) ** 2 for x in sl) / n) ** 0.5
    return (4 * sd) / m if m else None

def rsi_calc(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    rs = gains / (losses or 0.0001)
    return 100 - 100 / (1 + rs)

def volume_shrink(vols, n=10):
    if len(vols) < n + 1:
        return None
    avg = sum(vols[-n-1:-1]) / n
    return (avg - vols[-1]) / avg if avg else None

def price_range_narrow(candles, n=10):
    if len(candles) < n:
        return None
    sl = candles[-n:]
    hi = max(c["high"] for c in sl)
    lo = min(c["low"]  for c in sl)
    return (hi - lo) / lo if lo else None

# ── fetch one symbol ──────────────────────────────────────────────────────────
async def fetch_symbol(client: httpx.AsyncClient, symbol: str, interval: str):
    try:
        kl_res, fund_res, oi_res, ls_res = await asyncio.gather(
            client.get(f"{FAPI}/fapi/v1/klines",
                       params={"symbol": symbol, "interval": interval, "limit": 120}),
            client.get(f"{FAPI}/fapi/v1/fundingRate",
                       params={"symbol": symbol, "limit": 1}),
            client.get(f"{FAPI}/fapi/v1/openInterest",
                       params={"symbol": symbol}),
            client.get(f"{FAPI}/futures/data/globalLongShortAccountRatio",
                       params={"symbol": symbol, "period": interval, "limit": 1}),
            return_exceptions=True,
        )
    except Exception as e:
        return None

    # klines
    if isinstance(kl_res, Exception) or kl_res.status_code != 200:
        return None
    klines = kl_res.json()
    if not isinstance(klines, list) or len(klines) < 65:
        return None

    candles = [{"open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]),  "close": float(k[4]),
                "volume": float(k[5])} for k in klines]
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    last    = closes[-1]

    ma30 = sma(closes, 30)
    ma45 = sma(closes, 45)
    ma60 = sma(closes, 60)
    if not ma30 or not ma45 or not ma60:
        return None

    above_all = last > ma30 and last > ma45 and last > ma60
    ma_fan    = ma30 > ma45 > ma60
    shrink    = volume_shrink(volumes, 10)
    bw        = bollinger_width(closes, 20)
    rsi       = rsi_calc(closes, 14)
    pr_range  = price_range_narrow(candles, 10)

    # futures data
    funding_rate = None
    try:
        if not isinstance(fund_res, Exception) and fund_res.status_code == 200:
            fd = fund_res.json()
            if fd and isinstance(fd, list):
                funding_rate = float(fd[0]["fundingRate"]) * 100
    except Exception:
        pass

    oi_val = None
    try:
        if not isinstance(oi_res, Exception) and oi_res.status_code == 200:
            oi_val = float(oi_res.json().get("openInterest", 0))
    except Exception:
        pass

    ls_ratio = None
    try:
        if not isinstance(ls_res, Exception) and ls_res.status_code == 200:
            ls_data = ls_res.json()
            if ls_data and isinstance(ls_data, list):
                ls_ratio = float(ls_data[0].get("longShortRatio", 0))
    except Exception:
        pass

    # ── scoring ──────────────────────────────────────────────────────────────
    score   = 0
    signals = []

    # 1. MA 排列 30pt
    if above_all:
        score += 30
        signals.append({"key":"ma","label":"均線排列","weight":30,"score":30,"ok":True,
            "detail":f"價格在MA30/45/60之上{'，三線多頭排列✨' if ma_fan else ''}"})
    else:
        signals.append({"key":"ma","label":"均線排列","weight":30,"score":0,"ok":False,
            "detail":f"未完全站上三條均線 MA30={ma30:.4f} MA45={ma45:.4f} MA60={ma60:.4f}"})

    # 2. 縮量 20pt
    if shrink is not None:
        pct = round(shrink * 100, 1)
        if shrink > 0.25:
            score += 20
            signals.append({"key":"vol","label":"成交量萎縮","weight":20,"score":20,"ok":True,
                "detail":f"量較均量縮 {pct}%，橫盤儲能中"})
        elif shrink > 0.1:
            score += 10
            signals.append({"key":"vol","label":"成交量萎縮","weight":20,"score":10,"ok":"warn",
                "detail":f"量略縮 {pct}%，不明顯"})
        else:
            signals.append({"key":"vol","label":"成交量萎縮","weight":20,"score":0,"ok":False,
                "detail":f"量無明顯萎縮（{pct}%）"})

    # 3. BB 收窄 20pt
    if bw is not None:
        bw_pct = round(bw * 100, 2)
        if bw < 0.04:
            score += 20
            signals.append({"key":"bb","label":"布林帶極度收窄","weight":20,"score":20,"ok":True,
                "detail":f"帶寬 {bw_pct}%（<4% Squeeze）"})
        elif bw < 0.07:
            score += 10
            signals.append({"key":"bb","label":"布林帶收窄","weight":20,"score":10,"ok":"warn",
                "detail":f"帶寬 {bw_pct}%（收窄中）"})
        else:
            signals.append({"key":"bb","label":"布林帶收窄","weight":20,"score":0,"ok":False,
                "detail":f"帶寬 {bw_pct}%（尚未收窄）"})

    # 4. RSI 15pt
    if rsi is not None:
        r = round(rsi, 1)
        if 45 <= rsi <= 62:
            score += 15
            signals.append({"key":"rsi","label":"RSI蓄力區","weight":15,"score":15,"ok":True,
                "detail":f"RSI {r}（45–62 最佳蓄勢區間）"})
        elif 62 < rsi < 70:
            score += 8
            signals.append({"key":"rsi","label":"RSI偏強","weight":15,"score":8,"ok":"warn",
                "detail":f"RSI {r}（偏強，注意超買）"})
        else:
            signals.append({"key":"rsi","label":"RSI蓄力區","weight":15,"score":0,"ok":False,
                "detail":f"RSI {r}（不在蓄力區）"})

    # 5. 價格橫盤 15pt
    if pr_range is not None:
        pct = round(pr_range * 100, 2)
        if pr_range < 0.04:
            score += 15
            signals.append({"key":"range","label":"價格橫盤壓縮","weight":15,"score":15,"ok":True,
                "detail":f"10日高低幅 {pct}%（橫盤明顯）"})
        elif pr_range < 0.07:
            score += 8
            signals.append({"key":"range","label":"價格橫盤壓縮","weight":15,"score":8,"ok":"warn",
                "detail":f"高低幅 {pct}%（輕微橫盤）"})
        else:
            signals.append({"key":"range","label":"價格橫盤壓縮","weight":15,"score":0,"ok":False,
                "detail":f"高低幅 {pct}%（波動仍大）"})

    # extras
    extras = []
    if funding_rate is not None:
        note = ("正費率（多方付費，市場偏熱）" if funding_rate > 0.01
                else "負費率（空方付費，潛在軋空）" if funding_rate < -0.01
                else "費率中性")
        color = "#ef5350" if funding_rate > 0.05 else "#ff9500" if funding_rate > 0.01 else "#4dd0e1"
        extras.append({"label":"資金費率","value":f"{funding_rate:.4f}%","note":note,"color":color})

    if ls_ratio is not None:
        note = ("多頭過擁擠，注意反轉" if ls_ratio > 1.3
                else "空頭過多，潛在軋空" if ls_ratio < 0.8
                else "多空均衡")
        color = "#ef5350" if ls_ratio > 1.3 else "#ff9500" if ls_ratio < 0.8 else "#4dd0e1"
        extras.append({"label":"多空比 L/S","value":f"{ls_ratio:.3f}","note":note,"color":color})

    if oi_val:
        oi_str = f"{oi_val/1e9:.2f}B" if oi_val > 1e9 else f"{oi_val/1e6:.1f}M"
        extras.append({"label":"未平倉量","value":oi_str,"note":"USDT 計價","color":"#9fa8da"})

    grade_color = ("#ff4d4d" if score >= 85 else "#ff9500" if score >= 65
                   else "#f5c518" if score >= 45 else "#455a64")
    grade       = ("🔥 極強訊號" if score >= 85 else "⚡ 強訊號" if score >= 65
                   else "👀 留意觀察" if score >= 45 else "😴 無明顯")

    return {
        "symbol": symbol,
        "score": score,
        "grade": grade,
        "gradeColor": grade_color,
        "price": f"{last:.4f}",
        "ma30": f"{ma30:.4f}",
        "ma45": f"{ma45:.4f}",
        "ma60": f"{ma60:.4f}",
        "aboveAll": above_all,
        "maFan": ma_fan,
        "signals": signals,
        "extras": extras,
    }

# ── API routes ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/scan")
async def scan(
    symbols: str = Query(default=None),
    interval: str = Query(default="4h"),
    top: int = Query(default=0, description="若 >0，自動使用幣安合約交易量前N名，忽略 symbols 參數"),
):
    async with httpx.AsyncClient(timeout=20) as client:
        if top and top > 0:
            sym_list = await get_top_symbols_by_volume(client, limit=top)
        elif symbols:
            sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            sym_list = DEFAULT_SYMBOLS

        # 分批處理避免單次同時開太多連線（每批20個）
        all_results = []
        batch_size = 20
        for i in range(0, len(sym_list), batch_size):
            batch = sym_list[i:i + batch_size]
            tasks = [fetch_symbol(client, s, interval) for s in batch]
            batch_results = await asyncio.gather(*tasks)
            all_results.extend(batch_results)

    out = [r for r in all_results if r is not None]
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"results": out, "total": len(out), "scanned": len(sym_list)}

@app.get("/top-symbols")
async def top_symbols(limit: int = Query(default=100)):
    """回傳幣安合約交易量排名前N的幣種（不含分析，純清單）"""
    async with httpx.AsyncClient(timeout=15) as client:
        symbols = await get_top_symbols_by_volume(client, limit=limit)
    return {"symbols": symbols, "total": len(symbols)}

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <html><body style="background:#07090f;color:#9fa8da;font-family:monospace;padding:40px">
    <h2>🚀 Crypto Breakout Scanner API</h2>
    <p>Endpoints:</p>
    <ul>
      <li><a href="/scan" style="color:#4dd0e1">/scan</a> — scan default symbols (4h)</li>
      <li><a href="/scan?interval=1h" style="color:#4dd0e1">/scan?interval=1h</a> — 1h timeframe</li>
      <li><a href="/scan?symbols=BTCUSDT,ETHUSDT&interval=1d" style="color:#4dd0e1">/scan?symbols=BTCUSDT,ETHUSDT&interval=1d</a> — custom symbols</li>
      <li><a href="/health" style="color:#4dd0e1">/health</a> — health check</li>
      <li><a href="/docs" style="color:#4dd0e1">/docs</a> — Swagger UI</li>
    </ul>
    </body></html>
    """
