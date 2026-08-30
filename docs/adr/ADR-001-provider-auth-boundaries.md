# ADR-001：Provider 授權與執行模式邊界

> 狀態：Accepted（2026-08-30 隨 PRD-001／SPEC-001 核准）
> 日期：2026-08-16
> 範圍：Zhenhe AI V0、MoneyPrinterTurbo fork

## 決策

產品把 Provider 分成兩個互斥的執行模式：

- `assisted`：人員或互動式 Agent 使用現有登入／訂閱產生素材，系統只接收 manifest 與已驗證 Asset。
- `automated`：背景 Job 只使用正式 API／Vertex／ModelStudio credential，並受 Budget Guard、ProviderEvent 與 UsageLedger 控制。

Gemini CLI OAuth、Grok Build OAuth 與 Qwen Code 的互動式 Coding／Token Plan 憑證都屬於 `assisted`。它們不能被抽取成 MoneyPrinterTurbo backend 的通用 API Key，也不能被排程、n8n 或無人值守 Job 靜默重用。官方 ModelStudio Token Plan endpoint／credential 是獨立的 `automated` contract，不得與 Qwen Code session 混用。

## 優先序

| execution_mode | 優先序 |
|---|---|
| assisted | `gemini_cli`／`manual_google_flow` → `grok_build` → `qwen_code_plan` → `manual_import` |
| automated | `gemini_api`／`vertex_ai` → `xai_api` → `modelstudio_api`／`modelstudio_token_plan` → manual action |

Qwen OAuth 不作為新流程依賴；即使本機存在舊 cache，也必須視為未驗證。Qwen Code 的互動式 Token Plan 仍是 `assisted`；只有官方 ModelStudio Token Plan endpoint／credential 才能進入 `automated`，並須另行完成成本、條款與 smoke test，不能使用 Qwen Code 的互動式 session token。

Canonical alias：`gemini_veo_api` → `gemini_api`；`qwen_assisted` 只可讀取舊資料，寫入 manifest／event 前必須轉成 `qwen_code_plan`。

## 後果

MoneyPrinterTurbo 現有的 `gemini`、`grok`、`qwen` API adapters 可以保留作技術基線，但要由產品層 capability policy 決定是否可被選用。OAuth 登入成功本身不等於 API provider 可用；capability status 使用小寫 `ready`／`manual_reauth_required`／`manual_action_required`／`provider_unavailable`，selector decision status 使用 `ASSISTED_READY`／`AUTOMATED_READY`／`ASSISTED_ONLY`／`MANUAL_ACTION_REQUIRED`／`PROVIDER_UNAVAILABLE`。

## 驗證

每個候選至少要有：本機 credential／launcher discovery、最小 smoke request、錯誤分類、成本／request id 記錄，以及不洩漏 token 的測試輸出。未完成登入或 billing 的 provider 只能報告為 `manual_reauth_required` 或 `provider_unavailable`。
