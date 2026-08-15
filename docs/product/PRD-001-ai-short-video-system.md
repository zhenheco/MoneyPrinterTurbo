# PRD-001：Zhenhe AI 短影音經營系統

> 文件狀態：Draft，等待使用者核准  
> 文件版本：0.1  
> 目標 Repo：zhenheco/MoneyPrinterTurbo  
> 上游 Repo：harry0703/MoneyPrinterTurbo  
> 目標租戶：zhenhe  
> 目標品牌：zhenhe-ai  
> 建立日期：2026-08-16

## 0. 文件定位

本文件把既有 AI 短影音需求落地到 MoneyPrinterTurbo fork 的產品範圍與驗收標準。它是規劃文件，不代表功能已經實作，也不授權在核准前修改既有功能程式或部署正式資源。

第一階段原則：

1. 先重用 MoneyPrinterTurbo 現有的 Python、WebUI、API、CLI 與本機渲染能力。
2. 保留人工補素材與人工審核，先證明成片品質與單支成本。
3. Provider、素材、成本與狀態必須可追溯。
4. Cloudflare、Queues、Workflows、Containers 與多租戶能力只在 V0 證明後再評估。

## 1. 產品目標

### 1.1 產品結果

給定一個繁體中文題目，系統應能：

1. 讀取 Zhenhe AI 品牌設定。
2. 產出結構化腳本與 8～10 個語意場景。
3. 針對最多 3 個場景建立影片生成需求，其他場景使用圖片動態、流程圖、字卡或截圖。
4. 允許 Assisted Qwen 模式人工產圖，以及 Manual Google Flow 模式人工補做影片。
5. 以一致的 Master Voice 完成旁白、字幕與混音。
6. 用本機 Renderer 合成 1080×1920、45～60 秒的 H.264/AAC 影片。
7. 通過技術 QA 與內容 QA 後，只建立 Postiz Draft，不直接公開發布。
8. 為每個素材、Provider 呼叫、重試、成本與人工決策保留記錄。

### 1.2 第一個要驗證的假設

第一個真正的 POC 問題不是 Cloudflare 長流程能否啟動，而是：

> Qwen 產出的角色或情境圖片，經過人工 Flow／影片 Provider 動態化後，能否用合理成本形成可接受的連續短影音。

第一條可驗證鏈：

~~~text
1 個題目
→ 1 份結構化腳本
→ 8～10 個 Scene
→ 其中最多 3 個影片場景
→ 其餘圖片／圖卡／截圖場景
→ 1 條完整 Master Voice
→ 本機合成與 QA
→ Postiz Draft
~~~

## 2. 目標使用者與情境

V0 只服務 Zhenhe AI 內部內容工作者，不實作客戶登入、複雜 RBAC、訂閱計費或公開 SaaS onboarding。

主要情境：

- 輸入主題「企業導入 AI 最常犯的三個錯誤」。
- 系統產出腳本、Scene 計畫與素材 manifest。
- 工作人員在 Claude Code／相容 Agent 中依 manifest 產生 Qwen 圖片。
- 工作人員使用 Google Flow 補做少量 Hero Shot，或匯入既有影片。
- 系統驗證檔案、尺寸、Scene ID、時長與格式後繼續。
- 工作人員檢查成片、QA 報告與成本，最後在 Postiz 中人工確認 Draft。

## 3. 固定產品規格

| 項目 | V0 規格 |
|---|---|
| 語言 | 繁體中文（zh-TW） |
| 市場 | 台灣 |
| 影片比例 | 9:16 |
| 輸出尺寸 | 1080×1920 |
| 影片長度 | 45～60 秒 |
| 影片編碼 | H.264 |
| 音訊編碼 | AAC |
| Scene 數量 | 8～10 個語意場景 |
| AI 影片場景 | 最多 3 個 |
| 單一 AI 影片場景 | 4～6 秒，預設 720p 生成後統一渲染 |
| 發布 | Postiz Draft |
| 公開發布 | V0 禁止 |
| 單支預算 | API 自動化估算上限 US$3 |
| POC 數量 | 5 支實際影片 |

## 4. 功能需求

### FR-001：建立 Content Job

系統必須保留 tenant_id、brand_id、content_job_id、topic、target_duration_sec、language、image_mode、video_mode、budget_limit_usd 與 publish_mode。建立 Job 不得依賴檔名猜測狀態。

### FR-002：結構化腳本

腳本至少要有：

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

可查證主張要有來源或被標記為待人工確認。

### FR-003：Scene 計畫

每個 Scene 至少要有：

~~~json
{
  "scene_id": "scene-001",
  "scene_index": 1,
  "semantic_purpose": "",
  "narration": "",
  "caption": "",
  "duration_target_ms": 5000,
  "visual_type": "generated_image",
  "visual_prompt": "",
  "reference_assets": [],
  "generation_required": false,
  "provider": "",
  "fallback_type": "image_motion"
}
~~~

Scene 必須可以獨立重做，不得因單一場景失敗而重做整支影片。

### FR-004：聲音與字幕

