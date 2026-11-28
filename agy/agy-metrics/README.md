# agy-metrics

零外部依賴（僅使用 Python 3 標準函式庫）的 Antigravity CLI (`agy`) 與 Remote Control 本地遙測後端 + 油猴內嵌監控插件。

## 功能特色

- **自動啟停**：透過 `SessionStart` Hook 在啟動 `agy` 時靜默拉起背景後端服務（`http://127.0.0.1:47891`）；當所有 `agy.exe` 行程結束後約 1 秒自動優雅關閉。
- **Remote Control 內嵌上下文監控（油猴腳本）**：內建 `agy-metrics-overlay.user.js`，直接嵌在 `antigravity.google.com` 對話框模型選擇器右側，以空心圓環呈現上下文比例；點擊展開 2×3 完整統計（模型名稱、帳號、已用上下文、上次請求、上下文上限、快取命中率；其中「帳號」直接唯讀 `agy` 原生 `log/cli-*.log` 對應最新一次請求的帳號，零額外儲存）與單一可滾動輪次明細表（附固定底端加總列與 `X 輪對話`）。
- **5 小時模型額度扣減追蹤**：對齊 `agy` 的 `ChainedAuth` 順序讀取本機 OAuth 憑證呼叫 `v1internal` 官方配額介面（支援純 Remote Control 與 SSH／桌面雙憑證消歧），依對話模型自動對應 `gemini-5h` 或 `3p-5h`，以事件驅動紀錄 7 位小數精度的每輪額度扣減與本對話累計消耗；切換帳號時自動於記憶體重設配額基準線以避免跨帳號假扣減，遇到 Token 過期（HTTP 401）時自動於背景執行 `agy models` 透過 `refresh_token` 無感換發新 Token 並重試。
- **跨裝置區網／手機支援**：油猴腳本自動偵測 `127.0.0.1`，若未連線（例如手機瀏覽器）會自動顯示主機 IP 輸入框並儲存於油猴空間；對話框底部工具列支援橫向滑動，面板高度自動貼合標題欄不遮擋。

## 安裝與使用

```powershell
# 從 GitHub 倉庫安裝
agy plugin install https://github.com/Artin0123/harness-setup/tree/master/agy/agy-metrics
```

啟動 `agy` 後：
- **安裝油猴腳本**：開啟 [GitHub Raw 油猴腳本](https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/agy-metrics/agy-metrics-overlay.user.js) 直接安裝到 Tampermonkey（內建 `@updateURL` / `@downloadURL`，後續可一鍵檢查更新），之後開啟 Remote Control 頁面即可自動連線顯示。
- **區網／手機連線**：啟動指令加上 `--lan` 改綁 `0.0.0.0:47891`（`POST /api/shutdown` 僅限本機 loopback 呼叫以確保安全）。

### 選配：VS Code 快捷鍵一鍵啟動（自動接續最新有效專案對話 + 就緒後直達網頁 + `--lan`）

先透過一行指令安裝 `latest_cid.py` 到 `~\.gemini\config\scripts\latest_cid.py`（`latest_cid.py` 與 `ensure_server.py` 皆優先挑選所屬 `~\.gemini\config\projects\<project_id>.json` 存在的最新頂層對話，自動跳過 `outside-of-project` 與已刪專案以確保兩端同步且不跳 `Invalid Project`；若皆無有效專案則統一退回最新一筆頂層對話。純 CLI 亦可單獨執行 `agy -r (python "$env:USERPROFILE\.gemini\config\scripts\latest_cid.py")` 用於 `/compact` 等操作）：

```powershell
irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/install-scripts.ps1 | iex
```

再於 VS Code `keybindings.json` 加入：

```json
"args": {
  "text": "python \"$env:USERPROFILE\\.gemini\\config\\plugins\\agy-metrics\\hooks\\ensure_server.py\" --lan --open-remote; $cid = python \"$env:USERPROFILE\\.gemini\\config\\scripts\\latest_cid.py\"; if ($cid) { agy -r $cid --remote-control } else { agy --remote-control }\r"
}
```

（若資料庫完全沒有任何對話，`--open-remote` 會自動退回 `fixed` 模式開啟固定入口頁；若原本就只需固定入口也可直接傳 `--open-remote fixed`。腳本會優先從剛啟動的 `cli-*.log` 擷取實際登入帳號 Email 並帶入 `authuser=<email>`，找不到時再依 `ChainedAuth` 順序退回讀取本機憑證；`agy_metrics_quota.db` 與 `ensure_server.log` 皆統一存放在 `~\.gemini\antigravity-cli\plugin_data\agy-metrics\`。）

### 手動優雅關閉背景服務

```powershell
irm -Method Post http://127.0.0.1:47891/api/shutdown
```

### 執行單元測試

```powershell
python -m unittest discover -s .\agy\agy-metrics -p "test_*.py" -v
```
