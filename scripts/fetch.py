"""Daily Taiwan / US market and financial-stress snapshot.

Run modes:
  python scripts/fetch.py tw   # after Taiwan close (~15:40 Taipei)
  python scripts/fetch.py us   # after US close   (~05:40 Taipei)

Outputs (committed back to the repo by the workflow):
  data/latest.json              most recent value of every field, with as-of dates
  data/history/YYYY-MM-DD.json  snapshot per Taipei calendar date (tw and us blocks merged)
  data/raw/YYYY-MM-DD/*.json    raw responses, kept so a parser bug never loses a day
  data/log.txt                  one line per source per run (ok / error)

Every source is wrapped independently: one failure never stops the others.
"""
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TPE = timezone(timedelta(hours=8))
NOW = datetime.now(TPE)
TODAY = NOW.strftime("%Y-%m-%d")
ROOT = Path(__file__).resolve().parent.parent / "data"
RAW = ROOT / "raw" / TODAY
UA = {"User-Agent": "Mozilla/5.0 (market-data research bot)", "Accept": "application/json,text/plain,*/*"}
LOG = []


def log(src, ok, msg=""):
    line = f"{NOW.isoformat(timespec='minutes')}\t{src}\t{'ok' if ok else 'ERROR'}\t{msg}"
    LOG.append(line)
    print(line)


