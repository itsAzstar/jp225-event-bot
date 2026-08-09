#!/usr/bin/env python3
"""
JP225 事件推播 Telegram Bot
================================================================
把 Pine 指標「日經 時段+事件 Pro v9」的功能搬到 Telegram。

Pine 做不到、這裡做得到的：
  - 真正的新聞（Pine 沒有 HTTP，Python 有）
  - 主動推播（不用盯著圖表）
  - 跨裝置（手機收得到）

事件時間與跳升 % 來自 2026-04-27~08-07 實測
（跳升 = 該時段平均絕對變動 vs 前後各兩格的局部基準）。

免責：這是資訊推播工具，不是交易訊號。方向那一層跨期間不穩，
      不要拿事件時間當進出場依據。
================================================================
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"

TW = timezone(timedelta(hours=8))  # 台北時間，全年固定不調

# ── 事件表（台北時間分鐘數，夏令基準）────────────────────────
# dst_shift=True 表示冬令時要 +60 分鐘（歐美市場）；亞洲不受影響
EVENTS = [
    {"key": "fomc",    "name": "FOMC 利率決議",  "min": 120,  "lift": 89, "kind": "macro", "dst": True},
    {"key": "jp_open", "name": "日本現貨開盤",    "min": 480,  "lift": 52, "kind": "open",  "dst": False},
    {"key": "jp_pm",   "name": "日本午盤重開",    "min": 690,  "lift": 65, "kind": "open",  "dst": False},
    {"key": "jp_close","name": "日本大引け",      "min": 870,  "lift": 85, "kind": "close", "dst": False},
    {"key": "eu_open", "name": "歐洲開盤",        "min": 900,  "lift": 43, "kind": "open",  "dst": True},
    {"key": "us_data", "name": "美國 08:30ET 數據", "min": 1230, "lift": 62, "kind": "macro", "dst": True},
    {"key": "us_open", "name": "美股開盤",        "min": 1290, "lift": 33, "kind": "open",  "dst": True},
]

ICON = {"open": "🟢", "close": "🟠", "macro": "🩷"}

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "symbol": "^N225",
    "is_dst": True,
    "pre_alert_min": 10,
    "reaction_min": 30,
    "daily_digest_at": "07:00",
    "poll_seconds": 30,
    "enable_news": False,
    "news_api_key": "",
    "news_query": "Nikkei OR \"Japan stocks\" OR BOJ"
}


# ── 基礎工具 ────────────────────────────────────────────────
def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已建立範本設定檔：{CONFIG_PATH}")
        print("請填入 telegram_bot_token 與 telegram_chat_id 後重新執行。")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        print("config.json 尚未填入 telegram_bot_token / telegram_chat_id。")
        sys.exit(1)
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "sent": [], "snap": {}}


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def now_tw():
    return datetime.now(TW)


def fmt_hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def ev_minute(ev, is_dst):
    """回傳該事件今日的台北分鐘數（冬令時歐美 +60）。"""
    return ev["min"] + (0 if (is_dst or not ev["dst"]) else 60)


# ── Telegram ────────────────────────────────────────────────
def send(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": cfg["telegram_chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code != 200:
            print(f"[telegram] {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[telegram] 發送失敗：{e}")
        return False


# ── 價格 ────────────────────────────────────────────────────
def get_price(symbol):
    """用 Yahoo Finance 公開端點取最新價。失敗回 None，不中斷主流程。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": "5m", "range": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return float(closes[-1]) if closes else None
    except Exception as e:
        print(f"[price] 取價失敗：{e}")
        return None


# ── 新聞（選用，需自備 API key）──────────────────────────────
def get_news(cfg, hours=6, limit=5):
    """
    用 NewsAPI 抓近期日股相關新聞。需要 newsapi.org 的免費 key。
    未啟用或失敗時回空清單 —— 不會編造內容。
    情緒判定用關鍵字比對，是粗略的啟發法，不是 NLP 模型。
    """
    if not cfg.get("enable_news") or not cfg.get("news_api_key"):
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": cfg["news_query"], "from": since, "sortBy": "publishedAt",
            "language": "en", "pageSize": limit, "apiKey": cfg["news_api_key"],
        }, timeout=20)
        r.raise_for_status()
        return r.json().get("articles", [])[:limit]
    except Exception as e:
        print(f"[news] 抓取失敗：{e}")
        return []


POS = ["rally", "surge", "jump", "gain", "rise", "beat", "upgrade", "optimism", "record high", "climb"]
NEG = ["fall", "drop", "plunge", "slump", "loss", "miss", "downgrade", "fear", "selloff", "tumble", "slide"]


def sentiment(title):
    """關鍵字啟發法。粗略但透明 —— 不假裝是情緒分析模型。"""
    t = (title or "").lower()
    p = sum(w in t for w in POS)
    n = sum(w in t for w in NEG)
    if p > n:
        return "🟢 偏正面"
    if n > p:
        return "🔴 偏負面"
    return "⚪ 中性/不明"


