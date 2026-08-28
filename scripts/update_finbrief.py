#!/usr/bin/env python3
"""FINBRIEF 免費雲端晨晚報更新器。

只使用 Python 標準函式庫與免費公開來源，不使用 AI API、付費資料或本機狀態。
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Taipei")
REPORT_PATH = Path("latest.json")
REQUIRED_KEYS = [
    "taiex", "nikkei", "eurostoxx50", "dax", "ftse100", "sp500",
    "nasdaq", "us10y", "dollar", "usdtwd", "vix", "brent", "gold", "bitcoin",
]
USER_AGENT = "FINBRIEF-Free-Cloud-Updater/1.0 (+https://github.com/ibjennie/finbrief-data)"

FRED_SERIES = {
    "nikkei": ("NIKKEI225", "FRED", "https://fred.stlouisfed.org/series/NIKKEI225", 2, ""),
    "sp500": ("SP500", "FRED", "https://fred.stlouisfed.org/series/SP500", 2, ""),
    "nasdaq": ("NASDAQCOM", "FRED", "https://fred.stlouisfed.org/series/NASDAQCOM", 2, ""),
    "dollar": ("DTWEXBGS", "FRED", "https://fred.stlouisfed.org/series/DTWEXBGS", 2, ""),
    "brent": ("DCOILBRENTEU", "FRED／EIA", "https://fred.stlouisfed.org/series/DCOILBRENTEU", 2, "$"),
}

TRADINGVIEW_SYMBOLS = {
    "eurostoxx50": ("STOXX:SX5E", "EURO STOXX 50", "https://www.tradingview.com/symbols/STOXX-SX5E/", 2, ""),
    "dax": ("XETR:DAX", "DAX", "https://www.tradingview.com/symbols/XETR-DAX/", 2, ""),
    "ftse100": ("FTSE:UKX", "FTSE 100", "https://www.tradingview.com/symbols/FTSE-UKX/", 2, ""),
    "usdtwd": ("FX_IDC:USDTWD", "美元／臺幣", "https://www.tradingview.com/symbols/USDTWD/", 3, ""),
    "gold": ("FX_IDC:XAUUSD", "黃金現貨", "https://www.tradingview.com/symbols/XAUUSD/", 2, "$"),
}


def request_text(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> str:
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read().decode("utf-8-sig")
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"來源讀取失敗：{url}（{last_error}）")


def request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    return json.loads(request_text(url, method=method, payload=payload))


def number(value: Any) -> float:
    if value is None:
        raise ValueError("空值")
    return float(str(value).replace(",", "").replace("%", "").replace("$", "").strip())


def signed_pct(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}%"
    if value < 0:
        return f"−{abs(value):.2f}%"
    return "0.00%"


def signed_bp(value: float) -> str:
    if value > 0:
        return f"+{value:.1f} bp"
    if value < 0:
        return f"−{abs(value):.1f} bp"
    return "0.0 bp"


def tone(change: float) -> str:
    return "up" if change > 0 else "down" if change < 0 else "flat"


def fmt_value(value: float, decimals: int, prefix: str = "") -> str:
    return f"{prefix}{value:,.{decimals}f}"


def as_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def previous_business_day(day: date) -> date:
    result = day - timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def source_date_for_live_market(now: datetime, edition: str) -> str:
    if edition == "evening" and now.weekday() < 5:
        return now.isoformat(timespec="seconds")
    return previous_business_day(now.date()).isoformat()


def update_item(
    item: dict[str, Any], *, value: float, change: float, as_of: str,
    source: str, source_url: str, decimals: int = 2, prefix: str = "",
    change_kind: str = "percent",
) -> None:
    item["value"] = fmt_value(value, decimals, prefix)
    item["change"] = signed_bp(change) if change_kind == "bp" else signed_pct(change)
    item["tone"] = tone(change)
    item["asOf"] = as_of
    item["source"] = source
    item["sourceUrl"] = source_url


def fred_observations(series: str) -> list[tuple[str, float]]:
    text = request_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
    rows = list(csv.reader(io.StringIO(text)))
    result: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) < 2 or not row[1].strip() or row[1].strip() == ".":
            continue
        try:
            result.append((row[0].strip(), number(row[1])))
        except ValueError:
            continue
    if len(result) < 2:
        raise RuntimeError(f"FRED {series} 可用觀測值不足")
    return result


def refresh_fred(items: dict[str, dict[str, Any]]) -> list[str]:
    updated: list[str] = []
    for key, (series, source, url, decimals, prefix) in FRED_SERIES.items():
        observations = fred_observations(series)
        as_of, latest = observations[-1]
        previous = observations[-2][1]
        change = (latest / previous - 1) * 100
        update_item(items[key], value=latest, change=change, as_of=as_of, source=source,
                    source_url=url, decimals=decimals, prefix=prefix)
        updated.append(key)
    return updated


def refresh_twse(item: dict[str, Any], now: datetime) -> None:
    for offset in range(0, 12):
        target = now.date() - timedelta(days=offset)
        query_date = target.strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?date={query_date}&type=ALLBUT0999&response=json"
        )
        try:
            payload = request_json(url)
        except Exception:
            continue
        for table in payload.get("tables", []):
            fields = table.get("fields", [])
            if "收盤指數" not in fields or "漲跌百分比(%)" not in fields:
                continue
            for row in table.get("data", []):
                if row and row[0] == "發行量加權股價指數":
                    value = number(row[fields.index("收盤指數")])
                    change = number(row[fields.index("漲跌百分比(%)")])
                    update_item(item, value=value, change=change, as_of=target.isoformat(),
                                source="臺灣證交所", source_url=url, decimals=2)
                    return
    raise RuntimeError("臺灣證交所近 12 日無可用收盤資料")


def refresh_tradingview(items: dict[str, dict[str, Any]], now: datetime, edition: str) -> list[str]:
    tickers = [config[0] for config in TRADINGVIEW_SYMBOLS.values()]
    payload = {
        "symbols": {"tickers": tickers, "query": {"types": []}},
        "columns": ["name", "close", "change", "change_abs", "currency", "update_mode", "description"],
    }
    records: dict[str, list[Any]] = {}
    for attempt in range(3):
        result = request_json("https://scanner.tradingview.com/global/scan", method="POST", payload=payload)
        for row in result.get("data", []):
            values = row.get("d", [])
            if len(values) > 2 and values[1] is not None and values[2] is not None:
                records[row["s"]] = values
        if all(ticker in records for ticker in tickers):
            break
        if attempt < 2:
            time.sleep(2)
    updated: list[str] = []
    market_as_of = source_date_for_live_market(now, edition)
    for key, (ticker, _label, url, decimals, prefix) in TRADINGVIEW_SYMBOLS.items():
        values = records.get(ticker)
        if not values or values[1] is None or values[2] is None:
            continue
        update_item(items[key], value=number(values[1]), change=number(values[2]),
                    as_of=market_as_of, source="TradingView", source_url=url,
                    decimals=decimals, prefix=prefix)
        updated.append(key)
    return updated


def refresh_treasury(item: dict[str, Any]) -> None:
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        "daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve&"
        "field_tdr_date_value=2026&page&_format=csv"
    )
    rows = list(csv.DictReader(io.StringIO(request_text(url))))
    values: list[tuple[date, float]] = []
    for row in rows:
        try:
            values.append((datetime.strptime(row["Date"], "%m/%d/%Y").date(), number(row["10 Yr"])))
        except (KeyError, ValueError, TypeError):
            continue
    values.sort(key=lambda pair: pair[0])
    if len(values) < 2:
        raise RuntimeError("美國財政部 10 年期資料不足")
    latest_day, latest = values[-1]
    bp = (latest - values[-2][1]) * 100
    update_item(item, value=latest, change=bp, as_of=latest_day.isoformat(), source="美國財政部",
                source_url=url, decimals=2, prefix="", change_kind="bp")
    item["value"] = f"{latest:.2f}%"


def refresh_vix(item: dict[str, Any]) -> None:
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    rows = list(csv.DictReader(io.StringIO(request_text(url))))
    values: list[tuple[date, float]] = []
    for row in rows:
        try:
            values.append((datetime.strptime(row["DATE"], "%m/%d/%Y").date(), number(row["CLOSE"])))
        except (KeyError, ValueError, TypeError):
            continue
    values.sort(key=lambda pair: pair[0])
    if len(values) < 2:
        raise RuntimeError("Cboe VIX 資料不足")
    latest_day, latest = values[-1]
    change = (latest / values[-2][1] - 1) * 100
    update_item(item, value=latest, change=change, as_of=latest_day.isoformat(), source="Cboe",
                source_url="https://www.cboe.com/tradable-products/vix/", decimals=2)


def refresh_bitcoin(item: dict[str, Any], now: datetime) -> None:
    url = "https://api.exchange.coinbase.com/products/BTC-USD/stats"
    payload = request_json(url)
    latest = number(payload["last"])
    opened = number(payload["open"])
    change = (latest / opened - 1) * 100
    update_item(item, value=latest, change=change, as_of=now.isoformat(timespec="seconds"),
                source="Coinbase", source_url="https://www.coinbase.com/price/bitcoin",
                decimals=2, prefix="$")


def refresh_markets(data: dict[str, Any], now: datetime, edition: str) -> tuple[list[str], list[str]]:
    items = {item["key"]: item for item in data["marketSnapshot"]["items"]}
    updated: list[str] = []
    failures: list[str] = []

    def attempt(label: str, keys: list[str], action: Callable[[], Any]) -> None:
        try:
            result = action()
            updated.extend(result if isinstance(result, list) else keys)
        except Exception as exc:
            failures.append(f"{label}：{exc}")

    attempt("FRED", list(FRED_SERIES), lambda: refresh_fred(items))
    attempt("臺灣證交所", ["taiex"], lambda: refresh_twse(items["taiex"], now))
    attempt("TradingView", list(TRADINGVIEW_SYMBOLS), lambda: refresh_tradingview(items, now, edition))
    attempt("美國財政部", ["us10y"], lambda: refresh_treasury(items["us10y"]))
    attempt("Cboe", ["vix"], lambda: refresh_vix(items["vix"]))
    attempt("Coinbase", ["bitcoin"], lambda: refresh_bitcoin(items["bitcoin"], now))
    carried = [key for key in REQUIRED_KEYS if key not in set(updated)]
    return carried, failures


def pct(item: dict[str, Any]) -> str:
    return item["change"].replace("+", "").replace("−", "")


def direction(item: dict[str, Any]) -> str:
    return "上漲" if item["tone"] == "up" else "下跌" if item["tone"] == "down" else "持平"


def impact_tone(item: dict[str, Any]) -> str:
    return "positive" if item["tone"] == "up" else "negative" if item["tone"] == "down" else "neutral"


def link(item: dict[str, Any]) -> dict[str, str]:
    return {"label": f"{item['source']}｜{item['label']}", "url": item["sourceUrl"]}


def build_edition(data: dict[str, Any], edition: str, now: datetime, carried: list[str]) -> dict[str, Any]:
    items = {item["key"]: item for item in data["marketSnapshot"]["items"]}
    t, n = items["taiex"], items["nikkei"]
    eu, dax, ftse = items["eurostoxx50"], items["dax"], items["ftse100"]
    sp, nas, y10, vix = items["sp500"], items["nasdaq"], items["us10y"], items["vix"]
    dollar, twd, brent, gold, btc = items["dollar"], items["usdtwd"], items["brent"], items["gold"], items["bitcoin"]
    label = "晨報" if edition == "morning" else "晚報"
    time_label = "08:30" if edition == "morning" else "18:30"
    kicker = "Morning intelligence" if edition == "morning" else "Evening intelligence"
    carry_copy = "；部分免費來源沿用最近真實日期" if carried else "；14 項來源均取得可驗證資料"

    if edition == "morning":
        headline = [
            f"隔夜 S&P 500 {direction(sp)}、NASDAQ {direction(nas)}，",
            f"VIX 來到 {vix['value']}、美國 10 年期為 {y10['value']}，",
            "亞洲開盤先看美元、油價與科技股承接力",
        ]
        summary = (
            f"事實：最近完成的美國交易日，S&P 500 {direction(sp)} {pct(sp)}、NASDAQ {direction(nas)} {pct(nas)}，"
            f"VIX {direction(vix)} {pct(vix)}，美國 10 年期殖利率為 {y10['value']}。美元廣義指數為 {dollar['value']}，"
            f"布蘭特原油為 {brent['value']}。推論：晨間重點是隔夜美股風險如何傳到臺日股市，而不是重複解讀尚未開盤的亞洲價格{carry_copy}。"
        )
        lead_title = (
            f"隔夜美股{direction(sp)}、VIX {vix['value']}："
            "亞洲開盤先看利率與美元"
        )
        lead_tags = ["隔夜市場", "亞洲開盤"]
        risk_copy = "晨間觀察 · 隔夜美股與利率訊號等待亞洲確認"
        lead_copy = (
            f"事實：最近完成交易日的 S&P 500 收在 {sp['value']}、單日{direction(sp)} {pct(sp)}；NASDAQ 收在 {nas['value']}、"
            f"單日{direction(nas)} {pct(nas)}，VIX 為 {vix['value']}、變動 {vix['change']}。美國 10 年期殖利率為 {y10['value']}，"
            f"美元廣義指數為 {dollar['value']}；布蘭特原油 {brent['value']}、黃金 {gold['value']}。亞洲前一可得交易日，"
            f"臺灣加權為 {t['value']}、日經 225 為 {n['value']}，美元／臺幣為 {twd['value']}。晨報保留各來源真實日期，"
            "尚未開盤或尚未發布的數字不會被改標成今日；重點是建立今天開盤前的風險基準。"
        )
        why = (
            "推論：隔夜美股提供全球風險偏好的第一個方向，長端殖利率決定高估值公司的折現壓力，美元與油價則會透過外資換匯、"
            "進口成本及企業毛利傳到亞洲。如果 NASDAQ 上漲但殖利率、美元與 VIX 同步走高，臺日科技股的追價力道仍可能有限；"
            "若三項壓力同時下降，風險偏好才較有機會延續。開盤後應具體觀察臺股半導體權值、日圓與臺幣方向，以及前一小時成交量"
            "是否高於近期平均，判斷隔夜訊號是否真的被亞洲市場接受。"
        )
    else:
        headline = [
            f"臺股{direction(t)}、日股{direction(n)}，亞洲收盤已有答案，",
            f"歐洲三大指數變動 {eu['change']}／{dax['change']}／{ftse['change']}，",
            "晚間留意美股能否接住跨區域風險偏好",
        ]
        summary = (
            f"事實：臺灣加權收在 {t['value']}、{direction(t)} {pct(t)}；日經 225 為 {n['value']}、{direction(n)} {pct(n)}。"
            f"歐洲時段 EURO STOXX 50、DAX、FTSE 100 分別變動 {eu['change']}、{dax['change']}、{ftse['change']}。"
            f"推論：晚報重點是亞洲收盤結果能否獲得歐洲與稍後美股確認，而不是沿用晨間的隔夜敘事{carry_copy}。"
        )
        lead_title = (
            f"臺股{direction(t)}、日股{direction(n)}："
            "歐美接力檢驗區域風險偏好"
        )
        lead_tags = ["亞洲收盤", "歐美接力"]
        risk_copy = "晚間觀察 · 亞洲結果等待歐美時段確認"
        lead_copy = (
            f"事實：臺灣加權於 {t['asOf']} 收在 {t['value']}、單日{direction(t)} {pct(t)}；日經 225 於 {n['asOf']} 收在 {n['value']}、"
            f"單日{direction(n)} {pct(n)}。歐洲時段的 EURO STOXX 50、DAX 與 FTSE 100 分別為 {eu['value']}、{dax['value']}、"
            f"{ftse['value']}，變動 {eu['change']}、{dax['change']}、{ftse['change']}。美國最近完成交易日的 S&P 500 為 {sp['value']}、"
            f"NASDAQ 為 {nas['value']}，10 年期殖利率 {y10['value']}，VIX {vix['value']}。美元／臺幣 {twd['value']}、"
            f"布蘭特原油 {brent['value']}、黃金 {gold['value']}；所有數字均保留來源實際時間。"
        )
        why = (
            "推論：亞洲收盤提供當日資金選擇的實際結果，歐洲盤中表現可用來判斷風險偏好是否跨區域延續；美股開盤後的科技股、"
            "VIX 與長端殖利率則是最後確認。如果臺日股市走強、歐股也同步上漲，但美元與殖利率快速升高，全球行情仍可能只是短線輪動；"
            "若歐股與美股同步承接且 VIX 下降，趨勢的可信度才會提高。今晚應觀察 S&P 500 與 NASDAQ 開盤一小時的方向、"
            "美國 10 年期是否突破近期區間，以及黃金與原油是否反映新的通膨或避險需求。"
        )

    important = [
        {
            "number": "02", "category": "亞洲", "region": "臺灣／日本",
            "title": f"臺灣加權 {t['value']}、日經 225 {n['value']}，亞洲市場方向需分開判讀",
            "summary": f"事實：臺灣加權於 {t['asOf']} {direction(t)} {pct(t)}，日經 225 於 {n['asOf']} {direction(n)} {pct(n)}。推論：兩地產業權重與匯率敏感度不同，同方向不必然代表相同驅動，應搭配半導體、日圓與外資流向觀察。",
            "impact": "臺股、日股、亞洲貨幣", "links": [link(t), link(n)],
        },
        {
            "number": "03", "category": "歐洲", "region": "歐元區／英國",
            "title": f"歐洲三大指數最新變動：{eu['change']}、{dax['change']}、{ftse['change']}",
            "summary": f"事實：EURO STOXX 50 為 {eu['value']}，DAX 為 {dax['value']}，FTSE 100 為 {ftse['value']}；各自資料時間保留為來源的真實時間。推論：同步變動代表區域風險偏好，走勢分歧則更可能反映產業結構、英鎊與歐元差異。",
            "impact": "歐股、歐元、英鎊", "links": [link(eu), link(dax), link(ftse)],
        },
        {
            "number": "04", "category": "美股", "region": "美國",
            "title": f"S&P 500 {sp['change']}、NASDAQ {nas['change']}，VIX 收在 {vix['value']}",
            "summary": f"事實：最近完成交易日，S&P 500 {direction(sp)} {pct(sp)}、NASDAQ {direction(nas)} {pct(nas)}，VIX {direction(vix)} {pct(vix)}。推論：科技股、整體市場與波動指標若同時轉弱，風險訊號比單看一個指數更完整。",
            "impact": "美股、科技股、波動率", "links": [link(sp), link(nas), link(vix)],
        },
        {
            "number": "05", "category": "利率與商品", "region": "全球",
            "title": f"美國 10 年期 {y10['value']}、布蘭特 {brent['value']}、黃金 {gold['value']}",
            "summary": f"事實：美國 10 年期殖利率最新變動 {y10['change']}，布蘭特原油變動 {brent['change']}，黃金變動 {gold['change']}。推論：利率、能源與避險資產共同決定企業資金成本、通膨預期與風險承擔，三者同向走高時尤其需要留意。",
            "impact": "美債、能源、黃金", "links": [link(y10), link(brent), link(gold)],
        },
        {
            "number": "06", "category": "流動性", "region": "全球",
            "title": f"美元廣義指數 {dollar['value']}，比特幣 24 小時變動 {btc['change']}",
            "summary": f"事實：美元廣義指數最新為 {dollar['value']}、變動 {dollar['change']}；美元／臺幣為 {twd['value']}；比特幣為 {btc['value']}、24 小時變動 {btc['change']}。推論：美元與高波動資產的相對方向可協助判斷全球流動性是否正在收緊或轉鬆。",
            "impact": "美元、臺幣、加密資產", "links": [link(dollar), link(twd), link(btc)],
        },
    ]

    if edition == "morning":
        important = [important[2], important[3], important[4], important[0], important[1]]
        important[0]["title"] = f"隔夜美股先定調：S&P 500 {sp['change']}、NASDAQ {nas['change']}"
        important[0]["summary"] = (
            f"事實：最近完成交易日 S&P 500 {direction(sp)} {pct(sp)}、NASDAQ {direction(nas)} {pct(nas)}，"
            f"VIX 收在 {vix['value']}。推論：晨間先用美股廣度、科技股與波動率建立亞洲開盤基準；三者若方向不一致，"
            "臺日股市即使高開也需要等待成交量確認。"
        )
        important[1]["title"] = f"開盤前資金成本：美國 10 年期 {y10['value']}、油價 {brent['value']}"
        important[1]["summary"] = (
            f"事實：美國 10 年期單日變動 {y10['change']}，布蘭特原油變動 {brent['change']}，黃金變動 {gold['change']}。"
            "推論：殖利率影響估值，油價影響成本，黃金反映部分避險需求；三者是亞洲企業與投資人開盤前需要同時衡量的條件。"
        )
        important[2]["title"] = f"美元流動性晨間檢查：廣義美元 {dollar['value']}、美元／臺幣 {twd['value']}"
        important[2]["summary"] = (
            f"事實：美元廣義指數變動 {dollar['change']}，美元／臺幣變動 {twd['change']}，比特幣 24 小時變動 {btc['change']}。"
            "推論：美元若走強且高波動資產轉弱，亞洲資金承接力通常較受限制；若美元轉弱，外資換匯壓力可能減輕。"
        )
        important[3]["title"] = f"亞洲開盤基準：臺股前值 {t['value']}、日經前值 {n['value']}"
        important[4]["title"] = f"歐洲前一時段留下的訊號：{eu['change']}／{dax['change']}／{ftse['change']}"
    else:
        important[0]["title"] = f"亞洲收盤結果：臺灣加權 {t['change']}、日經 225 {n['change']}"
        important[0]["summary"] = (
            f"事實：臺灣加權於 {t['asOf']} 收在 {t['value']}，日經 225 於 {n['asOf']} 收在 {n['value']}。"
            "推論：晚報以已完成的亞洲交易結果為起點，應再比對半導體、匯率與外資方向，判斷走勢是區域共振或單一市場輪動。"
        )
        important[1]["title"] = f"歐洲接力狀況：EURO STOXX 50 {eu['change']}、DAX {dax['change']}"
        important[2]["title"] = f"美股今晚的比較基準：S&P 500 {sp['change']}、NASDAQ {nas['change']}"
        important[3]["title"] = f"晚間風險價格：10 年期 {y10['value']}、油價 {brent['value']}、黃金 {gold['value']}"
        important[4]["title"] = f"跨市場流動性：美元 {dollar['value']}、比特幣 {btc['change']}"

    for index, item in enumerate(important, start=2):
        item["number"] = f"{index:02d}"

    if edition == "morning":
        radar = [
            radar_card("長端資金", "8.9", y10, f"美國 10 年期殖利率為 {y10['value']}，單日變動 {y10['change']}", "長端殖利率會透過企業融資、房貸與股票折現率傳導到多個市場；即使股指短線上漲，只要實質資金成本沒有下降，高估值產業仍可能面臨估值壓力。接下來應比較 2 年期與 10 年期利差、美元方向及成長股相對表現，確認壓力是政策預期還是期限溢酬造成。"),
            radar_card("匯率傳導", "8.6", twd, f"美元／臺幣最新為 {twd['value']}，變動 {twd['change']}", "匯率會改變進口能源與原料成本，也會影響外資換匯與出口企業的帳面收益；小幅變動若與美元廣義指數方向一致，可能逐步累積成資金流訊號。應持續比較美元廣義指數、外資買賣超與央行公開資料，而不是只看單日價格。"),
            radar_card("能源成本", "8.4", brent, f"布蘭特原油最新為 {brent['value']}，變動 {brent['change']}", "原油不只影響能源公司，也會經由航運、航空、化工與物流成本傳到企業毛利與消費者物價；價格上升若伴隨美元走強，進口國承受的本幣成本可能更高。後續應觀察庫存、裂解價差與運價，判斷波動是供給問題還是需求重新定價。"),
        ]
    else:
        radar = [
            radar_card("區域分歧", "8.8", eu, f"EURO STOXX 50 為 {eu['value']}，與日經及美股方向未必一致", "歐洲、亞洲與美國股市的產業權重、交易時段與匯率環境不同；當指數方向分歧時，市場可能正在交易區域性成長、能源成本或政策差異，而不是單一全球風險偏好。應比較銀行、科技與出口類股，以及歐元和日圓是否同步確認。"),
            radar_card("波動流動性", "8.7", btc, f"比特幣為 {btc['value']}，24 小時變動 {btc['change']}", "比特幣全天交易且對槓桿與美元流動性敏感，劇烈變動有時會早於傳統市場顯示風險偏好轉折；但它也受加密市場自身清算影響，不能單獨作為全球訊號。應同時比對 VIX、美元與美股期現貨方向，避免把單一市場的擠壓誤判為全面趨勢。"),
            radar_card("亞洲輪動", "8.5", n, f"日經 225 為 {n['value']}，變動 {n['change']}；臺灣加權為 {t['value']}", "臺灣與日本同屬亞洲科技供應鏈，但半導體、金融、出口產業權重及貨幣條件不同；兩地走勢分歧可能反映資金在產業與匯率曝險間輪動。後續應觀察日圓、臺幣、半導體類股與外資成交比重，確認輪動是否具有連續性。"),
        ]

    return {
        "label": label, "time": time_label, "publishedAt": now.isoformat(timespec="seconds"),
        "kicker": kicker, "readTime": "閱讀時間 8 分鐘", "headline": headline,
        "summary": summary, "risk": 64, "riskCopy": risk_copy,
        "leadTags": lead_tags, "leadTitle": lead_title,
        "leadCopy": lead_copy, "why": why,
        "links": [link(t), link(n), link(sp), link(y10)],
        "impacts": [
            {"symbol": "臺股", "label": direction(t), "tone": impact_tone(t)},
            {"symbol": "NASDAQ", "label": direction(nas), "tone": impact_tone(nas)},
            {"symbol": "美國 10Y", "label": y10["change"], "tone": impact_tone(y10)},
        ],
        "importantNews": important, "underRadar": radar,
    }


def radar_card(tag: str, score: str, item: dict[str, Any], title: str, why: str) -> dict[str, Any]:
    return {
        "tag": tag, "score": score, "eventDate": item["asOf"][:10], "title": title,
        "why": f"事實：資料來源日期為 {item['asOf']}，來源保留在卡片連結。推論：{why}",
        "links": [link(item)],
    }


KNOWLEDGE = [
    ("太空科學", "韋伯望遠鏡為什麼能觀測早期宇宙？", "韋伯太空望遠鏡主要觀測紅外線，可捕捉因宇宙膨脹而被拉長波長的古老星光，也較能穿透塵埃觀察恆星形成區。", "紅外線讓天文學家同時研究遙遠星系與被塵埃遮蔽的近鄰天體。", "NASA｜韋伯太空望遠鏡", "https://science.nasa.gov/mission/webb/"),
    ("文學歷史", "死海古卷為何改變古代文本研究？", "死海古卷包含兩千多年前的宗教與社群文獻，讓研究者能比較不同時期的抄本、語言與文本流傳方式。", "古代文本不是固定不變的單一路線；抄寫、保存與社群使用都會留下歷史痕跡。", "以色列博物館｜死海古卷", "https://www.imj.org.il/en/wings/shrine-book/dead-sea-scrolls"),
    ("自然科學", "珊瑚白化不等於珊瑚立即死亡", "海水過熱會使珊瑚排出共生藻而失去顏色；若壓力及時解除，部分珊瑚仍可能恢復，但長期或反覆熱浪會提高死亡風險。", "白化是嚴重壓力訊號，持續時間與重複頻率決定生態系能否修復。", "NOAA｜珊瑚白化", "https://oceanservice.noaa.gov/facts/coral_bleach.html"),
    ("語言文化", "世界記憶計畫保存的不只是書", "聯合國教科文組織的世界記憶計畫涵蓋手稿、錄音、影像、檔案與其他紀錄，目標是降低重要文獻遺產因災害、衝突或老化而消失的風險。", "文化記憶的保存同時依賴實體修復、數位化與可持續的公共存取。", "UNESCO｜世界記憶計畫", "https://www.unesco.org/en/memory-world"),
    ("地球科學", "冰芯如何保存過去的大氣？", "冰層形成時會封存微小氣泡，研究者可分析其中的氣體與同位素，重建過去的溫度、火山活動與大氣組成。", "冰芯像是按年代堆疊的環境檔案，但解讀仍需配合定年與其他地質證據。", "NOAA｜古氣候代理資料", "https://www.ncei.noaa.gov/products/paleoclimatology"),
    ("藝術保存", "博物館為什麼嚴格控制光線？", "光會促使顏料、紙張與纖維發生不可逆的化學變化，因此博物館會依材質調整照度與展示時間。", "降低光照不是讓展覽變暗而已，而是在可觀看與長期保存之間取得平衡。", "加拿大保存研究所｜光線與文物", "https://www.canada.ca/en/conservation-institute/services/agents-deterioration/light.html"),
    ("海洋科學", "深海熱泉不依靠陽光也能形成生態系", "深海熱泉周圍的微生物利用化學能合成有機物，成為管蟲、貝類與其他生物的能量基礎。", "生命取得能量的方式不限於光合作用，這也影響科學家尋找外星生命的思考。", "NOAA｜深海熱泉", "https://oceanexplorer.noaa.gov/facts/vents.html"),
    ("考古方法", "樹輪可以精確到年份嗎？", "樹木每年的生長輪寬度會受到氣候影響；研究者把不同年代木材的紋理交叉比對，可建立跨越數百至數千年的年表。", "樹輪定年兼具年代與環境資訊，但必須使用同地區、同類型樹木建立可靠序列。", "美國國家公園管理局｜樹輪年代學", "https://www.nps.gov/articles/000/dendrochronology.htm"),
]


def daily_knowledge(day: date) -> dict[str, Any]:
    category, title, summary, takeaway, label, url = KNOWLEDGE[day.toordinal() % len(KNOWLEDGE)]
    return {
        "number": f"NO. {day.timetuple().tm_yday:03d}", "date": day.strftime("%Y.%m.%d"),
        "category": category, "readTime": "60 秒讀懂", "title": title,
        "summary": summary, "takeaway": takeaway, "links": [{"label": label, "url": url}],
    }


def future_events(today: date) -> list[dict[str, str]]:
    candidates = [
        (date(2026, 8, 26), "20:30", "美國", "第 2 季 GDP 第二次估計與個人所得及支出", "高"),
        (date(2026, 9, 16), "02:00", "美國", "聯準會利率決議（依官方會議日程）", "高"),
        (date(2026, 9, 17), "", "歐元區", "8 月 HICP 最終值（依官方發布行程）", "中"),
        (date(2026, 10, 28), "02:00", "美國", "聯準會利率決議（依官方會議日程）", "高"),
        (date(2026, 12, 9), "03:00", "美國", "聯準會利率決議（依官方會議日程）", "高"),
    ]
    upcoming = [row for row in candidates if row[0] >= today][:4]
    if len(upcoming) < 4:
        upcoming.append((today + timedelta(days=7), "", "全球", "下一週主要官方資料發布行程", "中"))
    return [
        {"time": f"{day:%m.%d}" + (f" {clock}" if clock else ""), "region": region, "event": event, "level": level}
        for day, clock, region, event, level in upcoming[:4]
    ]


def archive_entry(data: dict[str, Any], archived_at: str) -> dict[str, Any]:
    return {
        "date": data["report"]["date"], "archivedAt": archived_at,
        "report": copy.deepcopy(data["report"]), "marketSnapshot": copy.deepcopy(data["marketSnapshot"]),
    }


def upsert_history(data: dict[str, Any], entry: dict[str, Any]) -> None:
    entries = [entry] + [row for row in data["reportHistory"]["entries"] if row.get("date") != entry["date"]]
    entries.sort(key=lambda row: row["date"], reverse=True)
    data["reportHistory"]["entries"] = entries[:45]


def validate(data: dict[str, Any], edition: str, today: date, new_day: bool = False) -> list[str]:
    errors: list[str] = []
    required_top = {"schemaVersion", "generatedAt", "report", "marketSnapshot", "reportHistory"}
    if not required_top.issubset(data): errors.append("缺少必要頂層欄位")
    report = data.get("report", {})
    if report.get("date") != today.isoformat(): errors.append("report.date 不是臺北當日")
    if str(report.get("dailyKnowledge", {}).get("date", "")).replace(".", "-") != today.isoformat(): errors.append("dailyKnowledge.date 不一致")
    editions = report.get("editions", {})
    for name in ("morning", "evening"):
        block = editions.get(name, {})
        if len(block.get("headline", [])) != 3: errors.append(f"{name} 標題不是三段")
        if block.get("publishedAt"):
            if len(block.get("leadCopy", "")) < 180: errors.append(f"{name} leadCopy 少於 180 字")
            if len(block.get("why", "")) < 140: errors.append(f"{name} why 少於 140 字")
            if len(block.get("importantNews", [])) != 5: errors.append(f"{name} 重要新聞不是五則")
            radar = block.get("underRadar", [])
            if len(radar) != 3: errors.append(f"{name} 冷門雷達不是三則")
            if len({row.get("tag") for row in radar}) != 3: errors.append(f"{name} 冷門雷達面向重複")
            for row in radar:
                try:
                    age = (today - as_date(row["eventDate"])).days
                    if age < 0 or age > 31: errors.append(f"{name} 雷達日期超過 31 日")
                except Exception: errors.append(f"{name} 雷達日期格式錯誤")
    morning = editions.get("morning", {})
    evening = editions.get("evening", {})
    if morning.get("publishedAt") and evening.get("publishedAt"):
        if morning.get("headline") == evening.get("headline"):
            errors.append("晨報與晚報標題不可完全相同")
        if morning.get("leadTitle") == evening.get("leadTitle"):
            errors.append("晨報與晚報首要焦點不可完全相同")
    if edition == "morning" and new_day and editions.get("evening", {}).get("publishedAt") is not None:
        errors.append("新一天晚報 publishedAt 必須為 null")
    items = data.get("marketSnapshot", {}).get("items", [])
    keys = [row.get("key") for row in items]
    if len(items) != 14 or sorted(keys) != sorted(REQUIRED_KEYS): errors.append("14 項行情不完整")
    for row in items:
        for field in ("key", "label", "value", "change", "tone", "asOf", "source", "sourceUrl"):
            if not row.get(field): errors.append(f"行情 {row.get('key')} 缺少 {field}")
        if not str(row.get("sourceUrl", "")).startswith("https://"): errors.append(f"行情 {row.get('key')} 來源不是 HTTPS")
    all_links: list[str] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"url", "sourceUrl"} and isinstance(child, str): all_links.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(data)
    if any(not url.startswith("https://") for url in all_links): errors.append("存在非 HTTPS 來源")
    dates = [row.get("date") for row in data.get("reportHistory", {}).get("entries", [])]
    if dates != sorted(dates, reverse=True): errors.append("歷史日期未由新到舊")
    if len(dates) != len(set(dates)): errors.append("歷史日期重複")
    raw = json.dumps(data, ensure_ascii=False)
    if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", raw, re.I): errors.append("內容含電子郵件")
    if re.search(r"api[_-]?key|access[_-]?token|secret[_-]?key|bearer\s+[A-Za-z0-9._-]+", raw, re.I): errors.append("內容含密鑰或權杖")
    simplified = set("资讯场际发现历经业价险来链网软据连数个动华国").intersection(raw)
    if simplified: errors.append("內容疑似含簡體字：" + "".join(sorted(simplified)))
    return errors


def run(path: Path, edition: str, dry_run: bool, force: bool = False) -> dict[str, Any]:
    now = datetime.now(TZ)
    today = now.date()
    if not force and edition == "morning" and (now.hour, now.minute) < (8, 30):
        raise RuntimeError("尚未到臺北時間 08:30，晨報停止發布")
    if not force and edition == "evening" and (now.hour, now.minute) < (18, 30):
        raise RuntimeError("尚未到臺北時間 18:30，晚報停止發布")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not {"schemaVersion", "generatedAt", "report", "marketSnapshot", "reportHistory"}.issubset(data):
        raise RuntimeError("現有 latest.json 結構不完整，停止更新")

    existing = data.get("report", {}).get("editions", {}).get(edition, {})
    if (
        not force
        and data.get("report", {}).get("date") == today.isoformat()
        and str(existing.get("publishedAt") or "").startswith(today.isoformat())
    ):
        result = {
            "edition": edition,
            "publishedAt": existing["publishedAt"],
            "skipped": True,
            "reason": "今日版本已成功發布，略過重複排程",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    new_day = edition == "morning" and data["report"]["date"] != today.isoformat()
    if new_day:
        upsert_history(data, archive_entry(data, now.isoformat(timespec="seconds")))
        data["report"]["date"] = today.isoformat()
        data["report"]["dailyKnowledge"] = daily_knowledge(today)
        data["report"]["events"] = future_events(today)
        data["report"]["editions"]["evening"]["publishedAt"] = None
    elif edition == "evening" and data["report"]["date"] != today.isoformat():
        raise RuntimeError("晨報尚未建立臺北當日資料，晚報停止發布")
    elif edition == "morning":
        data["report"]["dailyKnowledge"] = daily_knowledge(today)
        data["report"]["events"] = future_events(today)

    carried, failures = refresh_markets(data, now, edition)
    if len(carried) > 8:
        raise RuntimeError(
            f"可驗證的新行情不足（14 項中有 {len(carried)} 項沿用），保留原檔不發布"
        )
    block = build_edition(data, edition, now, carried)
    data["report"]["editions"][edition] = block
    data["report"]["updatedAt"] = now.isoformat(timespec="seconds")
    data["report"]["contentStatus"] = "已查證" if not failures else "部分來源沿用"
    data["generatedAt"] = now.isoformat(timespec="seconds")
    data["marketSnapshot"]["generatedAt"] = now.isoformat(timespec="seconds")
    data["marketSnapshot"]["edition"] = f"{block['label']}免費雲端資料快照"
    if edition == "evening":
        upsert_history(data, archive_entry(data, now.isoformat(timespec="seconds")))

    errors = validate(data, edition, today, new_day)
    if errors:
        raise RuntimeError("發布前驗證失敗：\n- " + "\n- ".join(errors))

    result = {
        "edition": edition, "publishedAt": block["publishedAt"], "carried": carried,
        "failures": failures, "marketDates": {row["key"]: row["asOf"] for row in data["marketSnapshot"]["items"]},
        "leadCopyLength": len(block["leadCopy"]), "whyLength": len(block["why"]),
        "radarDates": [row["eventDate"] for row in block["underRadar"]],
    }
    if not dry_run:
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", choices=("morning", "evening"), required=True)
    parser.add_argument("--path", type=Path, default=REPORT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        run(args.path, args.edition, args.dry_run, args.force)
        return 0
    except Exception as exc:
        print(f"FINBRIEF 更新失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
