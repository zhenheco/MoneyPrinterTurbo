# SPEC-001：MoneyPrinterTurbo V0 本機 Vertical Slice

> 文件狀態：Approved，隨 PRD-001 於 2026-08-30 一併核准
> 文件版本：0.2
> 目標 Repo：zhenheco/MoneyPrinterTurbo
> 目標分支：main
> 建立日期：2026-08-16

## 1. 範圍與不變條件

本 SPEC 定義如何把 Zhenhe AI 的 AI 短影音需求映射到 MoneyPrinterTurbo 現有的 Python／WebUI／API／CLI／MoviePy／FFmpeg 基線。此階段只定義契約、資料、狀態、驗收與實作順序；PRD 未核准前不得建立正式功能或修改既有業務程式（此約束於 2026-08-30 核准前成立）。

V0 執行邊界：

- 本機 Python 3.11+。
- 主要渲染使用既有 MoviePy／FFmpeg。
- 互動入口使用現有 WebUI、API 或 CLI；不新增大型 Framework。
- 生成式影片使用人工匯入或受控 Provider adapter。
- 發布只建立 Postiz Draft。
- 正式 Cloudflare Runtime 留到 V1。
- Qwen Code 的互動式 Coding／Token Plan 權益只能在人工 Assisted／Manual 流程使用，不能被背景 Job 或服務端當 API Key；只有官方 ModelStudio Token Plan endpoint／credential 通過 contract 後，才能列入 Automated 候選。

## 2. 基線與實作對應

| V0 能力 | MoneyPrinterTurbo 基線 | 本 SPEC 調整 |
|---|---|---|
| 腳本與模型 | 既有 AI Agent／API／WebUI 與多 Provider | 輸出固定 Script JSON，不接受純文字作唯一結果 |
| Scene／素材 | 既有影片與本地／外部素材流程 | 增加 scene_id、manifest、Asset Record 與匯入驗證 |
| Creator Profile | 新增產品層 profile reference | 只保存明確同意的 voice／avatar asset reference 與 consent metadata |
| 語音 | 既有 Edge TTS、Azure、Gemini 等選項 | 一般 Scene 預設一次產生 Master Voice；`native_speech_avatar` Scene 必須保留 provider 原生音訊，不得覆蓋 |
| 字幕 | 既有 edge／whisper 路徑 | 字幕由後製層產生，不把文字交給生成影像或影片模型 |
| 渲染 | MoviePy／FFmpeg | 固定 1080×1920、H.264、AAC，輸出前執行 ffprobe |
| 發布 | 上游已有跨平台上傳能力 | V0 以 Postiz Draft adapter 取代直接公開上傳 |
| 成本 | 依設定呼叫外部服務 | 每次呼叫先過 Budget Guard 並寫 Usage Ledger |
| 長流程 | 目前以本機互動流程為主 | V0 使用可恢復 Job；Cloudflare Workflows 留到 V1 |

## 3. 輸入與輸出

### 3.1 V0 輸入

~~~json
{
  "tenant_id": "zhenhe",
  "brand_id": "zhenhe-ai",
  "topic": "企業導入AI最常犯的三個錯誤",
  "target_duration_sec": 50,
  "language": "zh-TW",
  "image_mode": "assisted_qwen",
  "video_mode": "manual_google_flow",
  "max_generated_video_scenes": 3,
  "publish_mode": "postiz_draft",
  "budget_limit_usd": 3
}
~~~

### 3.2 輸出物

~~~text
job/
├── input/request.json
├── profile/creator-profile.json
├── research/research.json
├── scripts/script.json
├── scenes/{scene_id}/
│   ├── scene.json
│   ├── references/
│   ├── images/
│   ├── videos/
│   └── qa/
├── audio/
│   ├── master-voice.*
│   └── master-voice-timestamps.json
├── subtitles/
│   ├── captions.srt
│   └── captions.json
├── renders/
│   ├── render-manifest.json
│   ├── final.mp4
│   └── ffprobe.json
├── publish/postiz-draft.json
└── audit/
    ├── provider-events.jsonl
    ├── usage-ledger.jsonl
    └── decisions.jsonl
~~~

路徑可因現有 storage helper 調整，但所有 Record 必須保留 tenant_id、brand_id、content_job_id 與 scene_id。不得以檔名單獨推論狀態。

## 4. 核心資料契約

### 4.1 Creator Profile

Creator Profile 只保存 asset reference 與 consent metadata，不保存 biometric material、secret 或 provider credential。voice／avatar reference 必須分別明確同意，且在使用前通過人工審核：

~~~json
{
  "creator_profile_id": "creator-001",
  "tenant_id": "zhenhe",
  "brand_id": "zhenhe-ai",
  "voice": {
    "asset_ref": "asset-voice-001",
    "consent_status": "explicit_granted",
    "usage_scope": "zhenhe-ai V0 short videos",
    "source": "user_recording",
    "expires_at": "",
    "revoked_at": null,
    "manual_review_status": "approved"
  },
  "avatar": {
    "asset_ref": "asset-avatar-001",
    "consent_status": "explicit_granted",
    "usage_scope": "zhenhe-ai V0 short videos",
    "source": "user_provided_still",
    "expires_at": "",
    "revoked_at": null,
    "manual_review_status": "approved"
  }
}
~~~

