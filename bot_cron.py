#!/usr/bin/env python3
"""
JP225 事件推播 — GitHub Actions / cron 版
================================================================
與 bot.py 的差別：

  bot.py       無限迴圈，常駐執行，用 state.json 記狀態
  bot_cron.py  單次執行即結束，完全無狀態

無狀態怎麼做到的：
  不再「事件當下拍快照、事後比對」，改成事後直接把 Yahoo 的
  5 分鐘 K 序列拉下來，用時間戳查出事件當下與 N 分鐘後的價格。
  這樣每次執行都是獨立的，不需要保存任何東西。

判斷「這次該不該發」：
  每次執行只看最近 WINDOW 分鐘的區間。若某個通知的觸發時刻
  落在 [now - WINDOW, now) 之內就發，否則跳過。
  cron 每 WINDOW 分鐘跑一次，每個通知自然只會落在一個視窗裡。

設定一律走環境變數（GitHub Secrets），不讀 config.json。
================================================================
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

TW = timezone(timedelta(hours=8))

EVENTS = [
    {"key": "fomc",     "name": "FOMC 利率決議",      "min": 120,  "lift": 89, "kind": "macro", "dst": True},
    {"key": "jp_open",  "name": "日本現貨開盤",        "min": 480,  "lift": 52, "kind": "open",  "dst": False},
    {"key": "jp_pm",    "name": "日本午盤重開",        "min": 690,  "lift": 65, "kind": "open",  "dst": False},
    {"key": "jp_close", "name": "日本大引け",          "min": 870,  "lift": 85, "kind": "close", "dst": False},
    {"key": "eu_open",  "name": "歐洲開盤",            "min": 900,  "lift": 43, "kind": "open",  "dst": True},
    {"key": "us_data",  "name": "美國 08:30ET 數據",   "min": 1230, "lift": 62, "kind": "macro", "dst": True},
    {"key": "us_open",  "name": "美股開盤",            "min": 1290, "lift": 33, "kind": "open",  "dst": True},
]

ICON = {"open": "🟢", "close": "🟠", "macro": "🩷"}


def env(name, default=""):
    return os.environ.get(name, default).strip()


TOKEN     = env("TELEGRAM_BOT_TOKEN")
CHAT_ID   = env("TELEGRAM_CHAT_ID")

# chat_id 自動發現：env → chat_id.txt → getUpdates
# 使用者只要對 bot 發一次任何訊息，下次排程就會抓到並寫回 repo，永久生效。
CHAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_id.txt")


NEWLY_FOUND = False   # 僅在「本次才從 getUpdates 發現」時為 True


def discover_chat_id():
    global CHAT_ID, NEWLY_FOUND
    if CHAT_ID:
        return CHAT_ID
    try:
        if os.path.exists(CHAT_FILE):
            v = open(CHAT_FILE, encoding="utf-8").read().strip()
            if v:
                CHAT_ID = v
                print(f"[chat_id] 由 chat_id.txt 取得：{v}")
                return v
    except Exception as e:
        print(f"[chat_id] 讀檔失敗：{e}")
    if not TOKEN:
        return ""
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20)
        r.raise_for_status()
        ids = []
        for it in r.json().get("result", []):
            m = it.get("message") or it.get("edited_message") or it.get("channel_post") or {}
            c = (m.get("chat") or {}).get("id")
            if c:
                ids.append(str(c))
        if ids:
            CHAT_ID = ids[-1]
            NEWLY_FOUND = True
            try:
                with open(CHAT_FILE, "w", encoding="utf-8") as f:
                    f.write(CHAT_ID)
                print(f"[chat_id] 已發現並寫入 chat_id.txt：{CHAT_ID}")
            except Exception as e:
                print(f"[chat_id] 寫檔失敗（本次仍可發送）：{e}")
            return CHAT_ID
        print("[chat_id] getUpdates 無資料 — 請對 bot 發一則訊息（例如 /start）")
    except Exception as e:
        print(f"[chat_id] getUpdates 失敗：{e}")
    return ""
SYMBOL    = env("SYMBOL", "^N225")
IS_DST    = env("IS_DST", "true").lower() == "true"
PRE_MIN   = int(env("PRE_ALERT_MIN", "10"))
REACT_MIN = int(env("REACTION_MIN", "30"))
DIGEST_AT = env("DAILY_DIGEST_AT", "07:00")
WINDOW    = int(env("WINDOW_MIN", "5"))
NEWS_ON   = env("ENABLE_NEWS", "false").lower() == "true"
NEWS_KEY  = env("NEWS_API_KEY")
NEWS_Q    = env("NEWS_QUERY", 'Nikkei OR "Japan stocks" OR BOJ')


def fmt_hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def ev_minute(ev):
    return ev["min"] + (0 if (IS_DST or not ev["dst"]) else 60)


def send(text):
    if not TOKEN:
        print("缺少 TELEGRAM_BOT_TOKEN")
        return False
    if not CHAT_ID:
        print("缺少 chat_id — 請對 bot 發一則訊息後等下次排程")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20)
        if r.status_code != 200:
            print(f"[telegram] {r.status_code} {r.text[:200]}")
            return False
        print(f"[telegram] 已送出 {len(text)} 字元")
        return True
    except Exception as e:
        print(f"[telegram] {e}")
        return False


def fetch_series():
    """回傳 [(datetime_tw, close), ...]，失敗回空清單。"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}",
            params={"interval": "5m", "range": "2d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        cl = res["indicators"]["quote"][0]["close"]
        return [(datetime.fromtimestamp(t, TW), c)
                for t, c in zip(ts, cl) if c is not None]
    except Exception as e:
        print(f"[price] {e}")
        return []


def price_at(series, target, tol_min=20):
    """取最接近 target 的收盤價；超過容許誤差回 None（避免拿到隔夜舊價）。"""
    if not series:
        return None
    best, best_d = None, None
    for dt, c in series:
        d = abs((dt - target).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best_d is not None and best_d <= tol_min * 60:
        return best
    return None


def due(now, trigger_min, today):
    """觸發時刻是否落在 [now - WINDOW, now) 內。"""
    trig = today.replace(hour=trigger_min // 60 % 24,
                         minute=trigger_min % 60,
                         second=0, microsecond=0)
    if trigger_min >= 1440:
        trig += timedelta(days=1)
    return now - timedelta(minutes=WINDOW) <= trig < now


POS = ["rally", "surge", "jump", "gain", "rise", "beat", "upgrade", "optimism", "record high", "climb"]
NEG = ["fall", "drop", "plunge", "slump", "loss", "miss", "downgrade", "fear", "selloff", "tumble", "slide"]


def sentiment(title):
    t = (title or "").lower()
    p, n = sum(w in t for w in POS), sum(w in t for w in NEG)
    return "🟢 偏正面" if p > n else "🔴 偏負面" if n > p else "⚪ 中性/不明"


def news_block():
    if not NEWS_ON or not NEWS_KEY:
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": NEWS_Q, "from": since, "sortBy": "publishedAt",
            "language": "en", "pageSize": 5, "apiKey": NEWS_KEY}, timeout=20)
        r.raise_for_status()
        arts = r.json().get("articles", [])[:5]
    except Exception as e:
        print(f"[news] {e}")
        return None
    if not arts:
        return None
    lines = ["<b>📰 近期日股相關新聞</b>", ""]
    for a in arts:
        src = (a.get("source") or {}).get("name", "?")
        lines.append(f"{sentiment(a.get('title'))}  <a href=\"{a.get('url')}\">{a.get('title')}</a>\n<i>{src}</i>\n")
    lines.append("<i>情緒為關鍵字啟發法，非 NLP 模型</i>")
    return "\n".join(lines)


