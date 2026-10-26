#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逆勢日誌 — 維護「近一年股價」走勢圖用的收盤價資料（每日排程用）

網站上的「近一年股價」圖（收盤價 + 20 日均線 + 60 日均線）是由 index.html
在瀏覽器裡用 prices/<代號>.json 即時畫出來的，不是預先畫好的 PNG。原因：
這張圖每個交易日都會變，如果每天重畫幾百張 PNG 再 commit，repo 會快速膨脹；
存純文字收盤價，每天的變動只有幾行，git 很好壓縮。

資料範圍：data.json 裡出現過的所有個股，每檔保留最近 HISTORY_DAYS 天的
收盤價（一年顯示範圍 + 60 日均線需要的暖機期間）。

抓價策略（盡量不用 FinMind，避免每天幾百次請求被限速）：
  1. 用證交所大盤月資料取得交易日曆，算出每檔股票缺哪幾個交易日。
  2. 只缺最近幾天的(日常情況)：
       - 上市：證交所「全市場收盤行情」，一天一次請求就涵蓋全部上市股票。
       - 上櫃：櫃買中心 OpenAPI，只會回傳最新一個交易日，日期對得上才採用。
  3. 還補不齊的（新加入的股票、排程漏跑好幾天、上櫃股票缺舊資料）：
     改用 FinMind 逐檔抓。

用法：
    python scripts/trend_prices.py              # 日常更新
    python scripts/trend_prices.py --bootstrap  # 第一次建立：上市股票整段用證交所
                                                # 全市場行情補，只有上櫃才逐檔用 FinMind

已經抓好的資料會保留，可以放心中斷、之後重跑。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from zoneinfo import ZoneInfo

import requests

import common
import reconstruct_screener as rs

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

PRICES_DIR = common.REPO_ROOT / "prices"
HISTORY_DAYS = 460          # 365 天顯示範圍 + 約 60 個交易日的均線暖機期
BULK_MAX_GAP = 5            # 缺的交易日 <= 這個數字才用全市場行情補，否則改逐檔抓
TPEX_LATEST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def load_series(code: str) -> dict:
    path = PRICES_DIR / f"{code}.json"
    if not path.exists():
        return {"code": code, "checked": "", "prices": {}}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {"code": code, "checked": raw.get("checked", ""),
            "prices": dict(zip(raw["dates"], raw["close"]))}


def save_series(series: dict, start: str) -> None:
    dates = sorted(d for d in series["prices"] if d >= start)
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    with open(PRICES_DIR / f"{series['code']}.json", "w", encoding="utf-8") as f:
        json.dump({"code": series["code"], "checked": series["checked"], "dates": dates,
                   "close": [series["prices"][d] for d in dates]},
                  f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def trading_calendar(start: datetime.date, end: datetime.date) -> list[str]:
    """證交所大盤月資料裡出現的日期 = 交易日。"""
    days = []
    month = start.replace(day=1)
    while month <= end:
        days += [d for d, _ in common.fetch_taiex_month(month.strftime("%Y%m"))]
        month = (month + datetime.timedelta(days=32)).replace(day=1)
        time.sleep(rs.REQUEST_DELAY)
    return sorted(d for d in days if start.isoformat() <= d <= end.isoformat())


def fetch_tpex_latest() -> tuple[str, dict[str, float]]:
    """上櫃：最新一個交易日的全市場收盤價，回傳 (iso_date, {code: close})。"""
    try:
        resp = requests.get(TPEX_LATEST_URL, headers=common.REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        common.log(f"  櫃買中心最新行情抓取失敗，上櫃改用 FinMind：{exc}")
        return "", {}

    out, iso_date = {}, ""
    for row in rows:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        close = rs._to_float(str(row.get("Close", "")))
        if common.STOCK_CODE_RE.match(code) and close is not None:
            out[code] = close
            iso_date = iso_date or common.roc_to_iso(
                f"{row['Date'][:-4]}/{row['Date'][-4:-2]}/{row['Date'][-2:]}")
    return iso_date, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true",
                        help="上市股票不管缺多少天都用證交所全市場行情補（第一次建立用）")
    args = parser.parse_args()

    today = datetime.datetime.now(TAIPEI_TZ).date()
    start = today - datetime.timedelta(days=HISTORY_DAYS)
    calendar = trading_calendar(start, today)
    if not calendar:
        common.log("抓不到交易日曆，結束。")
        return 1
    latest = calendar[-1]

    codes = sorted({s["code"] for e in common.load_data_json() for s in e["stocks"]})
    all_series = {code: load_series(code) for code in codes}

    def missing(series: dict) -> list[str]:
        have = max([*series["prices"], series["checked"]], default="")
        return [d for d in calendar if d > have]

    gaps = {code: missing(s) for code, s in all_series.items()}
    pending = {code: g for code, g in gaps.items() if g}
    common.log(f"追蹤 {len(codes)} 檔，最新交易日 {latest}，需要更新 {len(pending)} 檔。")
    if not pending:
        return 0

    # 全市場行情：上市一天一次請求；上櫃只有最新一天
    bulk_limit = len(calendar) if args.bootstrap else BULK_MAX_GAP
    bulk_days = sorted({d for g in pending.values() if len(g) <= bulk_limit for d in g})
    listed: dict[str, str] = {}  # 上市股票 -> 全市場行情裡第一次出現的日期
    for i, d in enumerate(bulk_days, start=1):
        closes = rs.fetch_twse_daily_all(datetime.date.fromisoformat(d))
        common.log(f"  [{i}/{len(bulk_days)}] 證交所 {d} 全市場行情：{len(closes)} 檔")
        for code in pending:
            if code in closes:
                all_series[code]["prices"][d] = closes[code]
                listed.setdefault(code, d)
        time.sleep(rs.REQUEST_DELAY)

    tpex_date, tpex_closes = fetch_tpex_latest()
    if tpex_date:
        common.log(f"  櫃買中心最新行情 {tpex_date}：{len(tpex_closes)} 檔")
        for code in pending:
            if code in tpex_closes:
                all_series[code]["prices"][tpex_date] = tpex_closes[code]

    # 全市場行情補完後仍有缺口：股票不在上市清單裡（上櫃）、或缺太多天
    api = None
    for code, gap in pending.items():
        series = all_series[code]
        still = [d for d in gap if d not in series["prices"]]
        # 上市股票若已經用全市場行情補過，剩下沒資料的日子就是停牌，不用再查；
        # 但缺的是上市之前的日子(剛從上櫃轉上市)，要用 FinMind 補上櫃時期的股價
        if still and (len(gap) > bulk_limit or code not in listed or still[0] < listed[code]):
            api = api or common.get_finmind_loader()
            common.log(f"  {code} 缺 {len(still)} 個交易日，改用 FinMind 抓……")
            try:
                close = common.fetch_daily_close(api, code, still[0], latest)
                series["prices"].update({common.date_key(d): float(c) for d, c in close.items()})
            except Exception as exc:  # noqa: BLE001 — 單檔失敗不要讓整批當掉，下次排程會再補
                common.log(f"    {code} 抓取失敗，略過：{exc}")
                save_series(series, start.isoformat())
                continue
            time.sleep(0.3)
        # 上市停牌的股票標記已檢查過，避免每天重查；上櫃若最新一天還沒資料，
        # 不標記，讓下一次排程(19:00 補跑或隔天)再試
        if latest in series["prices"] or code in listed:
            series["checked"] = latest
        save_series(series, start.isoformat())

    common.log(f"完成，prices/ 已更新到 {latest}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