voice 的 `asset_ref` 必須指向唯一的 Master Voice asset；avatar 的 `asset_ref` 必須指向 scene asset。expired、revoked、未明確同意或未通過 manual review 的 reference 不得進入 render。

### 4.2 ContentJob

~~~json
{
  "content_job_id": "job-20260816-001",
  "tenant_id": "zhenhe",
  "brand_id": "zhenhe-ai",
  "creator_profile_id": "creator-001",
  "topic": "企業導入AI最常犯的三個錯誤",
  "language": "zh-TW",
  "target_duration_sec": 50,
  "image_mode": "assisted_qwen",
  "video_mode": "manual_google_flow",
  "max_generated_video_scenes": 3,
  "publish_mode": "postiz_draft",
  "budget_limit_usd": 3,
  "estimated_cost_usd": 0,
  "actual_cost_usd": 0,
  "status": "DRAFT",
  "created_at": "",
  "updated_at": ""
}
~~~

### 4.3 Script

~~~json
{
  "title": "",
  "target_audience": "",
  "core_message": "",
  "hook": "",
  "body": [],
  "conclusion": "",
  "cta": "",
  "claims": [],
  "sources": [],
  "risk_flags": []
}
~~~

### 4.4 Scene

~~~json
{
  "scene_id": "scene-001",
  "content_job_id": "job-20260816-001",
  "scene_index": 1,
  "semantic_purpose": "hook",
  "narration": "",
  "caption": "",
  "duration_target_ms": 5000,
  "visual_type": "generated_image",
  "visual_prompt": "",
  "reference_assets": [],
  "generation_required": true,
  "provider": "qwen_code_plan",
  "provider_model": "",
  "fallback_type": "image_motion",
  "attempt_count": 0,
  "status": "AWAITING_ASSETS"
}
~~~

允許的 visual_type：avatar、generated_video、generated_image、screen_recording、motion_graphic、title_card。

V0 的 `avatar` 只代表人工匯入的 user-provided still／image-motion 或預先渲染 talking-head／avatar MP4，不授權直接呼叫 Provider；`generated_video` 仍最多 3 個，匯入 avatar video 不得擴大此限制。

### 4.5 Asset Record

~~~json
{
  "asset_id": "asset-001",
  "content_job_id": "job-20260816-001",
  "scene_id": "scene-001",
  "asset_type": "image",
  "storage_key": "",
  "original_filename": "",
  "mime_type": "image/png",
  "bytes": 0,
  "width": 0,
  "height": 0,
  "duration_ms": null,
  "sha256": "",
  "source_mode": "assisted_qwen",
  "provider": "qwen_code_plan",
  "model": "",
  "license_or_consent": "",
  "consent_status": "not_applicable",
  "usage_scope": "",
  "consent_source": "",
  "consent_expires_at": "",
  "consent_revoked_at": null,
  "manual_review_status": "pending",
  "created_at": ""
}
~~~

### 4.6 Provider Event 與 Usage Ledger

~~~json
{
  "provider_event_id": "provider-event-001",
  "content_job_id": "job-20260816-001",
  "scene_id": "scene-001",
  "provider": "manual_google_flow",
  "model": "",
  "request_id": "",
  "external_job_id": "",
  "idempotency_key": "job-20260816-001:scene-001:video:attempt-1",
  "attempt_count": 1,
  "estimated_cost_usd": 0,
  "actual_cost_usd": 0,
  "request_summary": "",
  "response_summary": "",
  "error_class": null,
  "retryable": false,
  "created_at": "",
  "completed_at": ""
}
~~~

完整憑證、API Key、Authorization header 與敏感回應不得寫入摘要欄位。

上面是 Provider Event。Usage Ledger 是另一份契約：每次呼叫一筆，只留計費相關欄位，不含 request／response 摘要與錯誤欄位，並額外保存 §10 要求的估算來源、被放棄素材、採用秒數與每秒有效成本。

~~~json
{
  "provider_event_id": "provider-event-001",
  "content_job_id": "job-20260816-001",
  "scene_id": "scene-001",
  "provider": "manual_google_flow",
  "model": "",
  "idempotency_key": "job-20260816-001:scene-001:video:attempt-2",
  "attempt_count": 2,
  "estimated_cost_usd": 0.9,
  "actual_cost_usd": 0.9,
  "created_at": "2026-08-16T09:00:00+00:00",
  "estimated_cost_source": "rate card 2026-08, 0.15 USD/sec",
  "discarded_asset_ids": ["asset-011", "asset-012"],
  "adopted_video_seconds": 6.0,
  "effective_cost_per_adopted_second_usd": 0.15
}
~~~

