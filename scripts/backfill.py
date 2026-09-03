#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逆勢日誌 — 一次性補歷史重挫日資料

用法：
    python scripts/backfill.py

會讀 scripts/backfill_dates.json 裡列出的每一個歷史重挫日(日期 + 已經
用證交所官方資料核對過的大盤收盤/跌點/跌幅)，針對每一天：

  1. 用 reconstruct_screener.py 重建「當天收盤價創近 30 個交易日新高、且
     股價 > 60」的股票名單(取代 MoneyDJ，因為它只有當天資料，查不到過去)。
  2. 用證交所 ISIN 清單把代號轉成中文名稱。
  3. 用 FinMind 抓每檔個股 + 大盤近 1000 天日收盤價，畫成疊圖。
  4. 把結果寫進 data.json。

跟每日排程的 crash_day_pipeline.py 不同，這支只需要手動跑一次(或分批跑
幾次)，不用排程。已經處理過的日期會自動跳過，可以放心中斷、之後重跑。

這支會發出相當多網路請求(全市場行情 + 每檔個股的 FinMind 查詢)，抓完
27 天全部資料粗估要 1–2 小時，請耐心等待，不要中途一直重複啟動。
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

PRICE_MIN = 60
LOOKBACK_DAYS = 1000
NEW_HIGH_WINDOW = 30

FINMIND_SLEEP = 1.5          # 補資料量大，放慢一點避免匿名連線被限速
CLUSTER_GAP_DAYS = 45        # 目標日期之間超過這麼多天就分開抓行情，省請求


def load_targets() -> list[dict]:
    with open(BACKFILL_DATES_PATH, encoding="utf-8") as f:
        return json.load(f)


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


def process_one_date(target: dict, stock_codes: list[str], api) -> dict:
    iso_date = target["date"]
    run_date = datetime.date.fromisoformat(iso_date)

    common.log(f"  重建名單：{iso_date} → {len(stock_codes)} 檔候選：{stock_codes}")
    code_to_name = common.enrich_stock_names(stock_codes) if stock_codes else {}

    start_date = (run_date - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = iso_date

    common.log(f"  向 FinMind 抓大盤到 {iso_date} 為止近 1000 天收盤價……")
    taiex_close = common.fetch_daily_close(api, "TAIEX", start_date, end_date)
    time.sleep(FINMIND_SLEEP)

    stocks_out = []
    for i, code in enumerate(stock_codes):
        name = code_to_name.get(code, "")
        label = f"{code}{name}" if name else code
        common.log(f"    [{i+1}/{len(stock_codes)}] {label} ……")
        try:
            stock_close = common.fetch_daily_close(api, code, start_date, end_date)
            if stock_close.empty:
                common.log(f"      {label} 沒有資料，略過。")
                continue
            img_path = common.PICTURES_DIR / iso_date / f"{i}.png"
            common.plot_overlay(code, code, stock_close, taiex_close, img_path)
            stocks_out.append({
                "code": code,
                "name": name,
                "img": f"pictures/{iso_date}/{i}.png",
            })
        except Exception as exc:  # noqa: BLE001
            common.log(f"      {label} 處理失敗，略過：{exc}")
        time.sleep(FINMIND_SLEEP)

    return {
        "date": iso_date,
        "count": len(stocks_out),
        "stocks": stocks_out,
        "close": target["close"],
        "prev": target["prev"],
        "dropPts": target["dropPts"],
        "dropPct": target["dropPct"],
    }


def main() -> int:
    targets = load_targets()
    entries = common.load_data_json()
    done_dates = {e["date"] for e in entries}

    pending = [t for t in targets if t["date"] not in done_dates]
    if not pending:
        common.log("backfill_dates.json 裡的日期都已經在 data.json 了，沒有需要補的。")
        return 0

    common.log(f"待補日期共 {len(pending)} 天（總共 {len(targets)} 天，{len(targets) - len(pending)} 天已完成）。")

    api = common.get_finmind_loader()

    for cluster in cluster_dates([t["date"] for t in pending]):
        common.log(f"處理群組：{cluster[0]} ~ {cluster[-1]}（{len(cluster)} 天）")
        calendar_days = rs.calendar_days_needed(cluster, lookback_trading_days=NEW_HIGH_WINDOW)
        common.log(f"  需要抓 {len(calendar_days)} 個平日的全市場行情（用來重建 30 日新高名單）……")
        panel, trading_days = rs.build_price_panel(calendar_days)
        common.log(f"  實際抓到 {len(trading_days)} 個交易日、{len(panel)} 檔股票的行情。")

        for d in cluster:
            iso_date = d.isoformat()
            target = next(t for t in pending if t["date"] == iso_date)

            stock_codes = rs.find_new_high_stocks(
                panel, trading_days, iso_date,
                lookback=NEW_HIGH_WINDOW, price_min=PRICE_MIN,
            )

            entry = process_one_date(target, stock_codes, api)
            entries.append(entry)
            common.save_data_json(entries)  # 每處理完一天就存檔，方便中斷重跑
            common.log(f"  完成 {iso_date}：{entry['count']} 檔逆勢股，已寫入 data.json。\n")

    common.log("全部處理完畢。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
