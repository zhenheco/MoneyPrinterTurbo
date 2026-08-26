# PLAN-001：V0 執行規劃判斷（餵給 /go）

> 狀態：Advisory，2026-08-26
> 依據：PRD-001 v0.2、SPEC-001 v0.2、ADR-001、現況量測（Phase 1 零實作、602 tests 全綠）
> 範圍：SPEC §13 Phase 1 + Phase 2；Phase 3 排除（見 Q6）

## Q1 持久化選型

**決定：新模組 pydantic model + job 目錄 JSON/JSONL 檔（選項 c 的變體），不用 SQLite，不擴充 schema.py。**

- SPEC §3.2 本身就把 job 目錄檔案佈局定為輸出契約（`input/request.json`、`scripts/script.json`、`audit/*.jsonl`）。檔案就是 persistence，SQLite 會變成第二份 truth，V0 反而多一層同步。
- 具體形態：`app/models/content_job.py`（或 `app/models/jobs/`）放 ContentJob/Script/Scene/AssetRecord/ProviderEvent/UsageLedger/RenderManifest 的 pydantic model；一個薄 repository（`app/services/jobs/store.py`）負責 job 目錄的 load/save，append-only 的 ProviderEvent/UsageLedger/decisions 走 JSONL append。
- **不放進 `app/models/schema.py`**：那是上游檔案，塞產品 model 會擴大 upstream merge 衝突面（見 Q2）。
- V1 遷移代價：pydantic schema 1:1 對映 D1 table，JSONL ledger 對映 append-only table，job 目錄檔案對映 R2 key。遷移是「換 repository 實作」，不是重寫 model。
- 既有 `state.py`（Memory/Redis task state）只當 WebUI/API 的 runtime 進度顯示用，不當 ContentJob 的 source of truth——SPEC §3.2 明定「不得以檔名單獨推論狀態」，同理不得以 in-memory state 推論 Job 狀態；`job.json` 內的 `status` 欄位是唯一權威。

**翻盤條件**：V0 內出現「跨 process 併發搶同一個 job」或「需要對數百個 job 做查詢/排序」的需求 → 換 SQLite（stdlib `sqlite3`，schema 直接沿用 pydantic 欄位）。目前 PRD 明定單人內部使用、5 支 POC，不成立。

## Q2 與既有 task.py 的共存策略

**決定：(c) 平行新路徑——新 orchestrator 模組，復用 leaf services，不碰 task.py。**

- 新增 `app/services/jobs/pipeline.py`（狀態機 + 各 stage handler），直接呼叫 leaf services：`llm.py`（script）、`voice.py`（master voice + timestamps）、`subtitle.py`、`video.py`（MoviePy 合成）、`video_material.py`（匯入驗證）、`provider_policy.py`、`creator_profile.py`。
- 不選 (a) 侵入式：task.py 1511 行是上游熱區（recent commits 都在動它），每改一行都是未來 merge conflict。不選 (b) 包 task.py：`_run_pipeline` 的階段切分（script 純文字、per-scene 無 scene_id、無狀態機）與 SPEC 契約不相容，包起來會被迫在 orchestrator 裡到處繞既有假設，比重寫 stage 膠水更貴。
- Upstream merge 衝突面：task.py = 0；schema.py = 0（新 model 另立檔案）；剩餘風險只在 leaf service 函式簽名被上游改動——這用薄 adapter（每個 provider 契約一個 `_adapt_*` 函式）隔離，衝突時只修 adapter。
- Renderer 這一段是唯一「必須從 task.py/video.py 抄邏輯」的地方：以 RenderManifest 為輸入寫一個 `Renderer` 實作，內部呼叫 `video.py` 的合成函數。若 `video.py` 對 `VideoParams` 耦合太深，允許在 jobs/ 內建一個最小 MoviePy 組裝（ken burns + concat + master voice + srt burn），仍不改上游檔。

**翻盤條件**：spike（Q5 風險 1）證明 leaf services 完全無法脫離 VideoParams 使用、且最小自建 MoviePy 組裝超過 ~300 行 → 退回 (b)，用「構造合成 VideoParams 餵 task.py stage 函式」的 adapter 路線。

## Q3 SPEC §15 十一項未決策分流

**本次 /go（Phase 1+2）BLOCKER：0 項。** 兩項在 Phase 3 前必須人工解決（標 ⚠）。