上面 14 個欄位是 Usage Ledger 的恆定形狀：每一筆都必須寫滿，沒有該資訊的欄位寫其預設值而不是省略。省略會讓讀取端的 `payload.get(k, 0)` 得到 §10 禁止的偽零，也讓「確實採用 0 秒」與「沒有記錄」無法區分。`actual_cost_usd` 為 `"unknown"` 時 `estimated_cost_source` 必填，且 `effective_cost_per_adopted_second_usd` 一併為 `"unknown"`，不得寫 0；`adopted_video_seconds` 為 0 時同樣寫 `"unknown"`。重試成本以「一次嘗試一筆」表達：attempt-1 與 attempt-2 是兩個 idempotency key、兩筆帳。

每寫入一筆 ledger，`ContentJob.actual_cost_usd` 必須同步更新為 ledger 的累計金額——那正是 §10 閘門讀取的欄位。未回寫則閘門永遠拿初始值比對，一串個別看來付得起的呼叫可以無上限地花下去。累計時 `actual_cost_usd` 為 `"unknown"` 的那筆以其 `estimated_cost_usd` 計入，不得計為 0。

## 5. Job 狀態機

### 5.1 狀態

~~~text
DRAFT
RESEARCHING
SCRIPTING
SCENE_PLANNING
VOICE_GENERATING
AWAITING_ASSETS
IMAGE_GENERATING
VIDEO_GENERATING
READY_TO_RENDER
RENDERING
TECHNICAL_QA
CONTENT_QA
READY_FOR_REVIEW
POSTIZ_DRAFTING
POSTIZ_DRAFTED
APPROVED
SCHEDULED
PUBLISHED
RETRYABLE_FAILED
MANUAL_ACTION_REQUIRED
BUDGET_EXCEEDED
FAILED
CANCELLED
~~~

### 5.2 V0 主要轉移

| From | To | 條件 |
|---|---|---|
| DRAFT | SCRIPTING | 輸入與 Brand Profile 通過驗證 |
| SCRIPTING | SCENE_PLANNING | Script schema 通過 |
| SCENE_PLANNING | VOICE_GENERATING | Scene 數量與 duration 合計通過 |
| VOICE_GENERATING | AWAITING_ASSETS | Master Voice 與時間軸已建立，等待人工素材 |
| AWAITING_ASSETS | READY_TO_RENDER | 必要素材通過驗證，或已有 fallback |
| READY_TO_RENDER | RENDERING | Render Manifest 通過且預算閘門通過 |
| RENDERING | TECHNICAL_QA | final.mp4 與 probe output 產生 |
| TECHNICAL_QA | CONTENT_QA | codec、尺寸、音訊、字幕通過 |
| CONTENT_QA | READY_FOR_REVIEW | 內容檢查與風險標記完成 |
| READY_FOR_REVIEW | POSTIZ_DRAFTING | 人工要求建立 Draft |
| POSTIZ_DRAFTING | POSTIZ_DRAFTED | Postiz 回傳 draft id |
| 任一可重試階段 | RETRYABLE_FAILED | retryable error 且未超過上限 |
| 任一階段 | MANUAL_ACTION_REQUIRED | 缺少人工素材、登入或決策 |
| 任一生成階段 | BUDGET_EXCEEDED | 預估成本超出 Job budget |
| 任一未完成階段 | CANCELLED | 使用者主動取消 |
| RETRYABLE_FAILED | 該次失敗的可恢復階段 | 重試上限未超過，且返回目標可由 `decisions.jsonl` 推導 |
| RETRYABLE_FAILED | FAILED | Job 層級重試耗盡 |
| MANUAL_ACTION_REQUIRED | 該次中斷的可恢復階段 | 人工處理完成，且返回目標可由 `decisions.jsonl` 推導 |

V0 不應直接從任何狀態進入 PUBLISHED。APPROVED、SCHEDULED 與 PUBLISHED 只保留給後續受控發布流程。

上表用類別名稱指涉多個狀態，此處逐一列出，讀者不需離開本 SPEC 就能重建整張表：

