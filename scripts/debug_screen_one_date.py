#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
單日測試工具 — 只針對「一天」重新跑一次「創 30 個交易日新高」的篩選，並把
過程完整印出來，方便你自己核對準不準。跟正式的 backfill_screen.py 用同一套
邏輯：上市股票用證交所全市場行情，上櫃股票改用 FinMind 逐檔抓(2026-09-01
更新，原本的 TPEx 全市場行情端點證實不會依日期回傳歷史資料，已經停用)。

用法：
    python scripts/debug_screen_one_date.py 2025-06-19

會印出：
  1. 這天實際抓到幾個交易日的上市全市場行情、併入了幾檔上櫃股票(FinMind
     來源)。
  2. 篩出的每一檔股票：代號、名稱、資料來源(上市/上櫃)、目標日收盤價，
     以及過去 30 個交易日(含當天)的完整收盤價序列——你可以直接拿這串數字
     去核對「目標日收盤價是不是這裡面最高的」。

第一次執行如果本地還沒有 scripts/backfill_otc_cache.json，會先花時間逐檔
用 FinMind 抓全部上櫃股票的歷史股價(可能要跑好一段時間，會被額度限速)；
之後有快取了，重跑這支會很快。這支不會動到 data.json。
"""

from __future__ import annotations

import datetime
import sys

import backfill_screen as bs
import common
import reconstruct_screener as rs

PRICE_MIN = 60
NEW_HIGH_WINDOW = 30


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python scripts/debug_screen_one_date.py YYYY-MM-DD")
        return 1

    target_iso = sys.argv[1]
    target_date = datetime.date.fromisoformat(target_iso)

    # 跟 backfill_screen.py 共用同一顆上櫃快取(FinMind 來源)
    otc_panel = bs.build_otc_panel([{"date": target_iso}])

    calendar_days = rs.calendar_days_needed([target_date], lookback_trading_days=NEW_HIGH_WINDOW)
    common.log(f"需要嘗試抓 {len(calendar_days)} 個平日的上市全市場行情"
               f"（{calendar_days[0]} ~ {calendar_days[-1]}）……")

    panel, trading_days = rs.build_price_panel(calendar_days)
    common.log(f"實際抓到 {len(trading_days)} 個交易日、{len(panel)} 檔上市股票的行情。")

    origin: dict[str, str] = {code: "上市" for code in panel}
    otc_merged = 0
    for code, series in otc_panel.items():
        aligned = {d: c for d, c in series.items() if d in trading_days}
        if aligned:
            panel[code] = aligned
            origin[code] = "上櫃"
            otc_merged += 1
    common.log(f"併入上櫃股票（FinMind 來源）：{otc_merged} 檔。\n")

    if target_iso not in trading_days:
        common.log(f"⚠️ {target_iso} 沒有出現在抓到的交易日清單裡"
                   f"（可能是非交易日，或那天官方資料剛好抓取失敗）。")
        return 1

    idx = trading_days.index(target_iso)
    window = trading_days[max(0, idx - NEW_HIGH_WINDOW + 1): idx + 1]
    common.log(f"{target_iso} 是抓到的第 {idx + 1}/{len(trading_days)} 個交易日，"
               f"用來比較的視窗共 {len(window)} 個交易日"
               f"（{window[0]} ~ {window[-1]}）。")
    if len(window) < NEW_HIGH_WINDOW:
        common.log(f"⚠️ 視窗只有 {len(window)} 天，不到完整的 {NEW_HIGH_WINDOW} 天！"
                   f"可能是往前抓的緩衝天數不夠，這樣算出來的「新高」不可靠。")

    result = rs.find_new_high_stocks(panel, trading_days, target_iso,
                                      lookback=NEW_HIGH_WINDOW, price_min=PRICE_MIN)
    common.log(f"\n篩出 {len(result)} 檔符合「收盤價創 {NEW_HIGH_WINDOW} 日新高、"
               f"股價 > {PRICE_MIN}」的股票：{result}\n")

    code_to_name = common.enrich_stock_names(result) if result else {}

    for code in result:
        series = panel[code]
        today_close = series[target_iso]
        print(f"--- {code} {code_to_name.get(code, '')}（資料來源：{origin.get(code, '?')}） ---")
        print(f"  {target_iso} 收盤：{today_close}")
        print(f"  視窗內 {len(window)} 天收盤價（由舊到新）：")
        for d in window:
            c = series.get(d)
            mark = "   <== 目標日" if d == target_iso else ""
            print(f"    {d}: {c}{mark}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
