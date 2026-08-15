# Provider Capability Probe：2026-08-16

本報告只記錄本機 capability probe 的結果，不保存 email、access token、refresh token、API Key、OAuth URL 或 authorization code。

## 結果

| Provider | 本機狀態 | 執行模式 | 最小 smoke | 產品判定 |
|---|---|---|---|---|
| Gemini CLI | 0.47.0；既有 OAuth cache 已過期，登入頁待人工完成 | Assisted | 未執行 | `manual_reauth_required` |
| Grok Build | 1.0.0；`grok models` 可列出模型 | Assisted | `grok -p 'Reply with exactly: OK'` → `OK` | `assisted_ready` |
| Qwen Code | 0.21.2；`qwen auth` help 標示 removed | Assisted | 非互動執行失敗：未選定 auth type | Qwen OAuth `provider_unavailable` |
| Gemini API／Vertex | 未配置正式 API／Vertex credential | Automated | 未執行 | `manual_action_required` |
| xAI API | 未配置 API key contract | Automated | 未執行 | `manual_action_required` |
| ModelStudio API／Token Plan endpoint | 未配置官方 endpoint／credential | Automated | 未執行 | `manual_action_required` |

## 判讀

1. Grok OAuth 只證明 Assisted 路徑可用，不代表 Grok OAuth token 可以交給 MoneyPrinterTurbo backend。
2. Gemini CLI OAuth 必須由人工在已開啟的 Google 登入頁完成；即使成功，也只會產生 Assisted capability，不會轉成 backend API credential。
3. Qwen Code 的互動式 OAuth／Token Plan 不列入新流程依賴；若要測 Automated，必須另備官方 ModelStudio endpoint／credential。
4. 目前沒有任何 provider 通過 Automated 的正式 credential、cost、request id 與使用量追溯 contract；因此不能宣稱背景 Job 已可自動 fallback。

## 驗證命令

- `/opt/homebrew/bin/gemini --version`
- `grok --version`
- `grok models`
- `grok -p 'Reply with exactly: OK'`
- `qwen --version`
- `qwen -p 'Reply with exactly: OK' --output-format json --safe-mode`