- 任一可重試階段（8）：RESEARCHING、SCRIPTING、SCENE_PLANNING、VOICE_GENERATING、IMAGE_GENERATING、VIDEO_GENERATING、RENDERING、POSTIZ_DRAFTING。
- 任一生成階段（7）：RESEARCHING、SCRIPTING、SCENE_PLANNING、VOICE_GENERATING、IMAGE_GENERATING、VIDEO_GENERATING、READY_TO_RENDER。READY_TO_RENDER 本身不生成，但它的 §5.2 條件含「預算閘門通過」，閘門在該狀態被評估，因此需要一個 BUDGET_EXCEEDED 出口。
- 任一階段：除 PUBLISHED、FAILED、CANCELLED 與 MANUAL_ACTION_REQUIRED 本身以外的所有狀態。
- 任一未完成階段：除 PUBLISHED、FAILED、CANCELLED 以外的所有狀態。
- 該次失敗的可恢復階段（6）：RESEARCHING、SCRIPTING、VOICE_GENERATING、IMAGE_GENERATING、VIDEO_GENERATING、RENDERING。即「任一可重試階段」扣掉 POSTIZ_DRAFTING 與 SCENE_PLANNING。
- 該次中斷的可恢復階段（4）：SCRIPTING、VOICE_GENERATING、AWAITING_ASSETS、READY_TO_RENDER。前三個是會停到 MANUAL_ACTION_REQUIRED、且有返回列的階段；READY_TO_RENDER 是本節指定的預算閘門落點，重新進入不產生任何 provider 成本。
- POSTIZ_DRAFTING 不是返回目標：§5.3 要求 resume 必須使用 idempotency key，而目前的 Postiz 發布路徑不讀取 idempotency key，重新進入會建立第二份 Draft。等該路徑改為讀取並比對 idempotency key，即可加入。
- SCENE_PLANNING 不屬於任何一個返回目標集合：Scene Planner 在既有 Scene 與重新推導的結果不一致時會整批替換，而重新推導出的 Scene 一律帶空的素材參照，人工補入的素材因此被抹掉——那正是這兩條返回列要保住的東西。同一個機制對兩條列都成立，所以兩邊一起排除，而不是只擋人工那條。代價是規劃階段停駐的 Job 目前只能取消，或繞道 SCRIPTING 重做（那一樣會重新規劃）。等 Planner 改為合併而非替換，兩邊一起加入。
- BUDGET_EXCEEDED 沒有任何返回列，恢復路徑固定是兩跳：先 BUDGET_EXCEEDED → MANUAL_ACTION_REQUIRED，再由該狀態返回。這是產品決定，用狀態機把人工檢查點做成結構性的，不是從「不得自動繼續生成」推導出來的——該條禁止的是自動續跑，不是返回列本身。返回之後預算閘門仍會重跑：真正擋住支出的是閘門，不是這張表。目前閘門實際只在 SCRIPTING 與 VOICE_GENERATING 兩處評估，READY_TO_RENDER 是本節已寫入、尚未實作的第三處；三者都在返回目標集合內，所以兩跳對每個閘門位置都收斂。
- 返回目標的推導：由後往前走 `decisions.jsonl`，取檔案順序而非時間欄位順序。每一筆記錄若 `to` 等於目前狀態就取它的 `from`，否則取它的 `to`；`from` 與 `to` 相同的記錄是拒絕留痕、不是移動，略過；落在 RETRYABLE_FAILED、MANUAL_ACTION_REQUIRED 或 BUDGET_EXCEEDED 的候選是連鎖停駐，繼續往前走。推導不得猜測，下列情況一律拒絕並要求人工判斷：記錄缺 `from`／`to` 或帶未知狀態、決策紀錄為空、走完整份紀錄仍找不到非停駐階段、推導出的階段不在上表允許的返回集合內。
- 上面兩條返回列是必要條件而非充分條件：呼叫端必須先推導出返回目標，不得自行挑一個合法目標。RETRYABLE_FAILED → FAILED 不是返回列，沒有推導對象。
- 推導回答的是「回哪裡」，不是「可不可以回」。RETRYABLE_FAILED 允許自動 resume，上限由 §5.3 的重試規則與 §10 的預算閘門把關；MANUAL_ACTION_REQUIRED 必須由人工觸發，這正是該狀態名稱的意思。

### 5.3 重試與 fallback

- 單一 AI 影片 Scene 最多重試一次。
- 第二次失敗後轉成 image_motion fallback。
- 暫時網路錯誤、429、Provider timeout 可標記 retryable。
- schema、檔案格式、權限、預算與未授權素材不是 retryable。
- Job resume 必須使用 idempotency key，避免重複計費或重複建立 Draft。

## 6. Provider 介面契約

介面可先用 Python Protocol 或既有 service abstraction 實作，核心 Job 不得依賴單一 Provider 名稱。

~~~python
class ResearchProvider(Protocol):
    def research(self, input: ResearchInput) -> ResearchResult: ...

class ScriptProvider(Protocol):
    def generate_script(self, input: ScriptInput) -> ScriptResult: ...

class ImageProvider(Protocol):
    def generate(self, input: ImageGenerationInput) -> ProviderJob: ...

class VideoProvider(Protocol):
    def generate(self, input: VideoGenerationInput) -> ProviderJob: ...
    def get_status(self, job_id: str) -> ProviderJobStatus: ...

class VoiceProvider(Protocol):
    def generate(self, input: VoiceGenerationInput) -> VoiceResult: ...

class Renderer(Protocol):
    def render(self, input: RenderManifest) -> RenderResult: ...

class Publisher(Protocol):
    def create_draft(self, input: PublishInput) -> PublishResult: ...
~~~

每個 Provider 呼叫都必須記錄 provider、model、request_id、external_job_id、idempotency_key、attempt_count、estimated_cost_usd、actual_cost_usd、request_summary、response_summary、error_class、retryable、created_at 與 completed_at。

### 6.1 Assisted Qwen

~~~text
產生 Scene Prompt
→ 寫入 Generation Manifest
→ 人工在 Claude Code／相容 Agent 產圖
→ 放入指定 Scene 目錄或上傳 storage
→ Asset Import 驗證
→ Job resume
~~~

禁止以 Qwen Code 互動式 Token Plan key／session 執行背景排程、放入 WebUI 後端、放入 n8n 或執行無人值守批次。官方 ModelStudio Token Plan endpoint／credential 是另一個 provider contract，不得與 Qwen Code session 混用。

