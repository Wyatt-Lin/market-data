"""歷史日序列回填：把每個交易日的關鍵指標整理成 data/series.json，供終端畫連續走勢。

用法：
  python scripts/history.py --start 2026-05-01            # 回填一段期間（第一次用）
  python scripts/history.py --recent 10                   # 每天例行：只補最近 10 天缺的欄位

輸出 data/series.json：
  {"updated_at": "...", "rows": {"YYYY-MM-DD": {"taiex": ..., "margin_ratio": ..., ...}}}
已存在的列會保留；只有拿到新值的欄位才覆蓋。每個資料源獨立 try/except，一個失敗不影響其他。

融資維持率（自算）口徑與 fetch.py 相同：Σ(個股融資今日餘額張 × 1000 × 當日收盤價) ÷ 融資金額(元)。
"""
import argparse
import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TPE = timezone(timedelta(hours=8))
NOW = datetime.now(TPE)
ROOT = Path(__file__).resolve().parent.parent / "data"
OUT = ROOT / "series.json"
UA = {"User-Agent": "Mozilla/5.0 (market-data research bot)", "Accept": "application/json,text/plain,*/*"}
LOG = []
TWSE_GAP = 3.2  # 證交所約 5 秒 3 次以上會擋 IP，保守一點


def log(src, ok, msg=""):
    line = f"{NOW.isoformat(timespec='minutes')}\thistory:{src}\t{'ok' if ok else 'ERROR'}\t{msg}"
    LOG.append(line)
    print(line, flush=True)


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("%", "").replace("X", "").strip()
    if s in ("", "-", "--", "---", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    s = str(s).strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


_last_twse = [0.0]


def twse(path, params):
    wait = TWSE_GAP - (time.time() - _last_twse[0])
    if wait > 0:
        time.sleep(wait)
    for i in range(3):
        try:
            r = requests.get("https://www.twse.com.tw/rwd/zh/" + path, params={**params, "response": "json"},
                             headers=UA, timeout=40)
            _last_twse[0] = time.time()
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(6 * (i + 1))
    raise last


def http(url, method="GET", **kw):
    last = None
    for i in range(3):
        try:
            r = requests.request(method, url, headers=UA, timeout=40, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise last


# ---------------------------------------------------------------- 台股（證交所）
def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def taiex_month(rows, start, end):
    """FMTQIK：每月一次請求，取得交易日清單、加權指數與成交值。"""
    days = []
    for y, m in months(start, end):
        try:
            d = twse("afterTrading/FMTQIK", {"date": f"{y}{m:02d}01"})
            for r in d.get("data") or []:
                day = roc_to_iso(r[0])
                if not (start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d")):
                    continue
                row = rows.setdefault(day, {})
                row["taiex"] = num(r[4])
                row["taiex_chg"] = num(r[5])
                if row["taiex"] and row["taiex_chg"] is not None:
                    row["taiex_pct"] = round(row["taiex_chg"] / (row["taiex"] - row["taiex_chg"]) * 100, 2)
                row["turnover"] = round(num(r[2]) / 1e8, 2) if num(r[2]) else None
                days.append(day)
            log(f"fmtqik_{y}{m:02d}", True, f"{len(d.get('data') or [])} rows")
        except Exception as e:  # noqa: BLE001
            log(f"fmtqik_{y}{m:02d}", False, repr(e)[:200])
    return sorted(set(days))


def flows_day(row, day):
    d = twse("fund/BFI82U", {"dayDate": day.replace("-", ""), "type": "day"})
    vals = {r[0]: num(r[3]) for r in d.get("data", []) if len(r) >= 4}
    yi = lambda v: None if v is None else round(v / 1e8, 2)
    if not vals:
        raise ValueError("empty")
    row["foreign"] = yi(sum(v for k, v in vals.items() if k.strip().startswith("外資") and v is not None))
    row["trust"] = yi(next((v for k, v in vals.items() if "投信" in k), None))
    row["dealer"] = yi(sum(v for k, v in vals.items() if k.strip().startswith("自營商") and v is not None))


def margin_day(row, day):
    ymd = day.replace("-", "")
    d = twse("marginTrading/MI_MARGN", {"date": ymd, "selectType": "ALL"})
    if str(d.get("date")) != ymd:
        raise ValueError(f"margin date {d.get('date')} != {ymd}")
    tables = d.get("tables") or []
    summary = next((t for t in tables if "信用交易統計" in (t.get("title") or "")), tables[0] if tables else None)
    per_stock = next((t for t in tables if t is not summary and len(t.get("data") or []) > 50), None)
    loan_k = prev_k = None
    for r in (summary or {}).get("data", []):
        if "融資金額" in str(r[0]):
            loan_k, prev_k = num(r[-1]), num(r[-2])
    if not loan_k:
        raise ValueError("no 融資金額")
    row["margin_bal"] = round(loan_k / 1e5, 2)
    if prev_k:
        row["margin_chg"] = round((loan_k - prev_k) / 1e5, 2)
    if not per_stock:
        return
    fields = per_stock.get("fields", [])
    i_bal = next((i for i, f in enumerate(fields) if "今日餘額" in f), None)
    px = twse("afterTrading/MI_INDEX", {"date": ymd, "type": "ALLBUT0999"})
    if str(px.get("date")) != ymd:
        raise ValueError(f"close date {px.get('date')} != {ymd}")
    closes = {}
    for t in px.get("tables") or []:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ic, ip = f.index("證券代號"), f.index("收盤價")
            for r in t.get("data") or []:
                c = num(r[ip])
                if c:
                    closes[str(r[ic]).strip()] = c
    if i_bal is None or not closes:
        return
    mv = sum(num(r[i_bal]) * 1000 * closes[str(r[0]).strip()]
             for r in per_stock.get("data", []) if num(r[i_bal]) and str(r[0]).strip() in closes)
    if mv > 0:
        row["margin_ratio"] = round(mv / (loan_k * 1000) * 100, 2)


# ---------------------------------------------------------------- 期交所（CSV 區間下載，Big5）
def taifex_csv(url, start, end, extra):
    """逐月下載，回傳 [dict(header→value)]。"""
    out = []
    for y, m in months(start, end):
        a = max(start, datetime(y, m, 1, tzinfo=TPE))
        b = min(end, (datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=TPE) - timedelta(days=1)))
        data = {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d"), **extra}
        try:
            r = http(url, "POST", data=data)
            text = r.content.decode("big5", "ignore") if b"," in r.content else ""
            rd = list(csv.reader(io.StringIO(text)))
            if len(rd) < 2:
                raise ValueError(f"no csv rows ({len(r.content)} bytes)")
            head = [h.strip() for h in rd[0]]
            for line in rd[1:]:
                if len(line) >= len(head) - 1:
                    out.append({head[i]: line[i].strip() for i in range(min(len(head), len(line)))})
            log(f"taifex_{url.rsplit('/', 1)[-1]}_{y}{m:02d}", True, f"{len(rd) - 1} rows")
        except Exception as e:  # noqa: BLE001
            log(f"taifex_{url.rsplit('/', 1)[-1]}_{y}{m:02d}", False, repr(e)[:200])
        time.sleep(1.5)
    return out


def getk(d, *frags):
    for f in frags:
        for k, v in d.items():
            if f in k:
                return v
    return None


def taifex_all(rows, start, end):
    for r in taifex_csv("https://www.taifex.com.tw/cht/3/futContractsDateDown", start, end, {"commodityId": "TXF"}):
        who = getk(r, "身份別") or ""
        if "外資" not in who:
            continue
        day = (getk(r, "日期") or "").replace("/", "-")
        v = num(getk(r, "多空淨額未平倉口數", "多空未平倉口數淨額"))
        if day and v is not None:
            rows.setdefault(day, {})["foreign_fut_net_oi"] = v
    for r in taifex_csv("https://www.taifex.com.tw/cht/3/pcRatioDown", start, end, {}):
        day = (getk(r, "日期") or "").replace("/", "-")
        v = num(getk(r, "未平倉量比率"))
        if day and v is not None:
            rows.setdefault(day, {})["pc_ratio"] = v


# ---------------------------------------------------------------- Yahoo / FRED
YAHOO = {"otc": "^TWOII", "twd": "TWD=X", "tsmc": "2330.TW", "sox": "^SOX", "spx": "^GSPC", "nasdaq": "^IXIC",
         "tsm_adr": "TSM", "vix": "^VIX", "vix3m": "^VIX3M", "move": "^MOVE", "us10y": "^TNX"}


def yahoo_all(rows, start, end):
    p1 = int((start - timedelta(days=3)).timestamp())
    p2 = int((end + timedelta(days=1)).timestamp())
    for field, sym in YAHOO.items():
        try:
            d = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"period1": p1, "period2": p2, "interval": "1d"}).json()
            res = d["chart"]["result"][0]
            tz = timezone(timedelta(seconds=res["meta"].get("gmtoffset", 0)))
            n = 0
            for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"]):
                if c is None:
                    continue
                day = datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")
                if start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d"):
                    rows.setdefault(day, {})[field] = round(c / 10 if sym == "^TNX" and c > 20 else c, 4)
                    n += 1
            log(f"yahoo_{sym}", n > 0, f"{n} days")
        except Exception as e:  # noqa: BLE001
            log(f"yahoo_{sym}", False, repr(e)[:200])
        time.sleep(0.4)


FRED = {"stlfsi4": "STLFSI4", "nfci": "NFCI", "real10y": "DFII10"}


def fred_all(rows, start, end):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        log("fred", False, "FRED_API_KEY missing")
        return
    for field, sid in FRED.items():
        try:
            d = http("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": sid, "api_key": key, "file_type": "json",
                             "observation_start": start.strftime("%Y-%m-%d"),
                             "observation_end": end.strftime("%Y-%m-%d")}).json()
            n = 0
            for o in d.get("observations", []):
                if num(o.get("value")) is not None:
                    rows.setdefault(o["date"], {})[field] = num(o["value"])
                    n += 1
            log(f"fred_{sid}", n > 0, f"{n} obs")
        except Exception as e:  # noqa: BLE001
            log(f"fred_{sid}", False, repr(e)[:200])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD（預設今天）")
    ap.add_argument("--recent", type=int, help="只處理最近 N 個日曆日")
    a = ap.parse_args()
    end = datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=TPE) if a.end else NOW
    if a.recent:
        start = NOW - timedelta(days=a.recent)
    elif a.start:
        start = datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=TPE)
    else:
        start = NOW - timedelta(days=180)

    store = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"rows": {}}
    old = store.get("rows", {})
    new = {}

    trading_days = taiex_month(new, start, end)
    for day in trading_days:
        prev = old.get(day, {})
        row = new.setdefault(day, {})
        if prev.get("foreign") is None:
            try:
                flows_day(row, day)
            except Exception as e:  # noqa: BLE001
                log(f"flows_{day}", False, repr(e)[:150])
        if prev.get("margin_ratio") is None:
            try:
                margin_day(row, day)
            except Exception as e:  # noqa: BLE001
                log(f"margin_{day}", False, repr(e)[:150])
    log("twse_days", True, f"{len(trading_days)} trading days {start:%Y-%m-%d}..{end:%Y-%m-%d}")

    taifex_all(new, start, end)
    yahoo_all(new, start, end)
    fred_all(new, start, end)

    # ADR 溢價：TSM × 匯率 ÷ 5，對比同一日期（或之前最近）的台積電收盤
    tw_close = sorted((d, r.get("tsmc") or old.get(d, {}).get("tsmc")) for d, r in {**old, **new}.items())
    tw_close = [(d, c) for d, c in tw_close if c]
    for d, r in new.items():
        adr, fx = r.get("tsm_adr"), r.get("twd") or old.get(d, {}).get("twd")
        ref = [c for dd, c in tw_close if dd <= d]
        if adr and fx and ref:
            r["adr_prem"] = round((adr * fx / 5 / ref[-1] - 1) * 100, 2)

    for d, r in new.items():
        merged = old.get(d, {})
        merged.update({k: v for k, v in r.items() if v is not None})
        if merged:
            old[d] = merged
    store = {"updated_at": NOW.isoformat(timespec="minutes"), "method": {
        "margin_ratio": "自算：Σ(融資今日餘額張×1000×收盤價)÷融資金額，上市",
        "otc": "Yahoo ^TWOII 日收盤", "twd": "Yahoo TWD=X 日收盤（非台北 16:00 收盤）"},
        "rows": dict(sorted(old.items()))}
    OUT.write_text(json.dumps(store, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with open(ROOT / "log.txt", "a", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    print(f"done: {len(old)} rows in series.json")


if __name__ == "__main__":
    main()
