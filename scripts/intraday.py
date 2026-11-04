"""盤中快照：從 Yahoo Finance 抓當日 5 分鐘線，寫入 data/intraday.json。
台美晨報終端的盤中排程會讀這個檔案。只用標準函式庫。"""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))
# key, Yahoo 代號, 名稱, 市場, 小數位, 單位
SYMBOLS = [
    ("taiex",  "^TWII",   "加權指數",   "tw", 2, ""),
    ("otc",    "^TWOII",  "櫃買指數",   "tw", 2, ""),
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

def fetch(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=5m&range=1d"
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
    pts = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        pts.append({"t": datetime.fromtimestamp(t, TPE).strftime("%H:%M"), "v": round(c * scale, 4)})
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    last = round(last * scale, 4) if last is not None else (pts[-1]["v"] if pts else None)
    prev = round(prev * scale, 4) if prev is not None else None
    rmt = meta.get("regularMarketTime")
    session = datetime.fromtimestamp(rmt, tz).strftime("%Y-%m-%d") if rmt else None
    return {
        "label": label, "market": mkt, "symbol": sym, "dp": dp, "unit": unit,
        "session_date": session,
        "last": last, "prev_close": prev,
        "chg": round(last - prev, 4) if last is not None and prev else None,
        "pct": round((last / prev - 1) * 100, 3) if last is not None and prev else None,
        "last_time": datetime.fromtimestamp(rmt, TPE).strftime("%Y-%m-%d %H:%M") if rmt else None,
        "points": pts,
    }

def main():
    out = {"updated_at": datetime.now(TPE).strftime("%Y-%m-%dT%H:%M+08:00"), "series": {}, "errors": {}}
    for row in SYMBOLS:
        try:
            out["series"][row[0]] = build(*row)
        except Exception as e:  # 單一代號失敗不影響其他
            out["errors"][row[0]] = f"{type(e).__name__}: {e}"[:200]
        time.sleep(0.5)
    s = out["series"]
    # ADR 溢價：TSM × 匯率 ÷ 5 對比台積電最新價（台股收盤後即為收盤價）
    try:
        fx, tw = s["twd"]["last"], s["tsmc"]["last"]
        prem = [{"t": p["t"], "v": round((p["v"] * fx / 5 / tw - 1) * 100, 3)} for p in s["tsm"]["points"]]
        s["adr_prem"] = {"label": "TSM ADR 溢價", "market": "us", "symbol": "TSM×TWD÷5÷2330", "dp": 2, "unit": "%",
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
