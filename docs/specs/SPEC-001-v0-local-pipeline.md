# SPEC-001：MoneyPrinterTurbo V0 本機 Vertical Slice

> 文件狀態：Draft，等待 PRD-001 核准
> 文件版本：0.2
> 目標 Repo：zhenheco/MoneyPrinterTurbo
> 目標分支：main
> 建立日期：2026-08-16

## 1. 範圍與不變條件

本 SPEC 定義如何把 Zhenhe AI 的 AI 短影音需求映射到 MoneyPrinterTurbo 現有的 Python／WebUI／API／CLI／MoviePy／FFmpeg 基線。此階段只定義契約、資料、狀態、驗收與實作順序；PRD 未核准前不得建立正式功能或修改既有業務程式。

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
| 語音 | 既有 Edge TTS、Azure、Gemini 等選項 | 預設一次產生 Master Voice，Scene 只引用時間區間 |
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

### 4.1 ContentJob

~~~json
{
  "content_job_id": "job-20260816-001",
  "tenant_id": "zhenhe",
  "brand_id": "zhenhe-ai",
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

### 4.2 Script

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

### 4.3 Scene

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

### 4.4 Asset Record

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
  "created_at": ""
}
~~~

### 4.5 Provider Event 與 Usage Ledger

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

V0 不應直接從任何狀態進入 PUBLISHED。APPROVED、SCHEDULED 與 PUBLISHED 只保留給後續受控發布流程。

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

### 6.3 Voice

V0 可使用 Edge TTS、既有錄音或另一個已核准 TTS Provider，但 Job 只建立一個 Master Voice 輸出。Scene 只儲存旁白時間區間與字幕引用，不各自生成主旁白。

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
- 人像、聲音、Logo 與品牌素材要有來源／授權紀錄。
- 產物要保留 Provider、Model 與時間戳。
- 以 tenant_id、brand_id、content_job_id 做隔離，即使 V0 沒有複雜 RBAC。

## 12. 測試契約

### Unit

- Scene Planner 產生 8～10 個 Scene。
- 最多 3 個 generated_video Scene。
- Budget Guard 在超額前阻止 Provider。
- State Transition 拒絕非法跳轉。
- Idempotency 防止重複生成與重複 Draft。
- Asset Validation 拒絕錯誤 MIME、尺寸、時長與 checksum。
- Provider Error Mapping 區分 retryable／manual／fatal。
- Provider Auth Policy 拒絕 `oauth_cli` 進入 `automated`，並保留明確的 `ASSISTED_ONLY` 結果。
- Provider 優先序只在同一 `execution_mode` 內 fallback，不跨 Assisted／Automated 邊界。
- Subtitle Timing 不超出 Master Voice 與影片長度。
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
- 確認 Assisted／Automated Provider 優先序與 OAuth／Token Plan 邊界。

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
→ 人工匯入 3 個圖片／最多 3 個影片
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
- Ace Character Pack 與聲音素材是否已具備。
- 每月產量與長期成本上限。
- Gemini API billing 是否另有正式專案。
- Qwen assisted 素材匯入的人工工作台與目錄。
- 是否保留所有失敗生成素材與 storage retention。
- 是否把現有 Upload-Post 流程標記為 deprecated，或只在 V0 adapter 層禁用。

> 本 SPEC 完成後仍未進行功能程式修改；等待 PRD 與 SPEC 核准。
