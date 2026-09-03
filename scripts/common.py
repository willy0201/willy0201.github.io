#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共用工具：日常自動化(crash_day_pipeline.py)跟一次性補資料
(backfill.py)都會用到的部分，抽出來避免兩邊邏輯兜不起來。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

STOCK_CODE_RE = re.compile(r"^[1-9]\d{3}$")  # 排除 00xx(ETF)、5碼以上(權證等)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JSON_PATH = REPO_ROOT / "data.json"
PICTURES_DIR = REPO_ROOT / "pictures"

TWSE_MONTH_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
TWSE_ISIN_URLS = [
    "https://isin.twse.com.tw/isin/class_main.jsp?owncode=&stockname=&isincode=&market=1&issuetype=1&industry_code=&Page=1&chklike=Y",  # 上市
    "https://isin.twse.com.tw/isin/class_main.jsp?owncode=&stockname=&isincode=&market=2&issuetype=4&industry_code=&Page=1&chklike=Y",  # 上櫃
]

REQUEST_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def log(msg: str) -> None:
    print(f"[crash-day] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 大盤重挫判斷（官方證交所資料）
# ---------------------------------------------------------------------------

def roc_to_iso(roc_date: str) -> str:
    """'113/08/05' -> '2024-08-05'"""
    y, m, d = roc_date.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def fetch_taiex_month(yyyymm: str) -> list[tuple[str, float]]:
    """回傳 [(iso_date, close), ...]，依日期由舊到新排序。"""
    resp = requests.get(
        TWSE_MONTH_URL,
        params={"date": f"{yyyymm}01", "response": "json"},
        headers=REQUEST_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        return []
    rows = []
    for row in payload["data"]:
        iso = roc_to_iso(row[0])
        close = float(row[4].replace(",", ""))
        rows.append((iso, close))
    return rows


def get_taiex_trigger(run_date) -> dict | None:
    """
    找出 run_date(datetime.date)當天的大盤收盤，和前一個交易日收盤比較。
    若 run_date 當天資料還沒出來，回傳 None。
    """
    import datetime

    yyyymm = run_date.strftime("%Y%m")
    rows = fetch_taiex_month(yyyymm)

    iso_today = run_date.isoformat()
    idx = next((i for i, (d, _) in enumerate(rows) if d == iso_today), None)
    if idx is None:
        log(f"官方資料尚未有 {iso_today} 的收盤指數，先跳過。")
        return None

    if idx == 0:
        prev_month = (run_date.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y%m")
        prev_rows = fetch_taiex_month(prev_month)
        if not prev_rows:
            log("找不到前一交易日的大盤收盤資料，先跳過。")
            return None
        prev_close = prev_rows[-1][1]
    else:
        prev_close = rows[idx - 1][1]

    close = rows[idx][1]
    drop_pts = round(prev_close - close, 2)
    drop_pct = round(drop_pts / prev_close * 100, 2)
    return {"close": close, "prev": prev_close, "dropPts": drop_pts, "dropPct": drop_pct}


# ---------------------------------------------------------------------------
# 股票代號 -> 中文名稱
# ---------------------------------------------------------------------------

def enrich_stock_names(stock_ids: list[str]) -> dict[str, str]:
    code_to_name: dict[str, str] = {}
    remaining = set(stock_ids)
    for url in TWSE_ISIN_URLS:
        if not remaining:
            break
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
        listed = pd.read_html(resp.text)[0]
        listed.columns = listed.iloc[0, :]
        listed = listed[["有價證券代號", "有價證券名稱"]].iloc[1:]
        lookup = dict(zip(listed["有價證券代號"], listed["有價證券名稱"]))
        for code in list(remaining):
            if code in lookup:
                code_to_name[code] = lookup[code]
                remaining.discard(code)
    for code in remaining:
        log(f"警告：代號 {code} 在上市/上櫃清單都找不到名稱，網站上會只顯示代號。")
        code_to_name[code] = ""
    return code_to_name


def get_otc_stock_codes() -> list[str]:
    """回傳所有上櫃(TPEx)股票代號清單，來源是證交所 ISIN 上櫃股票清單。"""
    resp = requests.get(TWSE_ISIN_URLS[1], headers=REQUEST_HEADERS, timeout=20)
    listed = pd.read_html(resp.text)[0]
    listed.columns = listed.iloc[0, :]
    listed = listed[["有價證券代號"]].iloc[1:]
    codes = listed["有價證券代號"].astype(str).str.strip().tolist()
    return sorted({c for c in codes if STOCK_CODE_RE.match(c)})


def date_key(d) -> str:
    """把 pandas/py 的日期物件或字串統一轉成 'YYYY-MM-DD'，避免帶時間的
    Timestamp 字串('2025-06-19 00:00:00')跟純日期字串對不起來。"""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


# ---------------------------------------------------------------------------
# FinMind 抓價、畫疊圖
# ---------------------------------------------------------------------------

def get_finmind_loader():
    from FinMind.data import DataLoader
    return DataLoader()


FINMIND_RATE_LIMIT_WAIT_SECONDS = 3900  # 匿名額度用完後，休息多久再自動重試(65分鐘，保守多留5分鐘緩衝)
FINMIND_MAX_RETRIES = 8                 # 最多自動重試幾次才放棄(8次 x 65分鐘 涵蓋約8小時)


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "upper limit" in msg or "reach the upper" in msg or "402" in msg


def fetch_daily_close(api, stock_id: str, start_date: str, end_date: str) -> pd.Series:
    """
    抓個股(或大盤)收盤價。FinMind 匿名連線平均每小時大約 300 次請求的額度，
    補歷史資料這種量一定會用完，用完會丟出「Requests reach the upper
    limit」的例外。這裡遇到這種情況會自動睡到下一個額度週期再重試，不用
    你人工重開程式；如果是其他種類的錯誤(不是額度問題)，會直接往外丟出，
    交給呼叫端處理(單檔股票失敗只略過那一檔，不影響其他天)。
    """
    for attempt in range(1, FINMIND_MAX_RETRIES + 1):
        try:
            df = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date, end_date=end_date)
            df = df[df["close"] != 0]
            df.set_index("date", inplace=True)
            return df["close"]
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit_error(exc) or attempt == FINMIND_MAX_RETRIES:
                raise
            wait_min = FINMIND_RATE_LIMIT_WAIT_SECONDS // 60
            log(f"    FinMind 匿名額度用完(第 {attempt} 次)，休息 {wait_min} 分鐘後自動重試，"
                f"這段時間可以放著不用管……")
            time.sleep(FINMIND_RATE_LIMIT_WAIT_SECONDS)
    raise RuntimeError("FinMind 多次重試後仍然被限速，請稍後手動重新執行一次。")


TICK_INTERVAL_MONTHS = 3  # 疊圖 X 軸日期標籤間隔，改這個數字就能調整(3~4 個月都合理)


def plot_overlay(stock_id: str, stock_label: str, stock_close: pd.Series,
                  taiex_close: pd.Series, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    # 確保 X 軸是真正的日期型別(不是純字串)，這樣才能用「每 N 個月標一次」
    # 精準控制標籤密度，不會因為每檔股票/每個日期視窗長度不完全一樣，導致
    # 標籤忽多忽少、有時候太密。
    stock_close = stock_close.copy()
    stock_close.index = pd.to_datetime(stock_close.index)
    taiex_close = taiex_close.copy()
    taiex_close.index = pd.to_datetime(taiex_close.index)

    fig, ax = plt.subplots(figsize=(12, 5))
    plt.grid()

    ax2 = ax.twinx()
    ax2.plot(taiex_close, color="skyblue", label="TAIEX")
    ax2.set_ylabel("TAIEX", color="skyblue", fontsize=20)
    ax2.tick_params(axis="y", labelcolor="skyblue")
    ax2.legend(loc="upper right")

    # 圖例/座標軸文字只用代號(不含中文名稱)：matplotlib預設字型不含中文字形
    ax.plot(stock_close, color="red", label=stock_label)
    ax.set_ylabel(stock_label, color="red", fontsize=20)
    ax.tick_params(axis="y", labelcolor="red")
    ax.legend(loc="upper left")

    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=TICK_INTERVAL_MONTHS))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# data.json 讀寫
# ---------------------------------------------------------------------------

def load_data_json() -> list[dict]:
    if not DATA_JSON_PATH.exists():
        return []
    with open(DATA_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_data_json(entries: list[dict]) -> None:
    entries_sorted = sorted(entries, key=lambda e: e["date"], reverse=True)
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(entries_sorted, f, ensure_ascii=False, indent=1)
        f.write("\n")


def sleep_polite(seconds: float) -> None:
    time.sleep(seconds)
