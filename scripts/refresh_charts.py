#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 data.json 裡現有的股票名單，重新畫一次所有疊圖——並且把抓到的股價存成
快取(scripts/backfill_price_cache.json)。

背景：原本 backfill_plot.py 抓到股價之後直接畫成圖就結束了，沒有另外存
原始數字下來，所以想調整 plot_overlay() 的樣式(顏色、字型、日期標籤密度
等等)就得重新抓一次股價。這支補上快取機制：

  - 第一次執行：因為之前沒存過，全部都要重新用 FinMind 抓一次(照樣會被
    額度限速，可能要跑好一陣子)，但每抓到一檔就會存進快取檔一次。
  - 之後只要改了 common.py 的 plot_overlay()，重跑這支就會直接用快取
    重畫，幾乎不用等待、也不會再用到 FinMind 額度。

建議先把股價門檻、篩選名單都定案(跑完 backfill_screen.py +
prune_bad_stocks.py)之後，再執行這支，才不會浪費額度去抓之後會被踢掉的
股票。

用法：
    python scripts/refresh_charts.py

中途可以 `Ctrl+C` 中斷，重跑會利用快取跳過已經抓過的部分繼續。
"""

from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path

import pandas as pd

import common

PRICE_CACHE_PATH = Path(__file__).resolve().parent / "backfill_price_cache.json"
LOOKBACK_DAYS = 1000
FINMIND_SLEEP = 1.5


def load_cache() -> dict[str, dict[str, float]]:
    if PRICE_CACHE_PATH.exists():
        with open(PRICE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict[str, dict[str, float]]) -> None:
    with open(PRICE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def get_series(cache: dict, key: str, fetch_fn) -> pd.Series:
    """先查快取，沒有才呼叫 fetch_fn() 抓，抓到之後立刻存檔。"""
    cached = cache.get(key)
    if cached is not None:
        return pd.Series(cached, dtype=float).sort_index()

    series = fetch_fn()
    cache[key] = {common.date_key(d): float(c) for d, c in series.items()}
    save_cache(cache)
    time.sleep(FINMIND_SLEEP)
    return pd.Series(cache[key], dtype=float).sort_index()


def main() -> int:
    entries = common.load_data_json()
    cache = load_cache()
    api = common.get_finmind_loader()

    total_stocks = sum(len(e.get("stocks", [])) for e in entries)
    done = 0
    redrawn = 0
    failed = 0

    for entry in entries:
        iso_date = entry["date"]
        stocks = entry.get("stocks", [])
        if not stocks:
            continue

        run_date = datetime.date.fromisoformat(iso_date)
        start_date = (run_date - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
        end_date = iso_date

        taiex_key = f"TAIEX@{iso_date}"
        taiex_close = get_series(
            cache, taiex_key,
            lambda sd=start_date, ed=end_date: common.fetch_daily_close(api, "TAIEX", sd, ed),
        )

        for s in stocks:
            code = s["code"]
            done += 1
            key = f"{code}@{iso_date}"
            common.log(f"[{done}/{total_stocks}] {iso_date} {code}{s.get('name', '')} ……")
            try:
                stock_close = get_series(
                    cache, key,
                    lambda c=code, sd=start_date, ed=end_date: common.fetch_daily_close(api, c, sd, ed),
                )
                if stock_close.empty:
                    common.log("    沒有股價資料，保留原本的圖，略過重畫。")
                    continue
                img_path = common.REPO_ROOT / s["img"]
                common.plot_overlay(code, code, stock_close, taiex_close, img_path)
                redrawn += 1
            except Exception as exc:  # noqa: BLE001 — 單檔失敗不要讓整批當掉，保留原圖
                common.log(f"    處理失敗，保留原本的圖，略過：{exc}")
                failed += 1

    common.log(f"\n全部處理完畢：重畫 {redrawn} 張、失敗略過 {failed} 張"
               f"（總共 {total_stocks} 張）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
