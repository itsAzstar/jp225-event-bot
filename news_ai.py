#!/usr/bin/env python3
"""
新聞抓取 + DeepSeek 分析
================================================================
DeepSeek 是 LLM，本身不會抓新聞。分工如下：

  Google News RSS  →  提供標題／時間／來源（免金鑰）
  DeepSeek         →  把標題摘要成簡潔結論

防幻覺設計（這是本模組的重點）：
  1. 只餵標題給模型，不餵任何我們沒實際取得的內容
  2. system prompt 明確要求：只根據提供的標題作答，
     不得補充未出現在標題中的數字、事件或因果推論
  3. 要求模型在資訊不足時直接說「標題資訊不足」
  4. 輸出附上實際使用的標題數與時間範圍，方便查核
  5. API 失敗時回 None，絕不編造內容
================================================================
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

TW = timezone(timedelta(hours=8))

GOOGLE_NEWS = "https://news.google.com/rss/search"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

DEFAULT_QUERY = "Nikkei 225 OR Japan stocks OR Bank of Japan"


def fetch_news(query=DEFAULT_QUERY, since=None, until=None, limit=40):
    """
    從 Google News RSS 取新聞。
    since / until 為 aware datetime（台北時區），None 表示不限。
    回傳 [{title, source, url, dt}]，依時間新到舊排序。
    """
    try:
        r = requests.get(GOOGLE_NEWS,
                         params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[news] 抓取失敗：{e}")
        return []

    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TW)
        except Exception:
            continue
        if since and dt < since:
            continue
        if until and dt > until:
            continue
        src = ""
        m = re.search(r"\s-\s([^-]+)$", title)
        if m:
            src = m.group(1).strip()
            title = title[:m.start()].strip()
        out.append({"title": title, "source": src,
                    "url": (it.findtext("link") or "").strip(), "dt": dt})

    out.sort(key=lambda x: x["dt"], reverse=True)
    return out[:limit]


SYSTEM_PROMPT = """你是一位嚴謹的金融資訊摘要員，服務對象是日經225指數的交易者。

鐵則（違反即為失敗）：
1. 只能根據使用者提供的新聞標題作答。標題沒寫的，一律不准補充。
2. 禁止編造任何數字、百分比、公司名、政策細節或事件。
   標題若無具體數字，就不要出現數字。
3. 禁止因果推論超出標題所述。標題說「股市下跌」就只能說下跌，
   不能自行推斷原因。
4. 資訊不足時，直接寫「標題資訊不足，無法判斷」。這是正確答案，不是失敗。
5. 不提供投資建議、不預測後市、不給進出場點位。
6. 不要寫「標題未提及X」這類句子。你一旦寫出 X，就是在引入標題中
   不存在的概念。缺什麼由系統自動說明，不需要你補。