| # | 未決策 | 分流 | V0 預設值 |
|---|---|---|---|
| 1 | Postiz Cloud/Self-hosted | DEFAULTABLE ⚠Phase 3 | adapter 吃 config base URL + API key；Phase 2 測試全走 mock（§12 本來就要求 Mock Postiz）。真實 instance 是 Phase 3 前的人工決策 |
| 2 | 第一個發布平台 | DEFAULTABLE | draft payload 的 `platform` 是 passthrough 字串，config 預設 `"threads"`；換平台零重做 |
| 3 | Brand Guideline/Logo/字型 | DEFAULTABLE | 沿用 repo 既有字型；brand assets 走 config 路徑，缺席時 render 不疊 Logo。品質問題留給 Phase 3 |
| 4 | 首批 Creator Profile consent | DEFAULTABLE ⚠Phase 3 | Phase 1+2 用 fixture profile（`explicit_granted`+`approved`，creator_profile.py 已能驗證）。真人素材 consent owner 是 Phase 3 前人工決策 |
| 5 | POC 用 provider-generated voice？ | DEFAULTABLE | 否——Master Voice 走既有 TTS（edge/azure 由現行 config 決定）。人工匯入 voice 的路徑照 SPEC 實作但 POC 預設不用 |
| 6 | Video/Avatar Provider contract owner | DEFAULTABLE | V0 gate 保持關閉（manual import only，§6.6），不需要 contract 就能跑完 V0 |
| 7 | 每月產量/長期成本上限 | DEFAULTABLE | 不影響 code；per-job `budget_limit_usd=3` 已固定 |
| 8 | Gemini API billing 專案 | DEFAULTABLE | `gemini_api` 維持 `manual_action_required`；vertical slice 用 assisted/人工匯入，不需 automated video provider |
| 9 | Qwen assisted 工作台目錄 | DEFAULTABLE | 即 SPEC §3.2 的 `scenes/{scene_id}/images/`，generation manifest 印出絕對路徑 |
| 10 | 失敗素材 retention | DEFAULTABLE | V0 全留（本機磁碟），AssetRecord 標 `abandoned`；不做清理 |
| 11 | Upload-Post deprecated？ | DEFAULTABLE | adapter 層禁用：`publish_mode=postiz_draft` 時拒絕任何 auto-upload 路徑；不動上游 upload_post.py（merge 摩擦） |

判斷依據：使用者意圖是「跑完」，而這 11 項沒有任何一項會改變 Phase 1+2 的 schema、狀態機或 code 結構——全部都是 config 值或 Phase 3 營運決策。**翻盤條件**：若使用者其實想讓 /go 一路做到真實 Postiz draft（非 mock），則 #1 立刻升級 BLOCKER（要 instance + credential）。

## Q4 Issue 切片（Phase 1 + Phase 2，11 個 tracer-bullet）

每個 issue 完成時：全部既有 602 tests 仍綠、新 tests 綠、系統可運行（新路徑是平行的，天然不破壞既有功能）。TDD：每個 issue 先 failing test。

| # | 標題 | 範圍 | 驗收條件 | SPEC | 依賴 | 大小 |
|---|---|---|---|---|---|---|
| 1 | Job 資料契約與檔案儲存 | 7 個 pydantic model（ContentJob/Script/Scene/AssetRecord/ProviderEvent/UsageLedger/RenderManifest）+ job 目錄 store（load/save/JSONL append）+ frozen fixtures | schema 通過/拒絕案例各有 test；store round-trip；fixtures `three-scene-demo`/`ten-scene-demo` 建立 | §3.2 §4 §12 | — | M |
| 2 | Job 狀態機 | 21 狀態 + §5.2 轉移表 + 非法跳轉拒絕 + retryable/manual/fatal error 分類 + `decisions.jsonl` | 每條合法轉移與代表性非法轉移有 test；PUBLISHED 不可達 | §5 | 1 | M |
| 3 | Budget Guard + 追溯記錄 | 呼叫前預算檢查、BUDGET_EXCEEDED 轉移、ProviderEvent/UsageLedger writer、idempotency key、actual_cost=unknown 處理 | 超額前阻止 provider 呼叫；重複 resume 不重複計費（idempotency test） | §10 §4.6 | 1,2 | S |
| 4 | 建立 Job + Script JSON 生成 | CLI/API create（§3.1 輸入驗證）→ DRAFT→SCRIPTING；用既有 llm.py 產結構化 Script JSON + schema 驗證 + 1 次 repair retry | 從 topic 產出通過 schema 的 `scripts/script.json`；LLM 呼叫寫 ProviderEvent | §3.1 §9 FR-002 | 1,2,3 | M |
| 5 | Scene Planner + Generation Manifest | Script→8~10 Scene JSON、≤3 `generated_video`、visual_type 分配、fallback_type、per-scene 目錄 + 人工匯入用 manifest 輸出 | 8~10 scene 恆成立；>3 video 被拒；manifest 列出每個 scene 的匯入路徑與 prompt | §4.4 §6.1 FR-003 | 4 | M |
| 6 | Master Voice + 時間軸 | 沿用 voice.py 產單一 master voice + timestamps；VOICE_GENERATING→AWAITING_ASSETS；voice asset 記為 AssetRecord | `audio/master-voice.*` + timestamps JSON 產生；只有一個 voice asset | §6.3 FR-004 | 5 | M |
| 7 | 字幕生成 | 由 master voice timestamps 產 `captions.srt`/`captions.json`（沿用 subtitle.py）；字幕不超出 voice 長度 | subtitle timing test 通過；caption_ref 對映 scene | FR-004 §12 | 6 | S |
| 8 | Asset Import + Creator Profile preflight | `import-assets` 流程：§7 全部 13 條驗證（復用 video_material.py + creator_profile.py）；AWAITING_ASSETS→READY_TO_RENDER；缺件→MANUAL_ACTION_REQUIRED | 錯 MIME/尺寸/checksum/scene_id 被拒；`missing-asset` fixture 安全暫停可 resume；consent 不全拒進 render | §7 §4.1 FR-005 | 5 | L |
| 9 | Render Manifest + Renderer + ffprobe QA | manifest builder（含 `native_speech_avatar` audio.mode 規則）→ MoviePy render 1080×1920 H.264/AAC → ffprobe 驗 codec/尺寸/音訊 → TECHNICAL_QA | `three-scene-demo` fixture 端到端渲出 final.mp4 且 ffprobe QA 通過；manifest schema 拒絕案例 | §8 FR-008 §12 | 6,7,8 | L |
| 10 | Postiz Draft adapter | `PostizPublisher.create_draft`（mock HTTP session 測試）+ draft-only 強制（publish_now/auto_upload 一律拒絕）+ POSTIZ_DRAFTING→POSTIZ_DRAFTED | mock 只建 draft；公開發布請求被拒；draft_id 寫回 job | §6.4 FR-009 | 2,3 | M |
| 11 | `run --job` 可恢復全流程 + golden fixtures 收尾 | 端到端 orchestrator 串 4~10；中斷後 resume；補齊 `video-provider-timeout`/`budget-exceeded`/`render-failure` fixtures 與 integration tests；scene fallback（重試 1 次→image_motion） | §12 Integration 六條全綠；一次本機真實 demo：topic→…→final.mp4→mock draft | §5.3 §13 Phase 2 FR-006 | 4–10 | M |