def main():
    now = datetime.now(TW)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"執行時間（台北）：{now:%Y-%m-%d %H:%M:%S}　視窗 {WINDOW} 分鐘")

    cid = discover_chat_id()
    if not cid:
        print("尚無 chat_id，本次不發送。請對 bot 發一則訊息（/start）後等下次排程。")
        return 0

    # 只在「本次才從 getUpdates 發現 chat_id」時打招呼。
    # 不能用本地旗標檔判斷 —— Actions 每次都是全新容器，旗標不會留存，
    # 會導致每 5 分鐘重發一次問候（一天 288 則）。
    # chat_id.txt 有被 commit 回 repo，所以之後的執行都會走「由檔案取得」這條路。
    if NEWLY_FOUND:
        send("🤖 <b>JP225 事件推播已連線</b>\n\n"
             f"chat_id：<code>{cid}</code>\n"
             "已自動記錄，之後不需再設定。\n\n"
             "<i>這是資訊推播工具，不是交易訊號。</i>")

    sent = 0

    # 每日摘要
    dh, dm = (int(x) for x in DIGEST_AT.split(":"))
    if due(now, dh * 60 + dm, today):
        evs = sorted(EVENTS, key=ev_minute)
        lines = [f"<b>📅 JP225 今日事件</b>  {now:%Y-%m-%d}（台北時間）", ""]
        for ev in evs:
            lines.append(f"{ICON[ev['kind']]} <code>{fmt_hm(ev_minute(ev))}</code>  {ev['name']}  <i>+{ev['lift']}%</i>")
        lines += ["", "<i>+% = 該時段歷史平均波動相對鄰近時段的跳升幅度</i>",
                  "<i>粉紅為「可能有數據」的時間點，不代表今天一定有</i>"]
        if send("\n".join(lines)):
            sent += 1
        nb = news_block()
        if nb and send(nb):
            sent += 1

    series = None

    for ev in EVENTS:
        m = ev_minute(ev)

        # 事前提醒
        if due(now, m - PRE_MIN, today):
            if send(f"{ICON[ev['kind']]} <b>{ev['name']}</b> 還有 {PRE_MIN} 分鐘\n"
                    f"台北 <code>{fmt_hm(m)}</code>　歷史波動跳升 <b>+{ev['lift']}%</b>\n\n"
                    f"<i>波動即將放大，停損請放寬</i>"):
                sent += 1

        # 事後市場反應
        if due(now, m + REACT_MIN, today):
            if series is None:
                series = fetch_series()
            t0 = today.replace(hour=m // 60 % 24, minute=m % 60)
            t1 = t0 + timedelta(minutes=REACT_MIN)
            p0, p1 = price_at(series, t0), price_at(series, t1)
            if p0 and p1:
                d = p1 - p0
                arrow = "▲" if d > 0 else "▼" if d < 0 else "＝"
                tone = "市場正面解讀" if d > 0 else "市場負面解讀" if d < 0 else "市場無反應"
                if send(f"{ICON[ev['kind']]} <b>{ev['name']}</b> 事件後 {REACT_MIN} 分鐘\n"
                        f"<code>{p0:,.0f} → {p1:,.0f}</code>\n"
                        f"<b>{arrow} {d:+,.0f} 點（{d / p0 * 100:+.2f}%）</b> — {tone}\n\n"
                        f"<i>這是市場反應，不是新聞情緒</i>"):
                    sent += 1
            else:
                print(f"[skip] {ev['name']} 無盤中報價（{SYMBOL} 該時段可能休市）")

    print(f"本次送出 {sent} 則")
    return 0


if __name__ == "__main__":
    sys.exit(main())
