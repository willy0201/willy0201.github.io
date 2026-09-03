#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逆勢日誌 — 一次性補歷史資料，第 2 階段：抓股價、畫疊圖，寫進 data.json

要先執行過 scripts/backfill_screen.py，產生 scripts/backfill_screened.json，
才能跑這支。這支只做「FinMind 抓價 + 畫圖」，是整個補資料流程裡唯一會被
FinMind 匿名額度限速的部分，所以獨立成一支，方便中斷、恢復，不用每次卡住
都要連篩名單那步一起重跑。

跟舊版 backfill.py 最大的不同：**每處理完「一檔股票」就會存檔一次**(舊版是
每處理完一整天才存檔)，所以就算中途被限速、當掉，這一天裡已經畫完的圖不會
遺失，重新執行會接著這一天沒畫完的部分繼續，不用整天重來。

用法：
    python scripts/backfill_screen.py   # 先跑這個(見該檔說明)
    python scripts/backfill_plot.py     # 再跑這個
"""

from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path

import common

SCREENED_PATH = Path(__file__).resolve().parent / "backfill_screened.json"

LOOKBACK_DAYS = 1000       # 疊圖回溯天數(沿用原本 notebook 的設定)
FINMIND_SLEEP = 1.5        # 每次 FinMind 請求之間的間隔，放慢一點避免匿名連線被限速


def load_screened() -> list[dict]:
    if not SCREENED_PATH.exists():
        raise SystemExit(
            f"找不到 {SCREENED_PATH.name}，請先執行：python scripts/backfill_screen.py"
        )
    with open(SCREENED_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_entry(entries: list[dict], iso_date: str) -> dict | None:
    return next((e for e in entries if e["date"] == iso_date), None)


def main() -> int:
    screened = load_screened()
    entries = common.load_data_json()

    api = common.get_finmind_loader()
    taiex_cache: dict[str, object] = {}

    total_targets = len(screened)
    for n, target in enumerate(screened, start=1):
        iso_date = target["date"]
        run_date = datetime.date.fromisoformat(iso_date)
        wanted_codes = [s["code"] for s in target["stocks"]]
        name_lookup = {s["code"]: s["name"] for s in target["stocks"]}

        entry = find_entry(entries, iso_date)
        if entry is None:
            entry = {
                "date": iso_date,
                "count": 0,
                "stocks": [],
                "close": target["close"],
                "prev": target["prev"],
                "dropPts": target["dropPts"],
                "dropPct": target["dropPct"],
            }
            entries.append(entry)

        done_codes = {s["code"] for s in entry["stocks"]}
        pending_codes = [c for c in wanted_codes if c not in done_codes]

        if not wanted_codes:
            common.log(f"[{n}/{total_targets}] {iso_date}：這天沒有篩到任何逆勢股，略過。")
            continue

        if not pending_codes:
            common.log(f"[{n}/{total_targets}] {iso_date} 已經全部畫完"
                       f"（{len(entry['stocks'])}/{len(wanted_codes)} 檔），跳過。")
            continue

        common.log(f"[{n}/{total_targets}] {iso_date}：還有 {len(pending_codes)}/"
                   f"{len(wanted_codes)} 檔要畫。")

        start_date = (run_date - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
        end_date = iso_date

        if iso_date not in taiex_cache:
            common.log(f"  向 FinMind 抓大盤到 {iso_date} 為止近 1000 天收盤價……")
            taiex_cache[iso_date] = common.fetch_daily_close(api, "TAIEX", start_date, end_date)
            time.sleep(FINMIND_SLEEP)
        taiex_close = taiex_cache[iso_date]

        for code in pending_codes:
            name = name_lookup.get(code, "")
            label = f"{code}{name}" if name else code
            idx = len(entry["stocks"])  # 用目前已完成的數量當圖檔序號，保持穩定命名
            common.log(f"    [{idx + 1}/{len(wanted_codes)}] {label} ……")
            try:
                stock_close = common.fetch_daily_close(api, code, start_date, end_date)
                if stock_close.empty:
                    common.log(f"      {label} 沒有資料，略過。")
                    continue
                img_path = common.PICTURES_DIR / iso_date / f"{idx}.png"
                common.plot_overlay(code, code, stock_close, taiex_close, img_path)
                entry["stocks"].append({
                    "code": code,
                    "name": name,
                    "img": f"pictures/{iso_date}/{idx}.png",
                })
                entry["count"] = len(entry["stocks"])
                common.save_data_json(entries)  # 每畫完一檔股票就存檔一次
            except Exception as exc:  # noqa: BLE001 — 單檔失敗不要讓整批當掉
                common.log(f"      {label} 處理失敗，略過：{exc}")
            time.sleep(FINMIND_SLEEP)

        common.log(f"  {iso_date} 這輪完成，目前共 {entry['count']}/{len(wanted_codes)} 檔逆勢股。\n")

    common.log("畫圖階段全部處理完畢。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
