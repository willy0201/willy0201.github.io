#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逆勢日誌 — 一次性補歷史資料，第 1 階段：找出每個重挫日「創 30 日新高」的名單

跟畫圖的部分(backfill_plot.py)分開執行，原因：
  - 上市的部分只需要證交所的「全市場收盤行情」，不用 FinMind，跑起來快，
    也不會被限速。
  - 分開之後，就算後面畫圖那步因為 FinMind 額度用完卡住，這裡篩好的名單
    已經存檔，不用每次都重算一次；篩選邏輯本身也比較好單獨檢查結果對不對。

上櫃(TPEx)的部分(2026-09-01 更新)：原本想用櫃買中心的「全市場行情」bulk
API，試了兩個端點都發現不會依照日期回傳歷史資料(不管查哪天都回傳當下即時
報價)，沒辦法用。改成跟畫圖那步一樣，直接用 FinMind **逐檔**抓每一檔上櫃
股票的歷史收盤價(這段程式碼已經在畫圖那步驗證過可以正常運作，不是新猜的)。
因為要抓全部上櫃股票(而不是只有篩出來的那幾檔)，數量會多很多(約 800 檔)，
第一次跑可能要好幾個小時(含被 FinMind 額度限速、自動睡覺重試的時間)，但
只需要跑過一次，結果會存進 `backfill_otc_cache.json`，之後重跑會直接複用，
不用重抓。

用法：
    python scripts/backfill_screen.py

結果會寫進 scripts/backfill_screened.json，格式：
[
  {"date": "2025-06-02", "close": .., "prev": .., "dropPts": .., "dropPct": ..,
   "stocks": [{"code": "2330", "name": "台積電"}, ...]},
  ...python -m http.server 8000
]

