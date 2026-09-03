#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逆勢日誌 — 大盤重挫日自動抓股 + 疊圖 + 更新網站資料（每日排程用）

跑在 GitHub Actions 排程裡，取代原本每次大跌都要手動：
  1. 看盤面決定「今天算不算重挫」
  2. 開 MoneyDJ 網頁抓逆勢股名單
  3. 用 Jupyter notebook 逐檔畫疊圖
  4. 手動把圖片和股票名稱塞進網站的 JS 檔案
的整套流程。

邏輯（延續原本 notebook「爬蟲選股大師創新高與大盤疊圖.ipynb」的做法）：
  - 用台灣證交所官方資料判斷「今天」大盤是否較前一交易日收盤重挫 >= 1.5%。
    沒有重挫就直接結束，不做任何事（no-op）。
  - 有重挫才去爬 MoneyDJ「選股大師」頁面，篩出股價 > 60 的個股代號。
  - 用證交所 ISIN 上市/上櫃清單把代號轉成「代號+中文名稱」。
  - 用 FinMind 抓每檔個股 + 大盤近 1000 天日收盤價，畫成雙軸疊圖（紅：個股，
    水藍：大盤），存進 pictures/<日期>/<序號>.png。
  - 把這次結果寫回 data.json，供 index.html 讀取顯示。

只處理「今天」。要一次補歷史上很多天的資料，用 backfill.py（原理不同：
MoneyDJ 只有當天資料，過去的日子要自己從全市場行情重建 30 日新高名單）。

執行環境需求見 requirements.txt。
"""

from __future__ import annotations

import datetime
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

import common

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

DROP_THRESHOLD_PCT = 1.5          # 大盤重挫門檻（收盤 vs 前一交易日收盤）
PRICE_MIN = 100                   # 逆勢股股價門檻（沿用原本 notebook 的設定）
LOOKBACK_DAYS = 1000               # 疊圖回溯天數（沿用原本 notebook 的設定）

MONEYDJ_URL = "https://concords.moneydj.com/z/zk/zk1/zkparse_580_30.djhtm"


def fetch_moneydj_stock_ids() -> list[str]:
    res = requests.get(MONEYDJ_URL, headers=common.REQUEST_HEADERS, timeout=20)
    # MoneyDJ 頁面是 Big5 編碼，requests 有時猜不準，強制指定避免中文亂碼
    if not res.encoding or res.encoding.lower() in ("iso-8859-1",):
        res.encoding = "big5"

    soup = BeautifulSoup(res.text, "lxml")
    table_div = soup.select_one("#SysJustIFRAMEDIV")
    if table_div is None:
        raise RuntimeError("MoneyDJ 頁面結構跟預期不同，抓不到 #SysJustIFRAMEDIV，"
                            "可能是網站改版了，需要重新檢查 selector。")

    dfs = pd.read_html(str(table_div.prettify()), flavor="lxml")
    price_table = dfs[2]

    stock_ids = []
    for i in range(2, len(price_table[0]) - 1):
        try:
            price = float(price_table[1][i])
        except (ValueError, TypeError):
            continue
        if price > PRICE_MIN:
            stock_ids.append(str(price_table[0][i])[:4])
    return stock_ids


def main() -> int:
    run_date = datetime.datetime.now(TAIPEI_TZ).date()
    iso_date = run_date.isoformat()
    common.log(f"執行日期（台北時間）：{iso_date}")

    entries = common.load_data_json()
    if any(e["date"] == iso_date for e in entries):
        common.log("今天已經處理過了（data.json 裡已經有這個日期），結束。")
        return 0

    trigger = common.get_taiex_trigger(run_date)
    if trigger is None:
        return 0
    common.log(f"大盤收盤 {trigger['close']}，跌點 {trigger['dropPts']}，跌幅 {trigger['dropPct']}%")

    if trigger["dropPct"] < DROP_THRESHOLD_PCT:
        common.log(f"跌幅 {trigger['dropPct']}% 未達 {DROP_THRESHOLD_PCT}% 門檻，今天不算重挫日，結束。")
        return 0

    common.log(f"大盤重挫 {trigger['dropPct']}%，達到門檻，開始抓逆勢股名單……")
    stock_ids = fetch_moneydj_stock_ids()
    common.log(f"MoneyDJ 篩到股價 > {PRICE_MIN} 的個股 {len(stock_ids)} 檔：{stock_ids}")

    if not stock_ids:
        common.log("這次沒有篩到任何逆勢股，仍會把大盤重挫的紀錄寫進 data.json。")

    code_to_name = common.enrich_stock_names(stock_ids) if stock_ids else {}

    start_date = (run_date - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = iso_date

    api = common.get_finmind_loader()
    common.log("向 FinMind 抓大盤近 1000 天收盤價……")
    taiex_close = common.fetch_daily_close(api, "TAIEX", start_date, end_date)
    time.sleep(0.3)

    stocks_out = []
    for i, code in enumerate(stock_ids):
        name = code_to_name.get(code, "")
        label = f"{code}{name}" if name else code
        common.log(f"  [{i+1}/{len(stock_ids)}] 抓 {label} 近 1000 天收盤價並畫圖……")
        try:
            stock_close = common.fetch_daily_close(api, code, start_date, end_date)
            if stock_close.empty:
                common.log(f"    {label} 沒有資料，略過。")
                continue
            img_path = common.PICTURES_DIR / iso_date / f"{i}.png"
            common.plot_overlay(code, code, stock_close, taiex_close, img_path)
            stocks_out.append({
                "code": code,
                "name": name,
                "img": f"pictures/{iso_date}/{i}.png",
            })
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不要讓整批當掉
            common.log(f"    {label} 處理失敗，略過：{exc}")
        time.sleep(0.3)

    new_entry = {
        "date": iso_date,
        "count": len(stocks_out),
        "stocks": stocks_out,
        "close": trigger["close"],
        "prev": trigger["prev"],
        "dropPts": trigger["dropPts"],
        "dropPct": trigger["dropPct"],
    }
    entries.append(new_entry)
    common.save_data_json(entries)
    common.log(f"完成，寫入 data.json：{iso_date}，{len(stocks_out)} 檔逆勢股。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
