#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重建「MoneyDJ 選股大師 — 最近一個交易日股價創 30 個交易日新高」的名單，
給補歷史資料的 backfill.py，以及排程漏跑時 crash_day_pipeline.py 補抓用。

MoneyDJ 那個頁面只顯示「今天」的名單、沒有日期參數，過去的日子沒辦法直接
查詢，所以改成自己從證交所(上市)/櫃買中心(上櫃)每天公布的「全市場收盤行情」
重建：抓每個交易日全市場的收盤價，堆成一個 code -> {date: close} 的表，
再對每個目標日期算「收盤價是不是近 30 個交易日最高」。

已知限制（老實講）：
  - 上市(TWSE)的部分有實際驗證過欄位格式，應該穩定。
  - 上櫃(TPEx)：2026-11-12 起改用櫃買中心新版網站的行情端點，可依日期查詢，
    並會核對回傳日期(舊的 OpenAPI 端點不支援歷史日期，曾因此關閉上櫃抓取)。
  - 這是「用同一套規則重新算一次」，不是去 MoneyDJ 真正保存的歷史紀錄，所以
    不保證跟當時 MoneyDJ 頁面顯示的一模一樣（例如 MoneyDJ 本身可能還有其他
    沒公開的篩選條件）。
"""

from __future__ import annotations

import datetime
import re
import time

import requests

import common

TWSE_DAILY_ALL_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
TPEX_DAILY_ALL_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

STOCK_CODE_RE = re.compile(r"^[1-9]\d{3}$")  # 排除 00xx(ETF)、5碼以上(權證等)

REQUEST_DELAY = 0.6  # 對證交所/櫃買中心的請求間隔，禮貌一點


def _to_float(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if s in ("", "--", "---", "----", "除權", "除息", "除權息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_twse_daily_all(date: datetime.date) -> dict[str, float]:
    """上市：某一天全市場個股收盤價，{code: close}。非交易日回傳 {}。"""
    resp = requests.get(
        TWSE_DAILY_ALL_URL,
        params={"response": "json", "date": date.strftime("%Y%m%d"), "type": "ALLBUT0999"},
        headers=common.REQUEST_HEADERS,
        timeout=30,
    )
    if resp.status_code != 200:
        return {}
    try:
        payload = resp.json()
    except ValueError:
        return {}
    if payload.get("stat") != "OK":
        return {}

    tables = payload.get("tables", [])
    target = None
    for t in tables:
        if "每日收盤行情" in (t.get("title") or ""):
            target = t
            break
    if target is None:
        return {}

    out = {}
    for row in target.get("data", []):
        code = row[0].strip()
        if not STOCK_CODE_RE.match(code):
            continue
        close = _to_float(row[8])
        if close is None:
            continue
        out[code] = close
    return out


# 2026-11-12 更新：舊的 OpenAPI 端點(tpex_mainboard_daily_close_quotes)不管
# 查哪天都回傳當下行情，會造成假陽性，所以曾經整個關閉上櫃抓取。改用櫃買中心
# 新版網站的「上櫃股票行情」端點，實測可以依日期查歷史資料；保險起見仍會核對
# 回傳的日期，對不上就當作沒資料，不會再出現整段視窗都是同一個數字的情況。
def fetch_tpex_daily_all(date: datetime.date) -> dict[str, float]:
    """上櫃：某一天全市場個股收盤價，{code: close}。非交易日或日期對不上回傳 {}。"""
    try:
        resp = requests.get(
            TPEX_DAILY_ALL_URL,
            params={"date": date.strftime("%Y/%m/%d"), "response": "json"},
            headers=common.REQUEST_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            return {}
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        common.log(f"    TPEx 上櫃行情抓取失敗({date})：{exc}")
        return {}

    if payload.get("date") != date.strftime("%Y%m%d"):
        return {}
    tables = payload.get("tables") or [{}]
    out = {}
    for row in tables[0].get("data", []):
        code = str(row[0]).strip()
        if not STOCK_CODE_RE.match(code):
            continue
        close = _to_float(str(row[2]))
        if close is None:
            continue
        out[code] = close
    return out


def build_price_panel(dates: list[datetime.date]) -> tuple[dict[str, dict[str, float]], list[str]]:
    """
    對一串日期(通常是某個目標日往前推需要的所有交易日)抓全市場收盤價，
    回傳 (panel, trading_days)：
      panel[code][iso_date] = close
      trading_days = 實際有交易的日期(iso)，由舊到新排序
    """
    panel: dict[str, dict[str, float]] = {}
    trading_days: list[str] = []

    for d in dates:
        iso = d.isoformat()
        twse = fetch_twse_daily_all(d)
        time.sleep(REQUEST_DELAY)
        tpex = fetch_tpex_daily_all(d)
        time.sleep(REQUEST_DELAY)

        if not twse and not tpex:
            continue  # 非交易日(假日)

        trading_days.append(iso)
        for code, close in {**twse, **tpex}.items():
            panel.setdefault(code, {})[iso] = close

    return panel, trading_days


def calendar_days_needed(target_dates: list[datetime.date], lookback_trading_days: int = 30,
                          buffer_calendar_days: int = 20) -> list[datetime.date]:
    """
    產生需要抓的「候選」日曆天清單：從最早目標日往前多抓一點緩衝(避開連假)，
    到最晚目標日為止。實際是不是交易日，由 build_price_panel 抓的時候自然
    篩掉(該天抓不到資料就跳過)。
    """
    earliest = min(target_dates) - datetime.timedelta(days=lookback_trading_days * 1.6 + buffer_calendar_days)
    latest = max(target_dates)
    days = []
    d = earliest
    while d <= latest:
        if d.weekday() < 5:  # 只嘗試平日，週末不用浪費請求
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def find_new_high_stocks(panel: dict[str, dict[str, float]], trading_days: list[str],
                          target_iso: str, lookback: int = 30, price_min: float = 60) -> list[str]:
    """
    回傳 target_iso 當天，收盤價創近 lookback 個交易日新高、且股價 > price_min
    的股票代號清單，由代號排序。
    """
    if target_iso not in trading_days:
        return []
    idx = trading_days.index(target_iso)
    if idx < 0:
        return []
    window = trading_days[max(0, idx - lookback + 1): idx + 1]

    result = []
    for code, series in panel.items():
        if target_iso not in series:
            continue
        today_close = series[target_iso]
        if today_close <= price_min:
            continue
        window_closes = [series[d] for d in window if d in series]
        if not window_closes:
            continue
        if today_close >= max(window_closes):
            result.append(code)
    return sorted(result)
