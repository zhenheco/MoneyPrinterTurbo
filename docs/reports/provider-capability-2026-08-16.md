# Provider Capability Probe：2026-08-16

本報告只記錄本機 capability probe 的結果，不保存 email、access token、refresh token、API Key、OAuth URL 或 authorization code。

## 結果

| Provider | 本機狀態 | 執行模式 | 最小 smoke | 產品判定 |
|---|---|---|---|---|
| Gemini CLI | 0.47.0；OAuth code flow 已到達授權頁，code 已送回 CLI；CLI 回報「This client is no longer supported for Gemini Code Assist for individuals」 | Assisted | OAuth code flow → `This client is no longer supported for Gemini Code Assist for individuals` | `provider_unavailable`（原因：`client_deprecated`） |
| Grok Build | 1.0.0；`grok models` 可列出模型 | Assisted | `grok -p 'Reply with exactly: OK'` → `OK` | `assisted_ready` |
| Qwen Code／Token Plan CLI | 0.21.2；model `qwen3.8-max`；checked-in launcher `autoflow-qwen-token-plan.sh` 使用 Alibaba ModelStudio Token Plan endpoint `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | Assisted | `AUTOFLOW_QWEN_ACCOUNT=account-1`、`account-2` 各 1 次 non-interactive request，均 → `OK`；每次 1 API request、0 errors | `assisted_ready`（兩個帳號） |
| Qwen OAuth | 0.21.2；`--auth-type qwen-oauth` 的 non-interactive smoke 回報 Qwen OAuth free tier discontinued on 2026-04-15 | Assisted | `--auth-type qwen-oauth` → `Qwen OAuth free tier discontinued on 2026-04-15` | `provider_unavailable`（需改用 Coding Plan／其他 provider） |
| Gemini API／Vertex | 未配置正式 API／Vertex credential | Automated | 未執行 | `manual_action_required` |
| xAI API | 未配置 API key contract | Automated | 未執行 | `manual_action_required` |
| ModelStudio API／Token Plan endpoint（`modelstudio_token_plan`） | endpoint 已由 Qwen Code CLI Assisted smoke 使用；MoneyPrinterTurbo backend 的 Automated contract 尚未驗證 | Automated | 未執行 ProviderEvent／UsageLedger／cost／request-id contract smoke | `manual_action_required`（Automated candidate） |

## 判讀

1. Grok OAuth 只證明 Assisted 路徑可用，不代表 Grok OAuth token 可以交給 MoneyPrinterTurbo backend。
2. Gemini CLI OAuth code flow 曾到達授權頁並將 code 送回 CLI，但 CLI 回報「This client is no longer supported for Gemini Code Assist for individuals」，因此不是 Assisted-ready；產品判定為 `provider_unavailable`，原因為 `client_deprecated`。這不會轉成 backend API credential。
3. Qwen Code／Token Plan CLI 透過 checked-in launcher `autoflow-qwen-token-plan.sh`，使用 model `qwen3.8-max` 與 Alibaba ModelStudio Token Plan endpoint，對 `account-1`、`account-2` 各完成 1 次 non-interactive request，均回傳 `OK`，每次 1 API request、0 errors；launcher 隔離 `QWEN_HOME`，只載入明確選定的帳號認證資料，無 silent fallback。這只證明兩個帳號的 CLI Assisted 路徑可用。
4. Qwen Code 0.21.2 的另一個明確 non-interactive smoke 使用 `--auth-type qwen-oauth`，回報 `Qwen OAuth free tier discontinued on 2026-04-15`；Qwen OAuth 需改用 Coding Plan／其他 provider，這個 discontinued 結論不因 Token Plan CLI smoke 成功而改變。
5. `modelstudio_token_plan` 仍是 Automated candidate，但尚未執行 MoneyPrinterTurbo backend 的 ProviderEvent／UsageLedger／cost／request-id contract smoke，因此產品判定維持 `manual_action_required`；目前沒有任何 provider 通過 Automated 的正式追溯 contract，不能宣稱背景 Job 已可自動 fallback。
6. Antigravity 不列入本次已驗證能力，不能據此提高任何 provider 的 capability status。

## 驗證命令

- `/opt/homebrew/bin/gemini --version`
- `grok --version`
- `grok models`
- `grok -p 'Reply with exactly: OK'`
- `qwen --version`
- `qwen -p 'Reply with exactly: OK' --auth-type qwen-oauth --output-format json --safe-mode`
- `autoflow-qwen-token-plan.sh`：`AUTOFLOW_QWEN_ACCOUNT=account-1`、`account-2` 各執行一次 non-interactive smoke
