#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
測試用：驗證「新的」TPEx 上櫃歷史行情候選端點，是不是真的有依照日期回傳
不同的資料。

背景：上一版用的 tpex_mainboard_daily_close_quotes 這個端點，不管查哪一天
都回傳「當下」的即時報價，等於完全沒有歷史查詢功能，已經證實是錯的、也
已經關閉。這支改試另一個候選端點(TPEx 網站上「上櫃股票每日收盤行情」查詢
頁面實際呼叫的那個)，但我沒辦法在我這邊實際打(網路環境不允許)，所以**先
用這支單獨驗證，確認沒問題之後才會把它接回主流程**，不會重蹈覆轍。

用法：
    python scripts/test_tpex_endpoint.py

會查 3 個相隔很遠的日期(2025-01-15、2025-04-15、2025-06-19)，各自印出同一
檔上櫃股票(預設用 6488 環球晶，可以改成你自己熟悉股價、確定是上櫃的代號)
的收盤價原始資料列。

怎麼判斷結果：
  - 如果 3 個日期印出來的收盤價都一模一樣 → 這個端點大概率又是「不管查哪天
    都回傳同一份資料」，不能用，把完整輸出貼給我，我再想別的辦法。
  - 如果 3 個日期的價格看起來合理不同 → 麻煩你自己找一個管道(例如你平常看
    盤的軟體、Goodinfo、Yahoo奇摩股市)核對一下 2025-06-19 那天 6488 的真實
    收盤價，跟這支印出來的數字是否一致，把兩邊的數字都告訴我。
  - 如果整個執行失敗(連不上、回傳的不是 JSON)，把錯誤訊息貼給我。
"""

from __future__ import annotations

import sys

import requests

import common

TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"

TEST_DATES = ["2025-01-15", "2025-04-15", "2025-06-19"]
TEST_CODE = "6488"  # 環球晶，隨便挑一檔知名上櫃股；可以改成你自己想核對的上櫃代號


def to_roc(date_iso: str) -> str:
    y, m, d = date_iso.split("-")
    return f"{int(y) - 1911}/{m}/{d}"


def main() -> int:
    for date_iso in TEST_DATES:
        roc_date = to_roc(date_iso)
        print(f"=== {date_iso}（民國 {roc_date}） ===")
        try:
            resp = requests.get(
                TPEX_URL,
                params={"l": "zh-tw", "d": roc_date, "se": "EW", "o": "json"},
                headers=common.REQUEST_HEADERS,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  請求失敗：{exc}\n")
            continue

        print(f"  HTTP 狀態碼：{resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            print(f"  回傳的不是 JSON，前 300 字內容：{resp.text[:300]!r}\n")
            continue

        rows = None
        if isinstance(payload, dict):
            if isinstance(payload.get("aaData"), list):
                rows = payload["aaData"]
            elif isinstance(payload.get("tables"), list) and payload["tables"]:
                rows = payload["tables"][0].get("data", [])
        if rows is None:
            keys = list(payload.keys()) if isinstance(payload, dict) else type(payload)
            print(f"  抓不到預期的資料欄位，回傳內容的最外層長這樣（前 500 字）："
                  f"{str(payload)[:500]}\n  最外層 keys：{keys}\n")
            continue

        print(f"  總筆數：{len(rows)}")
        found = None
        for row in rows:
            if isinstance(row, list) and row and str(row[0]).strip() == TEST_CODE:
                found = row
                break
            if isinstance(row, dict):
                code = str(row.get("SecuritiesCompanyCode") or row.get("CompanyCode")
                            or row.get("Code") or "").strip()
                if code == TEST_CODE:
                    found = row
                    break

        if found is not None:
            print(f"  代號 {TEST_CODE} 這一列原始資料：{found}")
        else:
            print(f"  這次資料裡沒找到代號 {TEST_CODE}，貼前 2 筆看看欄位長相：{rows[:2]}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