def get(url, params=None, as_json=True, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            r.raise_for_status()
            if as_json:
                r.encoding = r.encoding or "utf-8"
                return r.json()
            return r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def save_raw(name, obj):
    RAW.mkdir(parents=True, exist_ok=True)
    p = RAW / f"{name}.json"
    if isinstance(obj, (bytes, bytearray)):
        p.with_suffix(".bin").write_bytes(obj)
    else:
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


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


def pick(row, *keys):
    """Return the first value whose key contains any of the given fragments."""
    for frag in keys:
        for k, v in row.items():
            if frag in str(k):
                return v
    return None


# ---------------------------------------------------------------- Taiwan
def roc_to_iso(s):
    """'115/10/28' or '1151028' -> '2026-10-28'."""
    s = str(s).strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def twse_daily(out):
    """Same-day TAIEX close, change and turnover (the openapi MI_INDEX lags a day)."""
    d = get("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK", {"response": "json"})
    save_raw("twse_fmtqik", d)
    rows = d.get("data") or []
    if rows:
        r = rows[-1]
        out["session_date"] = roc_to_iso(r[0])
        out["turnover"] = round(num(r[2]) / 1e8, 2) if num(r[2]) else None
        out["taiex"] = num(r[4])
        out["taiex_chg"] = num(r[5])
        if out.get("taiex") and out.get("taiex_chg") is not None:
            out["taiex_pct"] = round(out["taiex_chg"] / (out["taiex"] - out["taiex_chg"]) * 100, 2)
        out["taiex_date"] = out["session_date"]
    log("twse_daily", out.get("taiex") is not None, f"date={out.get('session_date')}")


def twse_index(out):
    if out.get("taiex") is not None:
        return  # already filled from the same-day source
    d = get("https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX")
    save_raw("twse_mi_index", d)
    for row in d:
        name = pick(row, "指數", "Index")
        if name and "發行量加權股價指數" == str(name).strip():
            out["taiex"] = num(pick(row, "收盤指數", "Close"))
            sign = str(pick(row, "漲跌", "Dir") or "")
            chg = num(pick(row, "漲跌點數", "Change"))
            if chg is not None and sign.strip() in ("-", "－"):
                chg = -chg
            out["taiex_chg"] = chg
            out["taiex_pct"] = num(pick(row, "漲跌百分比", "Percent"))
            out["taiex_date"] = pick(row, "日期", "Date")
    log("twse_index", "taiex" in out)


def twse_flows(out):
    d = get("https://www.twse.com.tw/rwd/zh/fund/BFI82U", {"response": "json"})
    save_raw("twse_bfi82u", d)
    rows = d.get("data", [])
    vals = {r[0]: num(r[3]) for r in rows if len(r) >= 4}
    yi = lambda v: None if v is None else round(v / 1e8, 2)  # 元 -> 億元
    # row names: 自營商(自行買賣) / 自營商(避險) / 投信 / 外資及陸資(不含外資自營商) / 外資自營商 / 合計
    # "外資及陸資(不含外資自營商)" contains the word 自營商, so match on the row's start.
    foreign = sum(v for k, v in vals.items() if k.strip().startswith("外資") and v is not None)
    dealer = sum(v for k, v in vals.items() if k.strip().startswith("自營商") and v is not None)
    out["dealer_self"] = yi(next((v for k, v in vals.items() if k.strip().startswith("自營商") and "自行" in k), None))
    out["dealer_hedge"] = yi(next((v for k, v in vals.items() if k.strip().startswith("自營商") and "避險" in k), None))
    out["foreign"] = yi(foreign)
    out["trust"] = yi(next((v for k, v in vals.items() if "投信" in k), None))
    out["dealer"] = yi(dealer)
    out["inst_total"] = yi(next((v for k, v in vals.items() if "合計" in k), None))
    out["flows_date"] = d.get("date")
    log("twse_flows", out.get("trust") is not None)


def twse_margin(out):
    """Market margin balance + maintenance ratio computed from per-stock data."""
    d = get("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN", {"response": "json", "selectType": "ALL"})
    save_raw("twse_mi_margn_all", d)
    tables = d.get("tables") or []
    summary = next((t for t in tables if "信用交易統計" in (t.get("title") or "")), tables[0] if tables else None)
    per_stock = next((t for t in tables if t is not summary and len(t.get("data") or []) > 50), None)

    # summary: row "融資金額(仟元)" -> 今日餘額 is the last column
    loan_k = prev_k = None
    if summary:
        for r in summary.get("data", []):
            if "融資金額" in str(r[0]):
                loan_k, prev_k = num(r[-1]), num(r[-2])
    if loan_k:
        out["margin_bal"] = round(loan_k / 1e5, 2)  # 仟元 -> 億元
        if prev_k:
            out["margin_chg"] = round((loan_k - prev_k) / 1e5, 2)

    # maintenance ratio = sum(融資餘額張 * 1000 * 收盤價) / 融資金額
    if per_stock and loan_k:
        fields = per_stock.get("fields", [])
        try:
            i_code = 0
            i_bal = next(i for i, f in enumerate(fields) if "今日餘額" in f)  # first 今日餘額 = 融資
        except StopIteration:
            i_bal = None
        closes = {}
        try:  # same-day closes first
            px = get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX", {"response": "json", "type": "ALLBUT0999"})
            save_raw("twse_mi_index_all", px)
            for t in px.get("tables") or []:
                f = t.get("fields") or []
                if "證券代號" in f and "收盤價" in f:
                    ic, ip = f.index("證券代號"), f.index("收盤價")
                    for r in t.get("data") or []:
                        c = num(r[ip])
                        if c:
                            closes[str(r[ic]).strip()] = c
            out["margin_px_date"] = px.get("date")
        except Exception as e:  # noqa: BLE001
            log("twse_close_sameday", False, repr(e)[:200])
        try:
            if closes:
                raise StopIteration  # skip the lagging fallback
            px = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
            save_raw("twse_stock_day_all", px)
            for r in px:
                c = num(pick(r, "ClosingPrice", "收盤價"))
                code = pick(r, "Code", "證券代號")
                if code and c:
                    closes[str(code).strip()] = c
        except StopIteration:
            pass
        except Exception as e:  # noqa: BLE001
            log("twse_close", False, repr(e)[:200])
        if i_bal is not None and closes:
            mv = 0.0
            for r in per_stock.get("data", []):
                code = str(r[i_code]).strip()
                bal = num(r[i_bal])
                if bal and code in closes:
                    mv += bal * 1000 * closes[code]
            if mv > 0:
                out["margin_ratio"] = round(mv / (loan_k * 1000) * 100, 2)
    log("twse_margin", out.get("margin_bal") is not None, f"ratio={out.get('margin_ratio')}")


def taifex(out):
    base = "https://openapi.taifex.com.tw/v1/"
    # Put/Call ratio
    pc = get(base + "PutCallRatio")
    save_raw("taifex_pcratio", pc)
    if isinstance(pc, list) and pc:
        row = sorted(pc, key=lambda r: str(pick(r, "Date", "日期")))[-1]
        out["pc_ratio"] = num(pick(row, "PutCallOIRatio", "未平倉量比率", "OIRatio"))
        out["pc_date"] = pick(row, "Date", "日期")
    log("taifex_pc", out.get("pc_ratio") is not None)

    # Foreign investors TX futures open interest
    try:
        fi = get(base + "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
        save_raw("taifex_inst_futures", fi)
        for r in fi if isinstance(fi, list) else []:
            prod = str(pick(r, "ContractCode", "商品名稱", "Contract") or "")
            who = str(pick(r, "Item", "身份別", "Identity") or "")
            if ("TX" == prod.strip() or "臺股期貨" in prod) and ("外資" in who or "Foreign" in who):
                out["foreign_fut_net_oi"] = num(pick(r, "OpenInterest(Net)", "未平倉餘額多空淨額口數", "NetOI", "OI(Net)"))
                out["foreign_fut_date"] = pick(r, "Date", "日期")
        log("taifex_foreign_oi", out.get("foreign_fut_net_oi") is not None)
    except Exception as e:  # noqa: BLE001
        log("taifex_foreign_oi", False, repr(e)[:200])

    # TAIEX futures daily (near month close)
    try:
        fut = get(base + "DailyMarketReportFut")
        save_raw("taifex_daily_fut", fut)
        tx = [r for r in fut if str(pick(r, "Contract", "契約")).strip() == "TX"
              and "/" not in str(pick(r, "ContractMonth(Week)", "到期月份")) and str(pick(r, "TradingSession", "交易時段")).find("一般") >= 0]
        if tx:
            tx.sort(key=lambda r: str(pick(r, "ContractMonth(Week)", "到期月份")))
            out["fut_close"] = num(pick(tx[0], "Last", "收盤價", "Close"))
        log("taifex_fut", out.get("fut_close") is not None)
    except Exception as e:  # noqa: BLE001
        log("taifex_fut", False, repr(e)[:200])


def taifex_vix(out):
    """TAIFEX VIX: daily close from the published txt file (fallback: leave empty)."""
    ym = NOW.strftime("%Y%m")
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vixMinNew/{ym}{NOW.strftime('%d')}new.txt"
    try:
        raw = get(url, as_json=False)
        save_raw("taifex_vix", raw)
        lines = [l for l in raw.decode("big5", "ignore").splitlines() if l.strip()]
        last = lines[-1].split()
        out["tw_vix"] = num(last[-1])
        log("taifex_vix", out["tw_vix"] is not None)
    except Exception as e:  # noqa: BLE001
        log("taifex_vix", False, repr(e)[:200])


def tpex_index(out):
    for url in ("https://www.tpex.org.tw/openapi/v1/tpex_index",
                "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_highlight"):
        try:
            d = get(url)
            save_raw("tpex_" + url.rsplit("/", 1)[-1], d)
            rows = d if isinstance(d, list) else []
            for r in rows:
                v = num(pick(r, "Close", "收盤", "TPExIndex", "指數"))
                if v and v > 50:
                    out["otc"] = v
                    out["otc_chg"] = num(pick(r, "Change", "漲跌"))
                    out["otc_date"] = roc_to_iso(pick(r, "Date", "日期") or "")
                    break
            if "otc" in out:
                break
        except Exception as e:  # noqa: BLE001
            log("tpex", False, f"{url}: {repr(e)[:150]}")
    log("tpex_index", "otc" in out)


# ---------------------------------------------------------------- US
# ICE BofA OAS series (BAMLH0A0HYM2, BAMLC0A0CM) are left out on purpose: FRED's licence for ICE data
# does not allow republishing it in a public repo. The morning report reads those two from the FRED page.
FRED = {
    "real10y": "DFII10",
    "us10y": "DGS10",
    "us2y": "DGS2",
    "vix_fred": "VIXCLS",
    "stlfsi4": "STLFSI4",
    "nfci": "NFCI",
    "dxy_broad": "DTWEXBGS",
}


def fred(out):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        log("fred", False, "FRED_API_KEY secret missing")
        return
    start = (NOW - timedelta(days=40)).strftime("%Y-%m-%d")
    for field, sid in FRED.items():
        try:
            d = get("https://api.stlouisfed.org/fred/series/observations",
                    {"series_id": sid, "api_key": key, "file_type": "json", "observation_start": start})
            obs = [o for o in d.get("observations", []) if num(o.get("value")) is not None]
            if obs:
                out[field] = num(obs[-1]["value"])
                out[field + "_date"] = obs[-1]["date"]
                if len(obs) > 1:
                    out[field + "_chg"] = round(num(obs[-1]["value"]) - num(obs[-2]["value"]), 4)
            log(f"fred_{sid}", field in out)
        except Exception as e:  # noqa: BLE001
            log(f"fred_{sid}", False, repr(e)[:200])


YAHOO = {"vix": "^VIX", "vix3m": "^VIX3M", "move": "^MOVE", "sox": "^SOX", "tsm_adr": "TSM",
         "spx": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI", "rut": "^RUT", "wti": "CL=F", "brent": "BZ=F",
         "gold": "GC=F", "dxy": "DX-Y.NYB", "usdtwd": "TWD=X", "us10y_live": "^TNX"}


def yahoo(out):
    for field, sym in YAHOO.items():
        try:
            d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": "5d", "interval": "1d"})
            res = d["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            ts = res["timestamp"]
            if closes:
                out[field] = round(closes[-1], 4)
                out[field + "_date"] = datetime.fromtimestamp(ts[len(closes) - 1], timezone.utc).strftime("%Y-%m-%d")
                if len(closes) > 1:
                    out[field + "_pct"] = round((closes[-1] / closes[-2] - 1) * 100, 3)
            log(f"yahoo_{sym}", field in out)
        except Exception as e:  # noqa: BLE001
            log(f"yahoo_{sym}", False, repr(e)[:200])
    # continuous futures (CL=F, BZ=F) can jump at contract roll; flag opposite-sign moves
    if out.get("wti_pct") is not None and out.get("brent_pct") is not None and out["wti_pct"] * out["brent_pct"] < 0 \
            and abs(out["wti_pct"] - out["brent_pct"]) > 2:
        out["oil_note"] = "WTI 與 Brent 漲跌方向相反且差距大，可能是期貨換月，請以新聞報價為準"
    if out.get("tsm_adr") and out.get("usdtwd"):
        out["_adr_twd_equiv"] = round(out["tsm_adr"] * out["usdtwd"] / 5, 2)


# ---------------------------------------------------------------- main
def run(step, fn, block):
    try:
        fn(block)
    except Exception as e:  # noqa: BLE001
        log(step, False, repr(e)[:300])


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    hist_dir = ROOT / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_path = hist_dir / f"{TODAY}.json"
    snap = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {"date": TODAY}

    if mode in ("tw", "all"):
        tw = {}
        for name, fn in [("twse_daily", twse_daily), ("twse_index", twse_index), ("twse_flows", twse_flows),
                         ("twse_margin", twse_margin), ("taifex", taifex), ("tpex", tpex_index)]:
            run(name, fn, tw)
        # flag any field whose own date differs from the trading session (e.g. a source not yet updated)
        sd = (tw.get("session_date") or "").replace("-", "")
        stale = []
        for k in ("flows_date", "pc_date", "foreign_fut_date", "margin_px_date", "otc_date"):
            v = str(tw.get(k) or "").replace("-", "").replace("/", "")
            if sd and v and v != sd:
                stale.append(f"{k}={tw.get(k)}")
        tw["stale_fields"] = stale
        tw["fetched_at"] = NOW.isoformat(timespec="minutes")
        snap["tw"] = tw
    if mode in ("us", "all"):
        us = {}
        run("fred", fred, us)
        run("yahoo", yahoo, us)
        us["fetched_at"] = NOW.isoformat(timespec="minutes")
        snap["us"] = us

    hist_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")

    latest_p = ROOT / "latest.json"
    latest = json.loads(latest_p.read_text(encoding="utf-8")) if latest_p.exists() else {}
    for k in ("tw", "us"):
        if k in snap:
            merged = latest.get(k, {})
            merged.update({kk: vv for kk, vv in snap[k].items() if vv is not None})
            for kk in [x for x in merged if x.startswith(("hy_oas", "ig_oas"))]:
                del merged[kk]  # ICE data is not republished (see FRED note above)
            merged["snapshot_date"] = TODAY
            latest[k] = merged
    latest["updated_at"] = NOW.isoformat(timespec="minutes")
    latest_p.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    with open(ROOT / "log.txt", "a", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