### 6.2 Manual Google Flow

manual_google_flow 只代表人工生成與匯入，不代表 Gemini API 已啟用。若之後接 Gemini API 生成 Veo 素材，`gemini_veo_api` 只是能力別名，audit／selection 一律 canonicalize 為 `gemini_api`，並在每次呼叫前執行成本與權限檢查。

Google AI Pro 權益不得被當成 Gemini API billing 或 API quota。

### 6.3 Voice and Creator Profile

V0 voice path 可使用 `improved TTS`、使用者提供的錄音，或由 Provider 產生後人工匯入的 voice output；Job 只建立一個 Master Voice asset。Voice reference 必須來自明確同意的 Creator Profile。不得默默 clone、模仿或揭露任何人的可識別身分；直接 automated voice-cloning provider call deferred／manual-only。

Scene 只儲存旁白時間區間與字幕引用，不各自生成主旁白。

若 Scene 的 `visual_type` 是 talking-head／avatar 且素材本身含 provider 生成的對白與嘴型同步，必須將 Render Manifest 的 `audio.mode` 設為 `native_speech_avatar`，並將該素材的原生音訊視為 authoritative voice。Renderer 不得用另一條 TTS／錄音覆蓋它；缺少原生音訊、voice selection 不符或影音 QA 未通過時，Scene 必須退回人工處理或 visual-only fallback。

### 6.4 Publisher

V0 只規劃 PostizPublisher.create_draft。它應回傳：

~~~json
{
  "provider": "postiz",
  "draft_id": "",
  "status": "draft",
  "platform": "",
  "scheduled_at": null
}
~~~

任何 publish_now、auto_upload=true 或公開狀態都必須在 V0 被拒絕。

### 6.5 Provider auth mode 與優先序

Provider selection 是產品層 policy，不直接沿用 MoneyPrinterTurbo 的 Registry 順序。每個候選必須宣告：

~~~json
{
  "provider": "gemini_cli",
  "auth_mode": "oauth_cli",
  "execution_mode": "assisted",
  "capability_status": "manual_reauth_required",
  "model": "",
  "fallback_policy": "no_silent_token_fallback"
}
~~~

Canonical provider ID 與 auth mapping：

| canonical provider | auth_mode | execution_mode | 備註 |
|---|---|---|---|
| `gemini_cli` | `oauth_cli` | `assisted` | Gemini CLI OAuth；不得進 backend |
| `manual_google_flow` | `manual_import` | `assisted` | Google Flow 產物匯入 |
| `grok_build` | `oauth_cli` | `assisted` | Grok Build OAuth；不得進 backend |
| `qwen_code_plan` | `interactive_subscription` | `assisted` | Qwen Code 互動式 Coding／Token Plan |
| `gemini_api` | `api_key` | `automated` | `gemini_veo_api` 是能力別名 |
| `vertex_ai` | `vertex` | `automated` | 正式 Vertex credential |
| `xai_api` | `api_key` | `automated` | xAI API |
| `modelstudio_api` | `api_key` | `automated` | Alibaba ModelStudio API |
| `modelstudio_token_plan` | `modelstudio_token_plan` | `automated` | 官方 endpoint／credential；非 Qwen Code session |

`qwen_assisted` 只允許作讀取舊資料時的 alias，寫入 manifest、ProviderEvent 或 UsageLedger 前必須 canonicalize 為 `qwen_code_plan`。`qwen_oauth` 不屬於可選 provider。

Capability status 固定使用小寫 `ready`、`manual_reauth_required`、`manual_action_required`、`provider_unavailable`；selector 的決策 status 才使用大寫 `ASSISTED_READY`、`AUTOMATED_READY`、`ASSISTED_ONLY`、`MANUAL_ACTION_REQUIRED`、`PROVIDER_UNAVAILABLE`。

固定規則：

1. `oauth_cli`、互動式訂閱與 Agent session 只能是 `assisted`，其輸出必須經 manifest／Asset Import 進入 Job；因此 `grok_build` 的 OAuth 不能宣告成 automated。
2. `api_key`、Vertex credential 或正式 ModelStudio Token Plan endpoint 才能成為 `automated` 候選，且仍要經 Budget Guard 與 ProviderEvent 記錄。
3. Assisted 優先序為 `gemini_cli`／`manual_google_flow` → `grok_build` → `qwen_code_plan` → 人工匯入。
4. Automated 優先序為 `gemini_api`／`vertex_ai` → `xai_api` → `modelstudio_api`／`modelstudio_token_plan` → `MANUAL_ACTION_REQUIRED`。
5. `qwen_oauth` 不列入新流程的可用 provider；已存在的舊 cache 不得被當作成功能力證明。
6. Provider 無法滿足執行模式時，狀態必須是 `ASSISTED_ONLY`、`MANUAL_ACTION_REQUIRED` 或 `PROVIDER_UNAVAILABLE`，不得偷偷把另一個 CLI token 當 API Key。

本機測試結果與日期寫入測試報告，不寫入任何 email、access token、refresh token 或 API Key。