已經處理過的日期會自動跳過，可以放心中斷、之後重跑。
"""

from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path

import common
import reconstruct_screener as rs

BACKFILL_DATES_PATH = Path(__file__).resolve().parent / "backfill_dates.json"
SCREENED_PATH = Path(__file__).resolve().parent / "backfill_screened.json"
OTC_CACHE_PATH = Path(__file__).resolve().parent / "backfill_otc_cache.json"

PRICE_MIN = 100
NEW_HIGH_WINDOW = 30
CLUSTER_GAP_DAYS = 45
OTC_FINMIND_SLEEP = 1.5


def load_targets() -> list[dict]:
    with open(BACKFILL_DATES_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_screened() -> dict[str, dict]:
    if not SCREENED_PATH.exists():
        return {}
    with open(SCREENED_PATH, encoding="utf-8") as f:
        return {e["date"]: e for e in json.load(f)}


def save_screened(by_date: dict[str, dict]) -> None:
    ordered = sorted(by_date.values(), key=lambda e: e["date"])
    with open(SCREENED_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=1)
        f.write("\n")


def load_otc_cache() -> dict[str, dict[str, float]]:
    if OTC_CACHE_PATH.exists():
        with open(OTC_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_otc_cache(cache: dict[str, dict[str, float]]) -> None:
    with open(OTC_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def build_otc_panel(pending: list[dict]) -> dict[str, dict[str, float]]:
    """
    用 FinMind 逐檔抓全部上櫃股票的歷史收盤價，涵蓋這次待篩選日期需要的
    整段範圍(一次抓好，之後每個分群直接複用，不用重抓)。有快取檔，可以
    安心中斷、之後重跑會跳過已經抓過的股票。
    """
    dates = [datetime.date.fromisoformat(t["date"]) for t in pending]
    earliest = min(dates) - datetime.timedelta(days=int(NEW_HIGH_WINDOW * 1.6) + 20)
    latest = max(dates)
    start_date = earliest.isoformat()
    end_date = latest.isoformat()

    common.log(f"\n準備補上櫃股票的歷史股價(改用 FinMind，取代壞掉的 TPEx 端點)，"
               f"範圍 {start_date} ~ {end_date}……")

    codes = common.get_otc_stock_codes()
    common.log(f"上櫃股票清單共 {len(codes)} 檔，開始逐檔用 FinMind 抓收盤價"
               f"(只需要跑過一次，結果會存進 {OTC_CACHE_PATH.name}，之後重跑會直接複用)……")

    cache = load_otc_cache()
    already = sum(1 for c in codes if c in cache)
    if already:
        common.log(f"  快取裡已經有 {already}/{len(codes)} 檔，接著補剩下的。")

    api = common.get_finmind_loader()
    for i, code in enumerate(codes, start=1):
        if code in cache:
            continue
        common.log(f"  [{i}/{len(codes)}] {code} ……")
        try:
            series = common.fetch_daily_close(api, code, start_date, end_date)
            cache[code] = {common.date_key(d): float(c) for d, c in series.items()}
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不要讓整批當掉
            common.log(f"    {code} 抓取失敗，略過：{exc}")
            cache[code] = {}
        save_otc_cache(cache)  # 每處理完一檔就存檔一次
        time.sleep(OTC_FINMIND_SLEEP)

    common.log("上櫃股票歷史股價抓取完成。\n")
    return {code: series for code, series in cache.items() if series}


def cluster_dates(iso_dates: list[str]) -> list[list[datetime.date]]:
    dates = sorted(datetime.date.fromisoformat(d) for d in iso_dates)
    clusters: list[list[datetime.date]] = []
    current: list[datetime.date] = []
    for d in dates:
        if current and (d - current[-1]).days > CLUSTER_GAP_DAYS:
            clusters.append(current)
            current = []
        current.append(d)
    if current:
        clusters.append(current)
    return clusters


def main() -> int:
    targets = load_targets()
    by_date = load_screened()

    pending = [t for t in targets if t["date"] not in by_date]
    if not pending:
        common.log(f"{SCREENED_PATH.name} 已經涵蓋所有 {len(targets)} 天，不用重新篩選。")
        return 0

    common.log(f"待篩選日期共 {len(pending)} 天（總共 {len(targets)} 天，"
               f"{len(targets) - len(pending)} 天已完成）。")

    otc_panel = build_otc_panel(pending)

    for cluster in cluster_dates([t["date"] for t in pending]):
        common.log(f"處理群組：{cluster[0]} ~ {cluster[-1]}（{len(cluster)} 天）")
        calendar_days = rs.calendar_days_needed(cluster, lookback_trading_days=NEW_HIGH_WINDOW)
        common.log(f"  需要抓 {len(calendar_days)} 個平日的上市全市場行情（用來重建 30 日新高名單）……")
        panel, trading_days = rs.build_price_panel(calendar_days)
        common.log(f"  實際抓到 {len(trading_days)} 個交易日、{len(panel)} 檔上市股票的行情。")

        # 把 FinMind 抓到的上櫃股價併進來，只留跟上市同一套交易日曆對得上的日期
        otc_merged = 0
        for code, series in otc_panel.items():
            aligned = {d: c for d, c in series.items() if d in trading_days}
            if aligned:
                panel[code] = aligned
                otc_merged += 1
        common.log(f"  併入上櫃股票（FinMind 來源）：{otc_merged} 檔。")

        for d in cluster:
            iso_date = d.isoformat()
            target = next(t for t in pending if t["date"] == iso_date)

            stock_codes = rs.find_new_high_stocks(
                panel, trading_days, iso_date,
                lookback=NEW_HIGH_WINDOW, price_min=PRICE_MIN,
            )
            common.log(f"  {iso_date} → {len(stock_codes)} 檔候選：{stock_codes}")
            code_to_name = common.enrich_stock_names(stock_codes) if stock_codes else {}

            by_date[iso_date] = {
                "date": iso_date,
                "close": target["close"],
                "prev": target["prev"],
                "dropPts": target["dropPts"],
                "dropPct": target["dropPct"],
                "stocks": [{"code": c, "name": code_to_name.get(c, "")} for c in stock_codes],
            }
            save_screened(by_date)  # 每篩完一天就存檔一次
            common.log(f"  已寫入 {SCREENED_PATH.name}。\n")

    common.log("篩選階段全部完成，接下來執行 python scripts/backfill_plot.py 畫圖。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
