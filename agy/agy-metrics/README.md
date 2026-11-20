# agy-metrics

零外部依賴（僅使用 Python 3 標準函式庫）的 Antigravity CLI (`agy`) 與 Remote Control 本地遙測儀表板插件。

## 功能特色

- **自動啟停**：透過 `SessionStart` Hook 在啟動 `agy` 時靜默拉起背景網頁服務（`http://127.0.0.1:47891`）；當所有 `agy.exe` 行程結束後約 1 秒自動優雅關閉。
- **即時推送（SSE）**：監聽本地 `conversations/*.db` 與 `conversation_summaries.db` 異動，無需輪詢即可即時刷新。
- **對話清單與搜尋**：自動過濾已刪除對話，支援依標題、ID 前綴與專案標籤（具名專案資料夾名稱、`CLI Project`）即時搜尋與篩選。
- **四核心指標卡片（鍵值兩端對齊）**：上下文（已用／上限與當前調用模型）、總輸入（快取／未快取／命中率）、總輸出（思考／回覆）、上次請求與總請求次數。
- **依使用者輪次彙總表格**：將單輪內多次工具呼叫請求合併統計，顯示 `TTFT / 總耗時`、純串流速度（`tok/s`）、`輸入 (快取 + 未快取)`、`輸出 (思考 + 回覆)` 與 `執行時間`（`MM/DD HH:mm`）。
- **PWA 與響應式排版**：支援安裝為獨立桌面／手機 PWA 視窗，窄螢幕自動切換為漢堡側邊欄與水平可捲動表格。

## 安裝與使用

```powershell
# 從 GitHub 倉庫安裝
agy plugin install https://github.com/Artin0123/harness-setup/tree/master/agy/agy-metrics
```

啟動 `agy` 後，直接於瀏覽器開啟 **`http://127.0.0.1:47891`** 即可查看；若需讓同區網裝置連線，可於啟動指令加上 `--lan` 改綁 `0.0.0.0:47891`。

### 選配：VS Code 快捷鍵一鍵啟動並開啟儀表板

```json
"args": {
  "text": "python \"$env:USERPROFILE\\.gemini\\config\\plugins\\agy-metrics\\hooks\\ensure_server.py\"; Start-Process http://127.0.0.1:47891; agy -c --remote-control\r"
}
```

### 手動優雅關閉背景服務

- 於終端機執行：
  ```powershell
  irm -Method Post http://127.0.0.1:47891/api/shutdown
  ```