# ── 訊息組裝 ────────────────────────────────────────────────
def build_digest(cfg):
    is_dst = cfg["is_dst"]
    evs = sorted(EVENTS, key=lambda e: ev_minute(e, is_dst))
    lines = [f"<b>📅 JP225 今日事件</b>  {now_tw():%Y-%m-%d}（台北時間）", ""]
    for ev in evs:
        m = ev_minute(ev, is_dst)
        lines.append(f"{ICON[ev['kind']]} <code>{fmt_hm(m)}</code>  {ev['name']}  <i>+{ev['lift']}%</i>")
    lines += [
        "",
        "<i>+% = 該時段歷史平均波動相對鄰近時段的跳升幅度</i>",
        "<i>粉紅為「可能有數據」的時間點，不代表今天一定有</i>",
    ]
    return "\n".join(lines)


def build_pre(ev, m, mins_left):
    return (f"{ICON[ev['kind']]} <b>{ev['name']}</b> 還有 {mins_left} 分鐘\n"
            f"台北 <code>{fmt_hm(m)}</code>　歷史波動跳升 <b>+{ev['lift']}%</b>\n\n"
            f"<i>波動即將放大，停損請放寬</i>")


def build_react(ev, m, before, after, minutes):
    d = after - before
    arrow = "▲" if d > 0 else "▼" if d < 0 else "＝"
    tone = "市場正面解讀" if d > 0 else "市場負面解讀" if d < 0 else "市場無反應"
    pct = (d / before * 100) if before else 0
    return (f"{ICON[ev['kind']]} <b>{ev['name']}</b> 事件後 {minutes} 分鐘\n"
            f"<code>{before:,.0f} → {after:,.0f}</code>\n"
            f"<b>{arrow} {d:+,.0f} 點（{pct:+.2f}%）</b> — {tone}\n\n"
            f"<i>這是市場反應，不是新聞情緒</i>")


def build_news(articles):
    if not articles:
        return None
    lines = ["<b>📰 近期日股相關新聞</b>", ""]
    for a in articles:
        s = sentiment(a.get("title"))
        src = (a.get("source") or {}).get("name", "?")
        lines.append(f"{s}  <a href=\"{a.get('url')}\">{a.get('title')}</a>\n<i>{src}</i>\n")
    lines.append("<i>情緒為關鍵字啟發法，非 NLP 模型，僅供快速掃描</i>")
    return "\n".join(lines)


# ── 主迴圈 ──────────────────────────────────────────────────
def main():
    cfg = load_config()
    st = load_state()
    is_dst = cfg["is_dst"]
    pre = int(cfg["pre_alert_min"])
    react = int(cfg["reaction_min"])
    dig_h, dig_m = (int(x) for x in cfg["daily_digest_at"].split(":"))
    poll = int(cfg["poll_seconds"])

    print("JP225 事件推播已啟動。Ctrl+C 停止。")
    send(cfg, "🤖 <b>JP225 事件推播已啟動</b>\n\n" + build_digest(cfg))

    while True:
        try:
            n = now_tw()
            today = n.strftime("%Y-%m-%d")
            cur = n.hour * 60 + n.minute

            if st.get("date") != today:
                st = {"date": today, "sent": [], "snap": {}}
                save_state(st)

            # 每日摘要
            tag = "digest"
            if cur == dig_h * 60 + dig_m and tag not in st["sent"]:
                send(cfg, build_digest(cfg))
                nb = build_news(get_news(cfg))
                if nb:
                    send(cfg, nb)
                st["sent"].append(tag)
                save_state(st)

            for ev in EVENTS:
                m = ev_minute(ev, is_dst)

                # 事前提醒
                t_pre = f"pre:{ev['key']}"
                if cur == m - pre and t_pre not in st["sent"]:
                    send(cfg, build_pre(ev, m, pre))
                    st["sent"].append(t_pre)
                    save_state(st)

                # 事件當下：記錄基準價
                t_snap = f"snap:{ev['key']}"
                if cur == m and t_snap not in st["sent"]:
                    p = get_price(cfg["symbol"])
                    if p is not None:
                        st["snap"][ev["key"]] = p
                    st["sent"].append(t_snap)
                    save_state(st)

                # 事件後：市場反應
                t_re = f"react:{ev['key']}"
                if cur == m + react and t_re not in st["sent"]:
                    before = st["snap"].get(ev["key"])
                    after = get_price(cfg["symbol"])
                    if before and after:
                        send(cfg, build_react(ev, m, before, after, react))
                    st["sent"].append(t_re)
                    save_state(st)

            time.sleep(poll)

        except KeyboardInterrupt:
            print("\n已停止。")
            break
        except Exception as e:
            print(f"[loop] {e}")
            time.sleep(poll)


if __name__ == "__main__":
    main()
