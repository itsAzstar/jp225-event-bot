# 部署現況

**Repo**：https://github.com/itsAzstar/jp225-event-bot （公開）
**排程**：每 5 分鐘（GitHub Actions）
**首次手動執行**：✅ 成功，12 秒
**Token**：已存入 GitHub Secrets，執行紀錄中確認被遮蔽（出現 0 次）

---

## ⚠️ 還差最後一步 —— 只有你能做

**對 bot 發一則訊息。**

1. Telegram 開啟 **@azstartest_bot**
2. 送出 `/start`（或任何字）

下一次排程（5 分鐘內）會自動偵測到你的 chat_id、寫回 repo、
並發一則「已連線」訊息給你。之後就永久生效，不需再設定。

**在你做這件事之前，bot 不會發任何訊息** —— 它拿不到收件人。
目前每次執行都會印「尚無 chat_id，本次不發送」。

---

## ⚠️ 兩件必須處理的事

### 1. Token 已曝光，請立刻更換

這組 token 出現在對話紀錄裡。任何人拿到都能冒充你的 bot。

確認能收到訊息後：
1. @BotFather → `/revoke` → 選 azstartest_bot → 取得新 token
2. GitHub repo → Settings → Secrets → 更新 `TELEGRAM_BOT_TOKEN`

repo 裡沒有任何檔案含 token（已掃描確認），但曝光在對話中就該換。

### 2. Bot 名稱是種族歧視字眼

`getMe` 顯示 `first_name` 是一個種族蔑稱。

@BotFather → `/setname` → 改掉。若要對外賣，這個名字會直接毀掉產品。

---

## 為什麼用公開 repo

| | 私有 | 公開 |
|---|---|---|
| Actions 免費額度 | 2000 分鐘/月 | 無上限 |
| 每 5 分鐘跑一次需要 | ~8640 分鐘/月 | — |
| 壓低頻率塞進額度 | 誤差 ±15 分鐘，提醒失效 | — |

Token 在 Secrets，不在程式碼裡，公開是安全的。
這 300 行膠水程式碼本身沒有商業價值 —— 真正的研究結論在你本機檔案，沒進 repo。

要改私有：`gh repo edit itsAzstar/jp225-event-bot --visibility private`
但同時得把 cron 頻率降到每 30 分鐘以上，否則額度爆掉會直接停跑。

---

## GitHub Actions 的三個真實限制

1. **排程延遲 5～15 分鐘**（免費層已知行為，改不掉）。
   「事件前 10 分鐘提醒」實際可能變成事件當下才到。
2. **repo 60 天無 commit 會自動停用排程**，需手動重啟。
3. **偶爾漏跑**，GitHub 不保證 cron 必定執行。

需要準時到分鐘 → 看 `DEPLOY.md` 的方案 B（Oracle Cloud 永久免費 VM）。

---

## 自我檢查清單

- [x] Token 有效（getMe 200）
- [x] Token 存入 Secrets
- [x] 執行紀錄中 token 被遮蔽（0 次出現）
- [x] repo 內無任何檔案含 token
- [x] workflow YAML 語法正確
- [x] 手動觸發成功
- [x] chat_id 自動偵測邏輯正確（無資料時安全跳過）
- [x] 取價功能實測正確（^N225 = 65,606.71，與 TradingView 一致）
- [x] 過期報價保護（超過 20 分鐘容許值回 None，不會拿兩天前的價格當「市場反應」）
- [ ] **使用者對 bot 發訊息** ← 只有你能做
- [ ] **更換 token**
- [ ] **改 bot 名稱**