### 6.5A LoomLoom B-roll Provider adapter

LoomLoom 是可選的影片素材 provider，不是 Creator Profile、voice cloning 或
avatar/lip-sync provider。其契約如下：

1. `video_source=loomloom` 必須明確啟用；沒有 `LoomLoomConfirmedVideoRequest` 時，後端不得發出 quote 以外的付費 execute。
2. WebUI 依目前 subject／video terms 建立 1～5 row batch；產品 V0 以最多 3 個 generated-video Scene 限制提交數量。quote 回傳的 `listingVersionId`、估算金額與 batch 必須在 execute 前保持一致。
3. execute 必須帶 `confirm=true` 與穩定 `clientRequestId`。遇到可重試 HTTP／網路錯誤時，最多有限重試且只能重用相同 request ID，不能產生新的付費請求。
4. execute 前必須先把 quote、stable request ID、budget 與 `execute_pending` 寫入 task state；付費 run 建立後再記錄安全的 `external_job_id`。任一持久化不明確時不得自動重送，必須標記 `manual_recovery_required`；secret 不能寫入 task state、`VideoParams`、history 或 log。
5. 每一 row 只接受 allowlisted host 的 HTTPS `video/mp4` artifact；禁止 private／loopback host，redirect 必須逐跳重新驗證，下載使用 timeout、512 MB 上限、`.part` 暫存與 atomic rename，且 signed artifact URL 不附帶 LoomLoom Bearer key。
6. quote 的 `estimated_buyer_payable_t` 必須不超過目前 job 的 `budget_limit_t`，並保存 estimated／actual-unknown cost 狀態；超出預算時不得呼叫付費 execute。
7. 下載完成的 clips 直接交給既有 narration／subtitle／BGM／MoviePy 合成；素材本身不得帶入 authoritative voice，也不會解除 `native_speech_avatar` 的人工 consent／QA 邊界。

本 adapter 的測試只使用 fake HTTP session；帳號申請、充值、真實 quote、遠端生成與服務商 SLA 必須另以 live provider evidence 驗證，不能由單元測試推定。

### 6.6 Video/Avatar Provider gate

V0 的 creator avatar 只走人工 Asset Import：user-provided still／image-motion 或預先渲染 talking-head／avatar MP4。Automated voice／avatar cloning 與 lip-sync provider call deferred／manual-only；要開放直接 provider automation，正式 Video/Avatar Provider contract 必須先通過，並記錄：

- credential mode、`auth_mode`、`execution_mode` 與憑證來源。
- `request_id`、`external_job_id` 與 idempotency。
- estimated／actual cost。
- deletion／retention policy。
- technical、consent 與 content QA。

## 7. Asset Import 驗證

匯入每個 Scene 素材時必須：

1. 確認 Asset Record 的 scene_id 與目前 Job 相符。
2. 以 MIME sniffing 與副檔名雙重驗證檔案類型。
3. 限制檔案大小、影片時長與圖片尺寸。
4. 產生 sha256，避免同一檔案重複記錄。
5. 確認影片可由 ffprobe 解碼。
6. 確認素材不是空檔或不完整下載。
7. 記錄來源、Provider、Model、授權／同意資訊。
8. 不允許外部 URL 直接成為 Shell command。
9. 對遠端下載採 allowlist、timeout 與大小上限。
10. 對 Ace 人像與聲音素材記錄來源與使用授權。
11. Creator Profile voice／avatar reference 必須有 explicit consent、usage scope、source、expiry／revocation 與 manual review 記錄。
12. biometric material、secret、credential 與原始敏感內容不得寫入 log、audit 摘要或 prompt。
13. `creator_profile_file` 必須先通過 metadata preflight，且只保存 opaque asset reference，不得保存原始媒體或檔案路徑。

## 8. Render Manifest

Render Manifest 必須是可重現資料，不依賴目前 WebUI 的暫存狀態。

~~~json
{
  "content_job_id": "job-20260816-001",
  "canvas": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "pixel_format": "yuv420p"
  },
  "audio": {
    "mode": "master_voice",
    "master_voice_asset_id": "asset-voice-001",
    "sample_rate": 48000,
    "codec": "aac"
  },
  "scenes": [
    {
      "scene_id": "scene-001",
      "asset_id": "asset-001",
      "start_ms": 0,
      "end_ms": 5000,
      "motion": {
        "type": "ken_burns",
        "scale_start": 1.0,
        "scale_end": 1.08
      },
      "caption_ref": "caption-001"
    }
  ],
  "subtitle_asset_id": "asset-subtitle-001",
  "output": {
    "container": "mp4",
    "video_codec": "h264",
    "audio_codec": "aac"
  }
}
~~~

禁止把繁體字幕、Logo、網址、CTA、QR Code、免責文字或關鍵數字預先烙印在生成圖片／影片中。

上例的 `audio.sample_rate` 寫 48000 是來源取樣率，不是產出取樣率。實作實測（2026-08-30）：moviepy 的 `AudioFileClip` 會把任何來源 resample 到自身預設的 44100，`generate_video` 讀到的 `clip.fps` 因此恆為 44100，產出的 MP4 也一定是 44100。Render Manifest 必須宣告產出值，否則 technical QA 只會恆定失敗；builder 因此固定寫 44100，兩份 frozen fixture 內的 48000 只保留為歷史值。