每一行都必須用半形括號標註來源標題編號，例如 (#3)。
沒有編號可標的句子會被系統自動刪除，所以不要寫。

輸出格式（繁體中文，全部合計不超過 150 字）：

【整體偏向】正面 / 負面 / 中性 / 分歧 (#編號)
【重點】
- 第一條，不超過 30 字 (#編號)
- 第二條 (#編號)
- 第三條 (#編號)

最多三條。語氣平實，不用感嘆詞，不誇大。不要輸出其他區塊。"""

VERIFY_PROMPT = """你是事實查核員。使用者會給你「原始標題清單」與「一份摘要」。

你的工作：逐句檢查摘要中的每個具體說法（事件、公司名、數字、指標名稱），
是否能在標題清單中找到直接依據。

- 有依據的：保留，維持原樣
- 沒有依據的：整句刪除，不要改寫、不要保留部分
- 刪除後若某個區塊變空，寫「（無可佐證內容）」

只輸出清理後的摘要本身，不要任何說明、前言或標記你做了什麼。
保持原本的【整體偏向】【重點】【不確定處】格式。"""


def _numbers(text):
    """
    抽出文字中的數字，用於機械檢核。
    先移除 (#3) 這類來源引用標記 —— 那是我們自己要求模型加的，
    不是內容數字，否則會產生誤報。
    """
    t = re.sub(r"#\s*\d+", " ", text or "")
    return set(re.findall(r"\d+(?:[.,]\d+)*", t))


def verify(summary, articles, key, model="deepseek-chat", timeout=60):
    """
    二次查核：把摘要與原始標題送回模型，刪除無依據的說法。
    失敗時回原文（附警告），不中斷流程。
    """
    numbered = "\n".join(f"#{i+1} [{a['dt']:%m-%d %H:%M}] {a['title']}"
                         for i, a in enumerate(articles))
    try:
        r = requests.post(DEEPSEEK_URL,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model,
                                "messages": [
                                    {"role": "system", "content": VERIFY_PROMPT},
                                    {"role": "user", "content":
                                     f"原始標題清單：\n{numbered}\n\n摘要：\n{summary}"}],
                                "temperature": 0,
                                "max_tokens": 600},
                          timeout=timeout)
        if r.status_code != 200:
            print(f"[verify] HTTP {r.status_code}")
            return summary, False
        return r.json()["choices"][0]["message"]["content"].strip(), True
    except Exception as e:
        print(f"[verify] {e}")
        return summary, False


def analyse(articles, api_key=None, model="deepseek-chat", timeout=60):
    """
    把標題交給 DeepSeek 摘要。
    回傳 dict{text, used, span, usage} 或 None（失敗時不編造）。
    """
    key = api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("[deepseek] 缺少 DEEPSEEK_API_KEY")
        return None
    if not articles:
        print("[deepseek] 無新聞可分析")
        return None

    lines = []
    for i, a in enumerate(articles):
        src = f"（{a['source']}）" if a["source"] else ""
        lines.append(f"#{i+1} [{a['dt']:%m-%d %H:%M}] {a['title']}{src}")
    payload = "以下是日經225相關新聞標題，請依規則摘要：\n\n" + "\n".join(lines)

    try:
        r = requests.post(DEEPSEEK_URL,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model,
                                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                             {"role": "user", "content": payload}],
                                "temperature": 0,
                                "max_tokens": 600},
                          timeout=timeout)
        if r.status_code != 200:
            print(f"[deepseek] HTTP {r.status_code} {r.text[:200]}")
            return None
        d = r.json()
        raw = d["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[deepseek] {e}")
        return None

    # ── 第二關：模型自我查核，刪除無依據的說法 ──
    cleaned, ok = verify(raw, articles, key, model, timeout)

    # ── 第三關：程式強制刪除沒有來源編號的內容行 ──
    # 標題是英文、摘要是中文，無法用字串比對驗證實體，
    # 因此改用「每句必須標註來源編號」這個可程式化檢查的規則。
    kept, dropped = [], []
    for ln in cleaned.splitlines():
        s = ln.strip()
        if not s:
            kept.append(ln)
            continue
        is_content = s.startswith(("-", "・", "•")) or s.startswith("【整體偏向】")
        if is_content and not re.search(r"#\s*\d+", s):
            dropped.append(s)
            continue
        kept.append(ln)
    cleaned = "\n".join(kept).strip()

    # ── 第三關：機械檢核，摘要中的數字必須出現在標題裡 ──
    src_nums = _numbers(" ".join(a["title"] for a in articles))
    out_nums = _numbers(cleaned)
    # 排除「#編號」引用與常見無害序數
    ghost = {n for n in out_nums - src_nums if n not in {"1", "2", "3", "225"}}

    return {"text": cleaned,
            "raw": raw,
            "changed": cleaned.strip() != raw.strip(),
            "verified": ok,
            "dropped": dropped,
            "ghost_numbers": sorted(ghost),
            "used": len(articles),
            "span": (articles[-1]["dt"], articles[0]["dt"]),
            "usage": d.get("usage", {})}


def build_message(result, articles, header="📰 日經新聞摘要"):
    """組成 Telegram HTML 訊息。result 為 None 時回 None。"""
    if not result:
        return None
    a, b = result["span"]
    lines = [f"<b>{header}</b>",
             f"<i>{a:%m/%d %H:%M} ～ {b:%m/%d %H:%M}（台北）　{result['used']} 則標題</i>",
             "", result["text"], "", "<b>來源標題</b>"]
    for i, x in enumerate(articles[:6]):
        src = f" <i>{x['source']}</i>" if x["source"] else ""
        lines.append(f"<code>#{i+1}</code> <a href=\"{x['url']}\">{x['title'][:75]}</a>{src}")
    lines += ["", "<i>每句後的 (#N) 對應上列編號，可自行查核</i>",
              "<i>由 DeepSeek 依標題摘要，未讀取內文；資訊工具，非投資建議</i>"]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="起始 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
    p.add_argument("--until", help="結束 同上")
    p.add_argument("--query", default=DEFAULT_QUERY)
    p.add_argument("--limit", type=int, default=40)
    args = p.parse_args()

    def parse(s):
        if not s:
            return None
        for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, f).replace(tzinfo=TW)
            except ValueError:
                continue
        raise SystemExit(f"無法解析時間：{s}")

    arts = fetch_news(args.query, parse(args.since), parse(args.until), args.limit)
    print(f"取得 {len(arts)} 則")
    for a in arts:
        print(f"  [{a['dt']:%m-%d %H:%M}] {a['title'][:80]}")
    if not arts:
        sys.exit(0)
    res = analyse(arts)
    if not res:
        print("\n分析失敗（未編造內容）")
        sys.exit(0)
    if res["changed"]:
        print("\n--- 查核前（原始輸出）---")
        print(res["raw"])
    print("\n" + "=" * 50)
    print(res["text"])
    print("=" * 50)
    print(f"二次查核執行：{res['verified']}　查核有修改：{res['changed']}")
    if res["dropped"]:
        print(f"程式刪除無來源編號的句子 {len(res['dropped'])} 句：")
        for s in res["dropped"]:
            print(f"   ✂ {s}")
    else:
        print("程式檢核：所有句子皆標註來源編號")
    if res["ghost_numbers"]:
        print(f"⚠ 摘要出現標題中沒有的數字：{res['ghost_numbers']}")
    else:
        print("機械檢核：摘要中的數字皆可在標題中找到")
    print("用量:", res["usage"])