順序即依賴序。#3/#10 可與相鄰 issue 並行。總量：2L + 7M + 2S。
註：V0 vertical slice 不含 automated image/video provider 呼叫（全走人工匯入），所以 §6 的 ImageProvider/VideoProvider automated contract 只需 Protocol 定義 + policy 拒絕測試（涵蓋在 #3/#8），不需真實 adapter。

## Q5 三大執行風險與先行驗證

1. **Renderer 復用失敗**（video.py 深耦合 VideoParams，manifest→MoviePy 接不起來）——最貴的 L issue 建立在未驗證假設上。**先行動作**：寫 code 前先讀 `app/services/video.py` 的合成函式簽名，並跑一個 30 行 throwaway spike（scratchpad 內）：拿 `storage/local_videos` 現成素材 + 一段現成音檔直接呼叫合成函式出 mp4 + ffprobe。半小時內知道走復用還是最小自建，直接決定 issue #9 的寫法與 Q2 是否翻盤。
2. **Script JSON 結構化輸出不穩**（現行 llm.py 是產純文字腳本的 prompt，zh-TW 結構化 JSON 命中率未知）。**先行動作**：用現行已配置的 LLM provider 手動跑 3 次結構化 prompt，量 schema 通過率；<3/3 就在 issue #4 內建 repair-retry（已預留），0/3 則 script prompt 設計要先獨立處理再開工。
3. **/go 多 issue dispatch 的契約漂移**（11 個 issue 各自實作，schema/路徑不一致，integration 在 #11 才爆）。**先行動作**：issue #1 的驗收就包含 frozen golden fixtures + 單一 model 模組路徑（`app/models/content_job.py`），後續每個 issue 的驗收條件都引用同一組 fixtures；to-issues 時把 module path 與 fixture 路徑寫死進每張 issue 內文，不留給 implementer 自由發揮。

## Q6 Phase 3（5 支 POC）是否納入本次 /go

**不納入。** Phase 3 的本質是人工營運驗收（真實 Postiz instance、真人 consent 素材、Assisted 人工產圖/影片、實際成本與 15 分鐘人工介入量測），不是 autonomous code loop 能完成的工作；硬塞進 /go 只會在缺憑證/缺素材處卡死或誘發 agent 造假繞過。/go 的終點 = issue #11 全綠 + 一次本機 demo render。可以額外加一張 S 號 docs issue：產出 Phase 3 POC 操作 runbook（人工步驟 + 記錄表格），作為 /go 交付的最後一項。**翻盤條件**：使用者已備妥 Postiz credential 與已核准的 creator 素材，且願意人工在 AWAITING_ASSETS 時補件——那 Phase 3 第 1 支可以在 /ship 後以 attended 模式跑，但仍不該進 autonomous loop。