## 9. CLI／入口映射

在不改程式前，先把需求映射到現有入口：

~~~text
建立 Job          → 現有 CLI／API create flow
規劃 Scene        → plan-assets 或等價 service
匯入素材          → import-assets
本機渲染          → render
技術／內容 QA     → qa
上傳儲存          → upload
建立 Postiz Draft  → postiz-draft
可恢復完整流程    → run --job
~~~

如果現有命令或 Controller 不符合，下一個實作 phase 才提出最小相容改動；不得為了文件先新增假的命令或未連接 API。

## 10. Budget Guard

Budget Guard 應在所有可能產生成本的 Provider 前執行：

~~~text
if actual_cost_usd + estimated_cost_usd > budget_limit_usd:
    transition(BUDGET_EXCEEDED)
    do_not_call_provider()
~~~

需要保存請求前估算、實際回應成本、重試成本、被放棄的圖片／影片、最終採用影片秒數與每個採用影片秒數的有效成本。若 Provider 無法提供實際成本，標記 actual_cost_usd=unknown 並保留估算來源，不可假裝為零。

## 11. 安全與合規

- Secret 只來自環境變數或平台 Secret Store。
- Repo、Log、PR 與文件不得寫入 API Key。
- Prompt 文字不得直接拼成 Shell command。
- FFmpeg 輸入路徑必須經過驗證與 allowlist。
- 檔案類型、大小、尺寸與時長必須在渲染前驗證。
- 外部 URL 下載必須限制來源、timeout、redirect 與大小。
- Postiz 預設拒絕公開發布。
- 人像、聲音、Logo 與品牌素材要有來源／授權紀錄；Creator Profile 另保留 consent、usage scope、expiry／revocation 與 manual review。
- biometric material、secret 與 credential 不進 Log 或 Prompt。
- 產物要保留 Provider、Model 與時間戳。
- 以 tenant_id、brand_id、content_job_id 做隔離，即使 V0 沒有複雜 RBAC。

## 12. 測試契約

### Acceptance criteria

- Creator Profile 必須同時提供明確同意的 voice／avatar reference；缺少 consent、usage scope、source、expiry／revocation 或 manual review，或已 expired／revoked，必須拒絕進入 render。
- Voice 只接受 `improved TTS`、使用者錄音，或人工匯入的 provider-generated output；最終只能有一個 Master Voice，且不得 silent clone 或 expose identity。
- Avatar 只接受人工匯入的 still／image-motion 或預先渲染 talking-head／avatar MP4；未通過正式 Video/Avatar Provider contract 前，直接 provider automation、cloning 與 lip-sync call 必須被拒絕或轉為 manual-only。
- `native_speech_avatar` 必須保留同一 provider 素材的原生音訊，不得用另一條 TTS／錄音覆蓋；voice selection 與角色需通過人工影音 QA，否則不得通過 render。
- V0 仍固定 8～10 個 Scene、最多 3 個 `generated_video` Scene、1080×1920 H.264/AAC、post-render captions 與 Postiz Draft-only publishing。
- log、audit 摘要與 prompt 不得包含 biometric material、secret 或 credential。

### Unit

- Scene Planner 產生 8～10 個 Scene。
- 最多 3 個 generated_video Scene。
- Budget Guard 在超額前阻止 Provider。
- State Transition 拒絕非法跳轉。
- 停駐 Job 的返回目標由 `decisions.jsonl` 推導，並在紀錄為空、格式錯誤、無非停駐階段或推導結果不在返回集合時拒絕而非猜測。
- Idempotency 防止重複生成與重複 Draft。
- Asset Validation 拒絕錯誤 MIME、尺寸、時長與 checksum。
- Creator Profile 驗證 explicit consent、expiry／revocation 與 manual review。
- Creator Profile Preflight 在 LLM／TTS／素材處理前拒絕缺 consent、過期、撤回、未審核或含敏感 payload 的 profile。
- Provider Error Mapping 區分 retryable／manual／fatal。
- Provider Auth Policy 拒絕 `oauth_cli` 進入 `automated`，並保留明確的 `ASSISTED_ONLY` 結果。
- Video/Avatar Provider 未通過 contract 時拒絕 direct automation、voice/avatar cloning 與 lip-sync call。
- Provider 優先序只在同一 `execution_mode` 內 fallback，不跨 Assisted／Automated 邊界。
- Subtitle Timing 不超出 Master Voice 與影片長度。
- Native Speech Sync 驗證 `native_speech_avatar` 的 video／audio 來自同一 provider asset，且 render 沒有覆蓋音軌。
- Render Manifest schema 有通過與拒絕案例。

### Contract

- ImageProvider。
- VideoProvider。
- VoiceProvider。
- Renderer。
- Postiz Publisher。

### Integration