- 旁白預設採一次產生的 Master Voice。
- 不得讓每個影片場景各自產生不一致的人聲。
- 主要旁白不得依賴生成式影片的原生音訊。
- 字幕、數字、Logo、網址、CTA、QR Code、免責文字與品牌標題全部由後製層產生。
- 字幕時間軸應能對應旁白句子或單字時間軸。

### FR-005：人工補入素材

V0 必須支援等待人工素材的明確狀態。人工可使用 Assisted Qwen 產圖、使用 Manual Google Flow 產生少量影片、選擇結果並放入指定 Scene 目錄、匯入既有聲音／圖片／影片。

系統必須驗證檔案類型、檔案大小、尺寸、時長、Scene ID 與 checksum。

### FR-006：Scene fallback

單一影片場景第一次生成失敗時可重試一次；第二次失敗必須降級為：

~~~text
靜態圖片
＋推鏡／平移／局部縮放
＋字幕與動態字卡
~~~

不得因單一 Scene 失敗而中止或重做整支影片。

### FR-007：預算閘門

每次 Provider 呼叫前都要驗證：

~~~text
actual_cost_usd + estimated_next_cost_usd <= budget_limit_usd
~~~

若不成立，Job 進入 BUDGET_EXCEEDED，不得自動繼續生成。成本記錄至少要包含 Provider、Model、重試、放棄結果與最後採用秒數。

### FR-008：技術與內容 QA

技術 QA 至少檢查影片可解碼、ffprobe metadata、1080×1920、音訊、字幕範圍與 Asset Record。內容 QA 至少檢查繁體中文、Hook、核心觀點、結論、CTA、來源／風險標記與人工否決能力。

### FR-009：Postiz Draft

V0 只允許建立 Draft。系統不得直接呼叫公開發布流程，不得以既有 Upload-Post 的 auto upload 設定取代人工核准。

## 5. MoneyPrinterTurbo 基線與調整

本 fork 重用 MoneyPrinterTurbo 已有的 Python 3.11+、Streamlit WebUI、FastAPI API、CLI、MoviePy、FFmpeg、TTS、字幕、素材與多 Provider 能力。基線可由 fork 的 [README.md](https://github.com/zhenheco/MoneyPrinterTurbo/blob/main/README.md) 與 [pyproject.toml](https://github.com/zhenheco/MoneyPrinterTurbo/blob/main/pyproject.toml) 核對。

需求對基線的調整：

1. 固定繁體中文、45～60 秒、8～10 Scene 與最多 3 個 AI 影片 Scene。
2. 將人工補素材變成可追蹤的流程狀態。
3. 將 Qwen 訂閱與 Google AI Pro 定義為 Assisted／Manual 模式，不把訂閱 Key 接到背景服務。
4. 將 Postiz Draft 設為唯一的 V0 發布出口。
5. 將成本、Provider、Model、Request ID、External Job ID、Retry 與 Asset Record 納入追溯資料。
6. 優先重用既有本機 Renderer；不在 V0 先加入 Cloudflare Containers 或大型事件系統。

## 6. 明確不做

- 自動品牌定位與全網趨勢搜尋。
- 完整多租戶登入、RBAC、Billing 與客戶 BYOK。
- 自動公開發布。
- 完整 Analytics Learning Loop。
- 數位人逐字對嘴。
- 同一場景多 Provider 競賽。
- Cloudflare Container 正式部署。
- 將 Qwen Token Plan 或 Google AI Pro 權益當成後端 API 額度。
- 為了需求文件而修改既有功能程式。

## 7. POC 驗收

POC 至少完成 5 支實際影片，並符合：

- 可從一個題目建立 Job。
- 每支有 8～10 個 Scene。
- 每支最多 3 個 AI 影片 Scene。
- 任一 Scene 可獨立重做或 fallback。
- 全片使用一致的 Master Voice。
- 字幕與旁白基本同步。
- 輸出為 1080×1920、H.264/AAC。
- 所有素材、Provider 呼叫與成本可追溯。
- 可建立 Postiz Draft，且不會直接公開。
- 5 支中至少 4 支完成。
- 單支影片人工介入低於 15 分鐘。
- 不需為每支影片修改程式。
- 至少一個 Provider 可替換而不改動核心 Job 流程。
- 成片品質足以進入人工審稿，而非完全重做。

## 8. 核准前決策

請核准以下方向後，才進入 SPEC 實作拆解與程式修改：

1. zhenhe／zhenhe-ai 是否為第一個固定 tenant／brand。
2. 第一個發布平台是否由 Postiz Draft 統一承接。
3. Assisted Qwen 與 Manual Google Flow 是否接受人工補素材。
4. 單支 API 預算上限是否採 US$3。
5. 是否先以本機 Renderer 做 V0，不先做 Cloudflare Runtime 遷移。
6. 是否接受一般 TTS／既有錄音，暫不做數位人對嘴。
7. 是否允許所有成片在人工核准前只停留在 Draft。

> 目前未進行功能程式修改；本文件本身是等待核准的產品草案。
