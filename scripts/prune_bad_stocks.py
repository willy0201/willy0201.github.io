#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正用：清掉「上櫃資料抓錯」污染到 data.json 的股票

背景：reconstruct_screener.py 裡上櫃(TPEx)的抓取邏輯有 bug(抓到的其實是
「今天」的即時報價，不是歷史資料，導致幾乎每一檔上櫃股票都被誤判成創 30 日
新高)，已經在 reconstruct_screener.py 修好(整個關閉上櫃抓取，只保留上市)。
但如果你在修好之前已經用 backfill_screen.py / backfill_plot.py 跑過幾天，
data.json 裡可能已經混進了這些錯誤的上櫃股票，這支就是用來把它們清掉的。

用法(順序很重要)：
    1. 先刪掉舊的 scripts/backfill_screened.json(裡面的名單是用有 bug 的版本
       篩出來的，不能再用)。
    2. 重新執行一次 python scripts/backfill_screen.py，用修好的版本重新篩選
       (這次只會篩出上市股票)。
    3. 執行這支：python scripts/prune_bad_stocks.py
       會比對 data.json 跟新的 backfill_screened.json，把「新名單裡沒有」的
       股票從 data.json 移除，同時砍掉對應的圖片檔，並把剩下的圖片重新
       編號(避免之後 backfill_plot.py 接著跑的時候檔名撞在一起)。
    4. 確認 data.json 內容沒問題之後，再繼續執行
       python scripts/backfill_plot.py 補上市股票原本沒抓完的部分
       (少掉的上櫃股票這次不會補，等 TPEx 抓取邏輯真的修好、驗證過之後再說)。

這支只會動到 scripts/backfill_dates.json 列出的這 27 個補歷史資料的日期，
不會動到其他(2023–2025 手動處理過的)57 筆舊資料。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import common

BACKFILL_DATES_PATH = Path(__file__).resolve().parent / "backfill_dates.json"
SCREENED_PATH = Path(__file__).resolve().parent / "backfill_screened.json"


def main() -> int:
    with open(BACKFILL_DATES_PATH, encoding="utf-8") as f:
        backfill_dates = {t["date"] for t in json.load(f)}

    if not SCREENED_PATH.exists():
        print(f"找不到 {SCREENED_PATH.name}，請先照說明重新跑一次 backfill_screen.py。")
        return 1
    with open(SCREENED_PATH, encoding="utf-8") as f:
        screened_by_date = {e["date"]: e for e in json.load(f)}

    entries = common.load_data_json()

    total_removed = 0
    for entry in entries:
        iso_date = entry["date"]
        if iso_date not in backfill_dates:
            continue  # 不是這次補歷史資料的範圍，不要動
        if iso_date not in screened_by_date:
            common.log(f"{iso_date}：還沒有乾淨的篩選結果(backfill_screened.json 裡沒有)，跳過，"
                       f"不會動這天的資料。")
            continue

        good_codes = {s["code"] for s in screened_by_date[iso_date]["stocks"]}
        old_stocks = entry["stocks"]
        kept = [s for s in old_stocks if s["code"] in good_codes]
        removed = [s for s in old_stocks if s["code"] not in good_codes]

        if not removed:
            continue

        removed_labels = [f"{s['code']}{s['name']}" for s in removed]
        common.log(f"{iso_date}：移除 {len(removed)} 檔錯誤股票：{removed_labels}")
        total_removed += len(removed)

        # 砍掉被移除股票的圖片檔
        for s in removed:
            img_path = common.REPO_ROOT / s["img"]
            if img_path.exists():
                img_path.unlink()

        # 剩下的圖片重新編號成 0..N-1，避免跟之後 backfill_plot.py 產生的新檔名撞在一起
        pic_dir = common.PICTURES_DIR / iso_date
        new_stocks = []
        for new_idx, s in enumerate(kept):
            old_path = common.REPO_ROOT / s["img"]
            new_path = pic_dir / f"{new_idx}.tmp.png"
            if old_path.exists() and old_path != new_path:
                old_path.rename(new_path)
            new_stocks.append({**s})  # 之後統一處理檔名

        # 第二輪：把 .tmp.png 換成正式檔名，順便寫回 img 欄位(避免跟舊檔名互相覆蓋)
        for new_idx, s in enumerate(new_stocks):
            tmp_path = pic_dir / f"{new_idx}.tmp.png"
            final_path = pic_dir / f"{new_idx}.png"
            if tmp_path.exists():
                tmp_path.rename(final_path)
            s["img"] = f"pictures/{iso_date}/{new_idx}.png"

        entry["stocks"] = new_stocks
        entry["count"] = len(new_stocks)

    if total_removed == 0:
        common.log("沒有找到需要清掉的錯誤股票，data.json 目前是乾淨的。")
        return 0

    common.save_data_json(entries)
    common.log(f"\n共移除 {total_removed} 檔錯誤股票，已更新 data.json 跟對應的圖片檔案。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
