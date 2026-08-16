# Provider Capability Probe：2026-08-16

本報告只記錄本機 capability probe 的結果，不保存 email、access token、refresh token、API Key、OAuth URL 或 authorization code。

## 結果

| Provider | 本機狀態 | 執行模式 | 最小 smoke | 產品判定 |
|---|---|---|---|---|
| Gemini CLI | 0.47.0；OAuth code flow 已到達授權頁，code 已送回 CLI；CLI 回報「This client is no longer supported for Gemini Code Assist for individuals」 | Assisted | OAuth code flow → `This client is no longer supported for Gemini Code Assist for individuals` | `provider_unavailable`（原因：`client_deprecated`） |
| Grok Build | 1.0.0；`grok models` 可列出模型 | Assisted | `grok -p 'Reply with exactly: OK'` → `OK` | `assisted_ready` |
| Qwen Code | 0.21.2；`--auth-type qwen-oauth` 的 non-interactive smoke 回報 Qwen OAuth free tier discontinued on 2026-04-15 | Assisted | `--auth-type qwen-oauth` → `Qwen OAuth free tier discontinued on 2026-04-15` | Qwen OAuth `provider_unavailable`（需改用 Coding Plan／其他 provider） |
| Gemini API／Vertex | 未配置正式 API／Vertex credential | Automated | 未執行 | `manual_action_required` |
| xAI API | 未配置 API key contract | Automated | 未執行 | `manual_action_required` |
| ModelStudio API／Token Plan endpoint | 未配置官方 endpoint／credential；仍待配置與驗證的 Automated candidate | Automated | 未執行 | `manual_action_required` |

## 判讀

1. Grok OAuth 只證明 Assisted 路徑可用，不代表 Grok OAuth token 可以交給 MoneyPrinterTurbo backend。
2. Gemini CLI OAuth code flow 曾到達授權頁並將 code 送回 CLI，但 CLI 回報「This client is no longer supported for Gemini Code Assist for individuals」，因此不是 Assisted-ready；產品判定為 `provider_unavailable`，原因為 `client_deprecated`。這不會轉成 backend API credential。
3. Qwen Code 0.21.2 的明確 non-interactive smoke 使用 `--auth-type qwen-oauth`，回報 `Qwen OAuth free tier discontinued on 2026-04-15`；Qwen OAuth 需改用 Coding Plan／其他 provider。Token Plan 僅保留為尚待配置與驗證的 ModelStudio Automated candidate，不代表已通過。
4. 目前沒有任何 provider 通過 Automated 的正式 credential、cost、request id 與使用量追溯 contract；因此不能宣稱背景 Job 已可自動 fallback。
5. Antigravity 不列入本次已驗證能力，不能據此提高任何 provider 的 capability status。

## 驗證命令

- `/opt/homebrew/bin/gemini --version`
- `grok --version`
- `grok models`
- `grok -p 'Reply with exactly: OK'`
- `qwen --version`
- `qwen -p 'Reply with exactly: OK' --auth-type qwen-oauth --output-format json --safe-mode`
