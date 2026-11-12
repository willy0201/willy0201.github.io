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
  - 用台灣證交所官方資料判斷「今天」大盤是否較前一交易日收盤重挫 >= 2%（2026-10 起由 1.5% 調高）。
    沒有重挫就直接結束，不做任何事（no-op）。
  - 有重挫才去爬 MoneyDJ「選股大師」頁面，篩出股價 > 100 的個股代號。
  - 用證交所 ISIN 上市/上櫃清單把代號轉成「代號+中文名稱」。
  - 用 FinMind 抓每檔個股 + 大盤近 1000 天日收盤價，畫成雙軸疊圖（紅：個股，
    水藍：大盤），存進 pictures/<日期>/<序號>.png。
  - 把這次結果寫回 data.json，供 index.html 讀取顯示。

GitHub 排程常延遲好幾小時，所以每次會往回檢查最近 CATCH_UP_TRADING_DAYS 個
交易日：MoneyDJ 頁面的日期跟重挫日對得上就用 MoneyDJ 名單，對不上(排程延遲到
隔天、漏跑)就從全市場收盤行情重建同一套「30 日新高」名單(標記 source=reconstructed)。
要一次補更久以前的歷史資料，用 backfill.py。

執行環境需求見 requirements.txt。
"""

from __future__ import annotations

import datetime
import re
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

import common
import reconstruct_screener as rs

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

DROP_THRESHOLD_PCT = 2.0          # 大盤重挫門檻（收盤 vs 前一交易日收盤）
PRICE_MIN = 100                   # 逆勢股股價門檻（2026-09 起改成 100，跟補歷史資料的門檻一致）
LOOKBACK_DAYS = 1000               # 疊圖回溯天數（沿用原本 notebook 的設定）
NEW_HIGH_WINDOW = 30               # 重建名單時的「創 N 日新高」視窗，跟 MoneyDJ 頁面一致
CATCH_UP_TRADING_DAYS = 15         # 每次往回檢查幾個交易日，補上排程延遲或漏跑的重挫日

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


def moneydj_list_date(today: datetime.date) -> datetime.date | None:
    """MoneyDJ 頁面上「日期:MM/DD」顯示的是哪一天的名單(頁面沒有年份，取不晚於今天的那一年)。"""
    res = requests.get(MONEYDJ_URL, headers=common.REQUEST_HEADERS, timeout=20)
    res.encoding = "big5"
    m = re.search(r"日期[:：]\s*(\d{1,2})/(\d{1,2})", res.text)
    if not m:
        return None
    d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
    return d if d <= today else d.replace(year=today.year - 1)


def recent_trading_days(today: datetime.date) -> list[tuple[str, float]]:
    """最近兩個月的 [(iso_date, 大盤收盤), ...]，由舊到新。"""
    prev_month = (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y%m")
    return common.fetch_taiex_month(prev_month) + common.fetch_taiex_month(today.strftime("%Y%m"))


def screen_stock_ids(target: datetime.date, moneydj_date: datetime.date | None,
                     cache: dict) -> tuple[list[str], str]:
    """
    回傳 (股票代號, 名單來源)。MoneyDJ 只有最新一個交易日的名單，日期對得上才用；
    排程延遲或漏跑造成日期對不上時，改用全市場收盤行情重建同一套條件的名單。
    """
    if moneydj_date == target:
        return fetch_moneydj_stock_ids(), "moneydj"
    if "panel" not in cache:
        common.log("  MoneyDJ 名單不是這一天的，改用證交所/櫃買中心收盤行情重建 30 日新高名單……")
        # 一次抓好涵蓋所有待處理日期的行情，多天補抓時不用重抓
        cache["panel"] = rs.build_price_panel(rs.calendar_days_needed(cache["targets"], NEW_HIGH_WINDOW))
    panel, trading_days = cache["panel"]
    ids = rs.find_new_high_stocks(panel, trading_days, target.isoformat(),
                                  lookback=NEW_HIGH_WINDOW, price_min=PRICE_MIN)
    return ids, "reconstructed"


def process_day(api, entries: list[dict], target: datetime.date, trigger: dict,
                moneydj_date: datetime.date | None, cache: dict) -> None:
    iso_date = target.isoformat()
    existing = next((e for e in entries if e["date"] == iso_date), None)
    common.log(f"{iso_date}：大盤收盤 {trigger['close']}，跌點 {trigger['dropPts']}，"
               f"跌幅 {trigger['dropPct']}%，開始抓逆勢股名單……")

    stock_ids, source = screen_stock_ids(target, moneydj_date, cache)
    common.log(f"  篩到股價 > {PRICE_MIN} 的個股 {len(stock_ids)} 檔（來源：{source}）：{stock_ids}")
    if not stock_ids:
        common.log("  這次沒有篩到任何逆勢股，仍會把大盤重挫的紀錄寫進 data.json。")

    code_to_name = common.enrich_stock_names(stock_ids) if stock_ids else {}

    start_date = (target - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = iso_date

    common.log("  向 FinMind 抓大盤近 1000 天收盤價……")
    taiex_close = common.fetch_daily_close(api, "TAIEX", start_date, end_date)
    time.sleep(0.3)

    stocks_out = list(existing.get("stocks", [])) if existing else []
    completed_codes = {stock["code"] for stock in stocks_out}
    failed_codes = []
    for i, code in enumerate(stock_ids):
        if code in completed_codes:
            continue
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
            failed_codes.append(code)
        time.sleep(0.3)

    new_entry = {
        "date": iso_date,
        "count": len(stocks_out),
        "stocks": stocks_out,
        **trigger,
        "status": "partial" if failed_codes else "complete",
        "failedStocks": failed_codes,
    }
    if source == "reconstructed":
        new_entry["source"] = source
    entries[:] = [e for e in entries if e["date"] != iso_date]
    entries.append(new_entry)
    common.save_data_json(entries)
    common.log(f"  完成，寫入 data.json：{iso_date}，{len(stocks_out)} 檔逆勢股；"
               f"{len(failed_codes)} 檔待重試。")


def main() -> int:
    today = datetime.datetime.now(TAIPEI_TZ).date()
    common.log(f"執行日期（台北時間）：{today.isoformat()}")

    # GitHub 排程常延遲好幾個小時(甚至跑到隔天凌晨)，所以不能只看「今天」，
    # 而是檢查最近幾個交易日裡還沒處理完的重挫日
    rows = recent_trading_days(today)
    entries = common.load_data_json()
    done = {e["date"] for e in entries if e.get("status", "complete") == "complete"}

    targets = []
    for (_, prev), (iso_date, close) in list(zip(rows, rows[1:]))[-CATCH_UP_TRADING_DAYS:]:
        drop_pts = round(prev - close, 2)
        trigger = {"close": close, "prev": prev, "dropPts": drop_pts,
                   "dropPct": round(drop_pts / prev * 100, 2)}
        if trigger["dropPct"] >= DROP_THRESHOLD_PCT and iso_date not in done:
            targets.append((datetime.date.fromisoformat(iso_date), trigger))

    if not targets:
        common.log(f"最近 {CATCH_UP_TRADING_DAYS} 個交易日（到 {rows[-1][0] if rows else '—'}）"
                   f"沒有待處理、跌幅 >= {DROP_THRESHOLD_PCT}% 的重挫日，結束。")
        return 0

    common.log(f"待處理重挫日：{[t.isoformat() for t, _ in targets]}")
    moneydj_date = moneydj_list_date(today)
    common.log(f"MoneyDJ 目前顯示的是 {moneydj_date} 的名單。")

    api = common.get_finmind_loader()
    cache: dict = {"targets": [t for t, _ in targets]}
    for target, trigger in targets:
        process_day(api, entries, target, trigger, moneydj_date, cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
