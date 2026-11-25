# agy-metrics

零外部依賴（僅使用 Python 3 標準函式庫）的 Antigravity CLI (`agy`) 與 Remote Control 本地遙測後端 + 油猴內嵌監控插件。

## 功能特色

- **自動啟停**：透過 `SessionStart` Hook 在啟動 `agy` 時靜默拉起背景後端服務（`http://127.0.0.1:47891`）；當所有 `agy.exe` 行程結束後約 1 秒自動優雅關閉。
- **Remote Control 內嵌上下文監控與一鍵壓縮（油猴腳本）**：內建 `agy-metrics-overlay.user.js`，直接嵌在 `antigravity.google.com` 對話框模型選擇器右側，以空心圓環呈現上下文比例；點擊展開完整統計（已用上下文＋二段確認「壓縮」按鈕、上下文上限、上次請求、快取命中率）與單一可滾動輪次明細表（附固定底端加總列與 `X 輪對話`）。點擊壓縮後由後端背景執行 `agy -r <cid> -p "/compact"`，若使用 `agy remote-control` 常駐模式會自動重啟 daemon 並刷新頁面同步壓縮結果。
- **5 小時模型額度扣減追蹤**：直接讀取本機 OAuth 憑證呼叫 `v1internal` 官方配額介面（支援純 Remote Control），依對話模型自動對應 `gemini-5h` 或 `3p-5h`，以事件驅動紀錄 7 位小數精度的每輪額度扣減與本對話累計消耗；遇到 Token 過期（HTTP 401）時會自動於背景執行 `agy models` 透過 `refresh_token` 無感換發新 Token 並重試。
- **跨裝置區網／手機支援**：油猴腳本自動偵測 `127.0.0.1`，若未連線（例如手機瀏覽器）會自動顯示主機 IP 輸入框並儲存於油猴空間；對話框底部工具列支援橫向滑動，面板高度自動貼合標題欄不遮擋。

## 安裝與使用

```powershell
# 從 GitHub 倉庫安裝
agy plugin install https://github.com/Artin0123/harness-setup/tree/master/agy/agy-metrics
```

啟動 `agy` 後：
- **安裝油猴腳本**：開啟 [GitHub Raw 油猴腳本](https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/agy-metrics/agy-metrics-overlay.user.js) 直接安裝到 Tampermonkey（內建 `@updateURL` / `@downloadURL`，後續可一鍵檢查更新），之後開啟 Remote Control 頁面即可自動連線顯示。
- **區網／手機連線**：啟動指令加上 `--lan` 改綁 `0.0.0.0:47891`（`POST /api/shutdown` 僅限本機 loopback 呼叫以確保安全）。

### 選配：VS Code 快捷鍵一鍵啟動（無閃窗背景 Remote Control 常駐 + `--lan`）

```json
"args": {
  "text": "python \"$env:USERPROFILE\\.gemini\\config\\plugins\\agy-metrics\\hooks\\ensure_server.py\" --lan --daemon; Start-Process 'https://antigravity.google.com/remote?path=%2Fview%2Fremote-control'\r"
}
```

> 加上 `--daemon` 會以 Windows `CREATE_NO_WINDOW`（隱藏主控台）直接背景啟動 `agy remote-control serve`，避免開網頁時 `agy.exe` 呼叫 `git.exe` 狂閃 Windows Terminal，也不會寫入開機登錄檔或觸發 `schtasks` 權限警告。關閉時執行 `python "$env:USERPROFILE\.gemini\config\plugins\agy-metrics\hooks\ensure_server.py" --stop` 即可一併停掉兩者。

### 手動優雅關閉背景服務

```powershell
irm -Method Post http://127.0.0.1:47891/api/shutdown
```

### 執行單元測試

```powershell
python -m unittest discover -s .\agy\agy-metrics -p "test_*.py" -v
```
