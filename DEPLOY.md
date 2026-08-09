# 24/7 免費部署

兩個版本，用途不同：

| 檔案 | 用途 | 執行方式 |
|---|---|---|
| `bot.py` | 本機常駐 | 無限迴圈，30 秒檢查一次，精準 |
| `bot_cron.py` | 雲端排程 | 單次執行即結束，無狀態 |

**`bot.py` 放不上 GitHub Actions** —— Actions 每個 job 最長 6 小時且會被砍掉，
無限迴圈跑不了。雲端一律用 `bot_cron.py`。

---

## 方案 A：GitHub Actions（推薦先試）

### 為什麼要用公開 repo

| | 私有 repo | 公開 repo |
|---|---|---|
| Actions 免費額度 | 2000 分鐘/月 | **無上限** |
| 每 5 分鐘跑一次需要 | 每月約 8640 分鐘 | — |
| 結論 | **額度不夠** | ✅ 可行 |

GitHub 對每次 job 以「分鐘」為單位計費（跑 20 秒也算 1 分鐘），
所以私有 repo 撐不到三分之一個月。

**公開 repo 安全嗎？** 安全 —— token 存在 GitHub Secrets，
不會出現在程式碼或執行紀錄裡。但**絕對不要把 token 寫進 config.json 後 commit**。

### 步驟

1. 建一個**公開** repo，把這個資料夾的內容全部推上去
   （`config.json` 和 `state.json` 不要推，已列在 `.gitignore`）

2. repo → **Settings → Secrets and variables → Actions → New repository secret**

   | Name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | 你的 bot token |
   | `TELEGRAM_CHAT_ID` | 你的 chat id |
   | `NEWS_API_KEY` | （選用）newsapi.org key |

3. 同一頁的 **Variables** 分頁可調參數（都有預設值，不設也能跑）：
   `SYMBOL` / `IS_DST` / `PRE_ALERT_MIN` / `REACTION_MIN` / `DAILY_DIGEST_AT` / `ENABLE_NEWS`

   > **冬令時記得把 `IS_DST` 設成 `false`**（11 月初～3 月中）

4. Actions 分頁 → 選「JP225 事件推播」→ **Run workflow** 手動測一次

### ⚠️ GitHub Actions 的三個真實限制

1. **排程會延遲。** 免費層 cron 常慢 5～15 分鐘，尖峰更久。
   所以「事件前 10 分鐘提醒」實際可能變成事件前 5 分鐘、或事件後才到。
   **這是 GitHub 的已知行為，不是設定問題，改不掉。**

2. **repo 60 天沒有 commit，排程會自動停用。** GitHub 會寄信通知，
   但你得手動去 Actions 頁面重新啟用。想避免就每兩個月隨便 commit 一次。

3. **偶爾會漏跑。** GitHub 不保證 cron 一定執行。

如果你需要「準時到分鐘」的提醒，Actions 不適合，看方案 B 或 C。

---

## 方案 B：Oracle Cloud Always Free（最穩，設定較麻煩）

Oracle 提供**永久免費**的 ARM 虛擬機（4 核 / 24GB RAM），真正 24/7 不休眠。

1. 註冊 Oracle Cloud，開一台 Ampere A1 的 Ubuntu 機器（Always Free 方案）
2. SSH 進去：
   ```bash
   sudo apt update && sudo apt install -y python3-pip
   git clone <你的 repo>
   cd jp225_bot && pip3 install -r requirements.txt
   ```
3. 建 systemd 服務讓它常駐：
   ```ini
   # /etc/systemd/system/jp225.service
   [Unit]
   Description=JP225 Telegram Bot
   After=network.target

   [Service]
   WorkingDirectory=/home/ubuntu/jp225_bot
   ExecStart=/usr/bin/python3 /home/ubuntu/jp225_bot/bot.py
   Restart=always
   RestartSec=30

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl enable --now jp225
   ```

**用 `bot.py`（迴圈版），30 秒檢查一次，提醒精準到分鐘。**

註冊時要綁信用卡驗證身分，但 Always Free 資源不會扣款。

---

## 方案 C：你自己的電腦（最簡單）

如果電腦本來就常開著，這是零成本零設定的選項。

Windows 工作排程器 → 建立工作：
- 觸發程序：登入時
- 動作：`pythonw.exe`，引數 `bot.py` 的完整路徑
- 起始位置：`jp225_bot` 資料夾

`pythonw.exe` 不會跳出黑視窗。電腦關機或睡眠就會停。

---

## 我的建議

**先用方案 A 跑一週**，看延遲能不能接受。

「事件前 10 分鐘提醒」延遲 10 分鐘就變成事件當下才通知，
如果你覺得這樣沒用，再花時間弄方案 B。

方案 C 適合你電腦本來就開整天的情況 —— 精準度和 B 一樣，
但關機就沒了。
