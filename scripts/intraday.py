"""盤中快照：從 Yahoo Finance 抓當日 5 分鐘線，寫入 data/intraday.json。
台美晨報終端的盤中排程會讀這個檔案。只用標準函式庫。"""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))
# key, Yahoo 代號, 名稱, 市場, 小數位, 單位
SYMBOLS = [
    ("taiex",  "^TWII",   "加權指數",   "tw", 2, ""),
    ("tsmc",   "2330.TW", "台積電",     "tw", 0, ""),
    ("twd",    "TWD=X",   "新台幣",     "tw", 3, ""),
    ("sox",    "^SOX",    "費城半導體", "us", 2, ""),
    ("spx",    "^GSPC",   "S&P 500",    "us", 2, ""),
    ("nasdaq", "^IXIC",   "Nasdaq",     "us", 2, ""),
    ("tsm",    "TSM",     "TSM ADR",    "us", 2, ""),
    ("vix",    "^VIX",    "VIX",        "us", 2, ""),
    ("us10y",  "^TNX",    "美債 10Y",   "us", 3, "%"),
]
UA = {"User-Agent": "Mozilla/5.0 (market-data intraday)"}

def fetch(sym, interval="5m", rng="5d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval={interval}&range={rng}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["chart"]["result"][0]

def build(key, sym, label, mkt, dp, unit):
    res = fetch(sym)
    meta = res["meta"]
    tz = timezone(timedelta(seconds=meta.get("gmtoffset", 0)))
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    scale = 0.1 if key == "us10y" and (meta.get("regularMarketPrice") or 0) > 20 else 1
    # 取 5 日 5 分鐘線，只保留最後一個交易日（以交易所時區分日）
    bars = [(t, c) for t, c in zip(ts, closes) if c is not None]
    session = datetime.fromtimestamp(bars[-1][0], tz).strftime("%Y-%m-%d") if bars else None
    today = [(t, c) for t, c in bars if datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d") == session]
    earlier = [(t, c) for t, c in bars if session and datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d") < session]
    pts = [{"t": datetime.fromtimestamp(t, TPE).strftime("%H:%M"), "v": round(c * scale, 4)} for t, c in today]
    # Yahoo 的 meta（尤其 ^TWII）有時停在前一日，所以最新價用最後一根 K 棒，前收用前一交易日最後一根
    rmt = meta.get("regularMarketTime")
    meta_day = datetime.fromtimestamp(rmt, tz).strftime("%Y-%m-%d") if rmt else None
    mp = meta.get("regularMarketPrice")
    if today:
        last = round(today[-1][1] * scale, 4)
        if meta_day == session and mp is not None:
            last = round(mp * scale, 4)
        last_ts = today[-1][0] if meta_day != session else max(rmt, today[-1][0])
    else:
        last, last_ts = (round(mp * scale, 4) if mp is not None else None), rmt
    # 前收用日 K 的官方收盤（5 分鐘線最後一根不一定等於收盤價）
    prev = None
    try:
        if key == "twd":  # 外匯日 K 收盤時點不固定，改用前一日最後一根 5 分鐘線
            raise ValueError("fx")
        d = fetch(sym, "1d", "10d")
        dtz = timezone(timedelta(seconds=d["meta"].get("gmtoffset", 0)))
        daily = [(datetime.fromtimestamp(t, dtz).strftime("%Y-%m-%d"), c)
                 for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0]["close"]) if c is not None]
        before = [(day, c) for day, c in daily if session and day < session]
        # 日 K 在開盤初期常缺前一交易日那根（11/06 曾拿到 11/04 收盤）：
        # 只有當日 K 的日期等於 5 分鐘線裡的前一交易日時才採用，否則改用前一交易日最後一根 5 分鐘線
        prev_day_5m = datetime.fromtimestamp(earlier[-1][0], tz).strftime("%Y-%m-%d") if earlier else None
        if before and (prev_day_5m is None or before[-1][0] >= prev_day_5m):
            prev = round(before[-1][1] * scale, 4)
    except Exception:
        pass
    if prev is None and earlier:
        prev = round(earlier[-1][1] * scale, 4)
    rmt = last_ts
    return {
        "label": label, "market": mkt, "symbol": sym, "dp": dp, "unit": unit,
        "session_date": session,
        "last": last, "prev_close": prev,
        "chg": round(last - prev, 4) if last is not None and prev else None,
        "pct": round((last / prev - 1) * 100, 3) if last is not None and prev else None,
        "last_time": datetime.fromtimestamp(rmt, TPE).strftime("%Y-%m-%d %H:%M") if rmt else None,
        "points": pts,
    }

# ---------------------------------------------------------------- 台指期夜盤（期交所 MIS，台北 15:00–次日 05:00）
MIS = "https://mis.taifex.com.tw/futures/api/"


def mis_post(path, body):
    req = urllib.request.Request(MIS + path, data=json.dumps(body).encode(), method="POST",
                                 headers={**UA, "Content-Type": "application/json", "Origin": "https://mis.taifex.com.tw",
                                          "Referer": "https://mis.taifex.com.tw/futures/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def night_session_date(now):
    """夜盤 15:00 開盤、次日 05:00 收盤；00:00–05:00 的點屬於前一天開始的那一盤。"""
    return (now - timedelta(days=1) if now.hour < 12 else now).strftime("%Y-%m-%d")


def tx_night(prev):
    """回傳台指期近月夜盤序列。先試 1 分 K（getChartData1M），失敗就用即時報價，接在上一份檔案的點後面。"""
    now = datetime.now(TPE)
    ses = night_session_date(now)
    if not (now.hour >= 15 or now.hour < 6):
        # 日盤時段：保留上一份檔案的夜盤（若是同一盤）供終端顯示，不重抓
        old = (prev or {}).get("series", {}).get("tx_night")
        return old  # 沒有就回 None，不算錯誤
    q = mis_post("getQuoteList", {"MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "TXF",
                                  "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"})
    rows = (q.get("RtData") or {}).get("QuoteList") or []
    rows = [r for r in rows if r.get("SymbolID") and r.get("CLastPrice") not in (None, "", "0")]
    if not rows:
        raise RuntimeError(f"no quote rows; keys={list(q)[:5]}")
    # 近月 = 成交量最大的月合約（避開週選與遠月）
    near = max(rows, key=lambda r: float(str(r.get("CTotalVolume") or 0).replace(",", "") or 0))
    sym = near["SymbolID"]
    last = float(str(near["CLastPrice"]).replace(",", ""))
    ref = near.get("CRefPrice")
    ref = float(str(ref).replace(",", "")) if ref not in (None, "", "0") else None
    pts = []
    try:  # 1 分 K：一次拿到整盤，取 5 分鐘一點
        c = mis_post("getChartData1M", {"SymbolID": sym})
        ticks = (c.get("RtData") or {}).get("Ticks") or (c.get("RtData") or {}).get("ChartData") or []
        for t in ticks:
            tm = str(t.get("Time") or t.get("time") or t.get("T") or "")
            px = t.get("Close") or t.get("Price") or t.get("C")
            if len(tm) >= 4 and px not in (None, ""):
                hhmm = tm[-6:-2] if len(tm) >= 6 else tm[-4:]
                if hhmm[-1] in "05":  # 每 5 分鐘一點
                    pts.append({"t": f"{hhmm[:2]}:{hhmm[2:]}", "v": round(float(px), 1)})
    except Exception as e:  # noqa: BLE001
        print("tx_night chart fallback:", repr(e)[:150])
    if not pts:  # 退而求其次：把這次的即時價接在同一盤的舊點後面
        old = (prev or {}).get("series", {}).get("tx_night")
        if old and old.get("session_date") == ses:
            pts = list(old.get("points") or [])
        stamp = now.strftime("%H:%M")
        if not pts or pts[-1]["t"] != stamp:
            pts.append({"t": stamp, "v": last})
    return {"label": "台指期夜盤", "market": "tw", "symbol": sym, "dp": 0, "unit": "", "session_date": ses,
            "last": last, "prev_close": ref, "chg": round(last - ref, 1) if ref else None,
            "pct": round((last / ref - 1) * 100, 3) if ref else None,
            "last_time": now.strftime("%Y-%m-%d %H:%M"), "points": pts, "night": True}


def main():
    out = {"updated_at": datetime.now(TPE).strftime("%Y-%m-%dT%H:%M+08:00"), "series": {}, "errors": {}}
    for row in SYMBOLS:
        try:
            out["series"][row[0]] = build(*row)
        except Exception as e:  # 單一代號失敗不影響其他
            out["errors"][row[0]] = f"{type(e).__name__}: {e}"[:200]
        time.sleep(0.3)
    s = out["series"]
    try:
        prev = json.load(open("data/intraday.json", encoding="utf-8")) if os.path.exists("data/intraday.json") else None
        tn = tx_night(prev)
        if tn:
            s["tx_night"] = tn
    except Exception as e:  # noqa: BLE001
        out["errors"]["tx_night"] = f"{type(e).__name__}: {e}"[:200]
    # ADR 溢價：TSM × 匯率 ÷ 5，對比「與 ADR 同一日期」的台積電收盤（台股先收盤、美股後開盤）
    try:
        fx = s["twd"]["last"]
        d = fetch("2330.TW", "1d", "10d")
        dtz = timezone(timedelta(seconds=d["meta"].get("gmtoffset", 28800)))
        closes_by_day = {datetime.fromtimestamp(t, dtz).strftime("%Y-%m-%d"): c
                         for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0]["close"]) if c is not None}
        ref_day = max(k for k in closes_by_day if k <= s["tsm"]["session_date"])
        tw = closes_by_day[ref_day]
        prem = [{"t": p["t"], "v": round((p["v"] * fx / 5 / tw - 1) * 100, 3)} for p in s["tsm"]["points"]]
        s["adr_prem"] = {"label": "TSM ADR 溢價", "market": "us", "symbol": f"TSM×TWD÷5 vs 2330 {ref_day} 收盤 {tw:g}", "dp": 2, "unit": "%",
                         "session_date": s["tsm"]["session_date"], "last": prem[-1]["v"] if prem else None,
                         "prev_close": None, "chg": None, "pct": None, "last_time": s["tsm"]["last_time"], "points": prem}
    except Exception as e:
        out["errors"]["adr_prem"] = f"{type(e).__name__}: {e}"[:200]
    os.makedirs("data", exist_ok=True)
    with open("data/intraday.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"ok {len(s)} series, errors={list(out['errors'])}")

if __name__ == "__main__":
    main()