- 本機渲染完整流程。
- 缺素材時安全暫停與 resume。
- Video Provider timeout 後 Scene fallback。
- Budget exceeded 不呼叫 Provider。
- Mock Postiz 只建立 Draft。
- ffprobe 產生可讀的 technical QA report。

### Golden fixtures

~~~text
fixtures/three-scene-demo
fixtures/ten-scene-demo
fixtures/missing-asset
fixtures/video-provider-timeout
fixtures/budget-exceeded
fixtures/render-failure
~~~

測試不得依賴每次都實際付費呼叫 AI 服務。

## 13. 實作順序與 gate

### Phase 0：文件核准

- 核准 PRD-001。
- 核准狀態機、Provider 邊界、Postiz Draft-only 與 Budget Guard。
- 確認品牌資料、字型、Logo、聲音與素材授權。
- 確認 Creator Profile 的 voice／avatar reference、consent scope、expiry／revocation 與 manual review owner。
- 確認 Assisted／Automated Provider 優先序與 OAuth／Token Plan 邊界。
- 確認 Video/Avatar Provider contract gate；未通過前只允許人工匯入。

### Phase 1：最小資料與 schema

- 建立 ContentJob、Script、Scene、Asset、ProviderEvent、UsageLedger 與 RenderManifest schema。
- 先寫 failing tests，再做最小實作。
- 不修改尚未被需求涵蓋的既有流程。

### Phase 2：一條本機 Vertical Slice

~~~text
topic
→ script JSON
→ 8～10 Scene JSON
→ generation manifest
→ 人工匯入 creator voice／avatar asset、3 個圖片／最多 3 個影片
→ Master Voice
→ render
→ ffprobe QA
→ Postiz mock／Draft
~~~

### Phase 3：5 支 POC

完成 5 支實際影片，記錄人工時間、成本、失敗 Scene、fallback 與成片品質。只有 5 支 POC 通過後，才評估 Cloudflare Workflows、Queues、R2 與 Containers。

## 14. Cloudflare 遷移邊界

V0 不把 Cloudflare 當作必要條件。若 V0 通過，V1 才評估 Workers、D1、R2、Queues、Workflows 與 Containers。Containers 只有在本機 Renderer 的資源需求與運行方式被量測後才評估；不得未驗證帳戶、地區、方案與官方支援就寫死。

## 15. 未決策清單

- Postiz Cloud 或 Self-hosted。
- 第一個發布平台。
- Brand Guideline、Logo、字型與色彩。
- 首批 Creator Profile 的 voice／avatar reference、consent owner、usage scope、source、expiry／revocation 與 manual review owner。
- 是否在首批 POC 使用 provider-generated voice output；若使用，僅能人工匯入為 Master Voice。
- Video/Avatar Provider contract 的 owner、deletion／retention policy 與 QA sign-off。
- 每月產量與長期成本上限。
- Gemini API billing 是否另有正式專案。
- Qwen assisted 素材匯入的人工工作台與目錄。
- 是否保留所有失敗生成素材與 storage retention。
- 是否把現有 Upload-Post 流程標記為 deprecated，或只在 V0 adapter 層禁用。
- 人工核可的 resume 是否重置該階段的重試計數，若重置由什麼 audit trail 記錄。
- FR-006／§5.3 的「單一影片場景重試一次→降級為 image_motion」在 V0 **沒有可達的觸發點**，因此不實作、也不做休眠函式。三個前提全部不存在（2026-08-30 實測）：(a) V0 不呼叫任何 Image/Video Provider（§6.6 gate 關閉，全部人工匯入），所以沒有「生成失敗」這件事；(b) 狀態機沒有任何前向邊進入 `VIDEO_GENERATING`（唯一入邊來自 `RETRYABLE_FAILED`，`SCENE_PLANNING → VIDEO_GENERATING` 直接 `IllegalTransitionError`）；(c) `image_motion` 不是 `VisualType` 成員，這條規則唯一能表達的形式是 `visual_type=generated_image` + `motion=ken_burns`（對應 PRD FR-006 的「靜態圖片＋推鏡／平移／局部縮放」）。同理，§12 golden fixtures 的 `video-provider-timeout` 也**未建立**：它的 `decisions.jsonl` 會是狀態機本身拒絕產生的鏈。三者都隨 §6.6 gate 開啟而解除。
- `native_speech_avatar` 是 per-Scene 還是 per-manifest；要表達 per-Scene 需要新增 `RenderSceneEntry.audio_mode` 並把 `RenderAudio.master_voice_asset_id` 改為 Optional。在此決策落地前，**V0 拒絕這個 mode**（`render_manifest.SUPPORTED_AUDIO_MODES` 只含 `master_voice`）：avatar Scene 的素材若帶原生音訊，builder 直接拒絕該 Job 並指名 Scene，而不是靜默用 Master Voice 覆蓋。因此 §12 的 `native_speech_avatar` 兩條（保留原生音訊、Native Speech Sync 驗證）在 V0 無對象可驗，屬 Phase 2。

> PRD-001 與本 SPEC 已於 2026-08-30 核准；V0 pipeline 依此實作，既有功能程式（task.py／llm.py／voice.py／subtitle.py／video.py）維持未修改。
