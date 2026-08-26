# V0 Job Pipeline Foundation — SPEC

> `/go` 唯一輸入。範圍 = PLAN-001 的 issue #1、#2、#3、#10。
> 上游文件：`docs/specs/SPEC-001-v0-local-pipeline.md`、`docs/product/PRD-001-ai-short-video-system.md`、`docs/adr/ADR-001-provider-auth-boundaries.md`、`docs/specs/PLAN-001-v0-execution.md`。

---

## Problem Statement

目前要產一支短影音，操作者必須在 WebUI 一次把主題、素材、語音、字幕設定填完，然後盯著它跑完。中途只要有一步失敗——素材沒下載到、語音 provider timeout、算到一半發現這支片已經燒掉預算——整個流程就從頭開始，前面付過錢的步驟再付一次。

更麻煩的是「等人」這件事沒有位置可放。V0 的素材有一部分必須由人工產生後匯入（Assisted Qwen 產圖、Manual Google Flow 產影片），但現行流程沒有「停在這裡等人補件，補完接著跑」的狀態，只能整批重來。

同時，成本是黑的。跑完才知道花了多少，失敗重試的花費沒有被記下來，沒辦法回答「這支片到底成本多少」或「這個月燒了多少」。

發布端也有風險：現行上傳流程能直接把片公開發出去，V0 階段不該有任何一條路徑能繞過人工審核。

## Solution

把一支影片的製作變成一個**可查詢、可中斷、可續跑的 Job**。

操作者建立 Job 後，Job 會停在它該停的地方——等腳本、等素材、等審核——而且每一次停都留下明確狀態與原因。補完素材後從斷點續跑，已完成且已付費的步驟不會重跑、不會重複計費。

每一次可能花錢的外部呼叫前都先過預算閘門：預估成本加上已花費超過這個 Job 的上限就直接擋下，狀態轉成 `BUDGET_EXCEEDED`，不呼叫 provider。每次呼叫的估算成本、實際成本、重試成本都寫進帳本，隨時能回答一支片花了多少。

發布端只做草稿。系統只會在 Postiz 建立 draft，任何要求立即公開發布的參數一律被拒絕，公開與否是人的決定。

本次交付的是這條 pipeline 的地基四塊：資料契約與儲存、狀態機、預算閘門、Postiz 草稿發布器。腳本生成與渲染（需要 LLM 憑證與影片合成）不在本次範圍。

## User Stories

1. As an operator, I want 建立一個 Content Job 並拿到 job id，so that 後續每一步都能掛在同一個 id 下追蹤。
2. As an operator, I want Job 的所有資料（job、script、scenes、assets、provider events、usage ledger、render manifest）以固定 schema 存在一個 job 目錄底下，so that 我能直接開檔案看發生什麼事，不需要查資料庫。
3. As an operator, I want 塞入不符 schema 的資料時立刻被拒絕並告訴我哪個欄位錯，so that 錯誤不會被寫進檔案然後在三步之後才炸。
4. As an operator, I want 同一個 job 目錄被讀出來後再寫回去內容完全一致，so that 續跑不會靜默弄丟欄位。
5. As an operator, I want 系統提供 `three-scene-demo` 與 `ten-scene-demo` 兩組凍結的測試 fixture，so that 後續每個功能都對同一組資料驗收，不會各做各的。
6. As an operator, I want Job 只能沿 SPEC-001 §5.2 定義的合法路徑轉移狀態，so that 不會出現「還沒生語音就進入渲染」這種不可能的狀態。
7. As an operator, I want 任何非法狀態跳轉被拒絕並拋出說明是哪條轉移不被允許的錯誤，so that bug 在寫入前就被擋住。
8. As an operator, I want V0 的 Job 永遠無法進入 `PUBLISHED`，so that 沒有任何程式路徑能繞過人工審核直接公開。
9. As an operator, I want 每一次狀態轉移連同原因寫進 `decisions.jsonl`，so that 事後能重建這個 Job 為什麼走到現在這一步。
10. As an operator, I want 錯誤被分類成 retryable / manual / fatal，so that 網路 timeout 會自動重試，而權限或 schema 錯誤不會浪費重試次數。
11. As an operator, I want 每一次可能產生成本的呼叫前先檢查「已花費 + 本次預估」是否超過 Job 預算上限，so that 超支在花錢之前就被擋下。
12. As an operator, I want 預算超標時 Job 轉成 `BUDGET_EXCEEDED` 且 provider 完全不被呼叫，so that 擋下來就是真的沒花到錢。
13. As an operator, I want 每次 provider 呼叫寫一筆 ProviderEvent 與一筆 UsageLedger，記錄估算成本、實際成本、重試次數與錯誤類別，so that 成本可以被回溯到單一 scene。
14. As an operator, I want provider 無法回報實際成本時記為 `unknown` 而不是 0，so that 帳本不會假裝這次呼叫免費。
15. As an operator, I want 中斷後續跑時已完成的付費呼叫因為 idempotency key 相同而不重複計費，so that 續跑是安全的。
16. As an operator, I want ProviderEvent 的摘要欄位永不包含 API key、Authorization header 或憑證，so that 帳本可以安全地被讀取與分享。
17. As an operator, I want 在 Postiz 建立草稿並拿到 draft_id，so that 我能在 Postiz 後台審核後自己決定發不發。
18. As an operator, I want 任何 `publish_now`、`auto_upload=true` 或公開狀態的請求被拒絕，so that V0 不可能誤發。
19. As an operator, I want Postiz 呼叫失敗時 Job 停在 `POSTIZ_DRAFTING` 並記錄錯誤而不是靜默成功，so that 我不會以為草稿建好了其實沒有。
20. As an operator, I want 上述所有行為都不改動既有的 `app/services/task.py` 流程，so that 現有 WebUI/CLI 功能與上游 merge 都不受影響。

## Modules

| Module | 職責（一句） | 公開介面（窄） | 新建/修改 |
|---|---|---|---|
| `app/models/content_job.py` | SPEC-001 §4.2–§4.6 與 §8 的 7 個資料契約，pydantic model，只管形狀驗證 | `ContentJob`、`Script`、`Scene`、`AssetRecord`、`ProviderEvent`、`UsageLedgerEntry`、`RenderManifest` | 新建 |
| `app/services/jobs/store.py` | 一個 job 目錄的讀寫，JSON 單檔 + JSONL append，檔案即 truth | `JobStore(root)`、`.create(record)`、`.load(job_id)`、`.save(job)`、`.replace(record)`、`.append_event(job_id, event)`、`.append_decision(job_id, record)` | 新建 |
| `app/services/jobs/state_machine.py` | SPEC-001 §5 的合法轉移判定與錯誤分類，純函式無 I/O | `transition(job, to_status, reason)`、`is_legal(from_status, to_status)`、`classify_error(exc)` | 新建 |
| `app/services/jobs/budget.py` | 呼叫 provider 前的預算閘門與呼叫後的帳本寫入 | `check_budget(job, estimated_cost_usd)`、`record_usage(store, job, event)`、`build_idempotency_key(...)` | 新建 |
| `app/services/jobs/postiz.py` | Postiz 草稿建立，draft-only 強制 | `PostizPublisher(settings, session=None)`、`.create_draft(job, media_path, caption)` | 新建 |
| `app/services/task.py` | 既有 pipeline | 不變 | **不得修改** |

模組全部落在新的平行路徑 `app/services/jobs/`，不觸碰 `app/models/schema.py` 與既有 service。理由見 PLAN-001 Q2：`task.py` 是上游熱區，侵入式修改會讓每次 upstream merge 都衝突。

## Implementation Decisions

- **Schema**: 無資料庫。持久化 = job 目錄下的 JSON/JSONL 檔案（PLAN-001 Q1）。目錄結構固定為 `storage/jobs/<content_job_id>/`，內含 `job.json`、`scripts/script.json`、`scenes/scene-NNN.json`、`assets/assets.jsonl`、`provider_events.jsonl`、`usage_ledger.jsonl`、`decisions.jsonl`、`render_manifest.json`。不使用 SQLite——V0 是單機單使用者，檔案即 truth，V1 遷 Cloudflare 時只換 `JobStore` 實作。
- **API contract**: 本次不新增 HTTP endpoint。所有模組以 Python 介面暴露，供後續 slice 的 CLI/API 層呼叫。
- **資料契約**: 欄位形狀完全照 SPEC-001 §4.2–§4.6 與 §8 的 JSON 範例，不增不減。`ContentJob.status` 為 §5.1 的 23 個狀態列舉。`actual_cost_usd` 允許 `"unknown"` 字串以外的數值型別，兩者都要能序列化回檔案。
- **狀態機**: §5.2 轉移表逐條實作。`PUBLISHED` 沒有任何入邊——不是靠檢查擋，是轉移表裡根本沒有通往它的路徑。`RETRYABLE_FAILED`、`MANUAL_ACTION_REQUIRED`、`BUDGET_EXCEEDED`、`CANCELLED` 是可從多個來源狀態進入的橫切狀態，依 §5.2 最後四列實作。
- **錯誤分類**: retryable = 網路錯誤、429、provider timeout。非 retryable = schema 錯誤、檔案格式錯誤、權限錯誤、預算超標、未授權素材（§5.3）。
- **預算閘門**: 判定式完全照 §10：`actual_cost_usd + estimated_cost_usd > budget_limit_usd` 即轉 `BUDGET_EXCEEDED` 且不呼叫 provider。閘門必須在呼叫發生**之前**執行，測試要能證明 provider 真的沒被呼叫（用 mock 斷言呼叫次數為 0），而不只是斷言狀態變了。
- **idempotency key**: 格式照 §4.6 範例 `<content_job_id>:<scene_id>:<operation>:attempt-<n>`。同 key 的第二次記錄不產生第二筆計費。
- **Postiz**: `create_draft` 回傳形狀照 §6.4。draft-only 是建構層強制——任何帶 `publish_now`、`auto_upload=true` 或非 `draft` 狀態的請求在送出 HTTP 前就被拒絕，不依賴伺服器端拒絕。HTTP 用 `requests.Session`，可從建構子注入以便測試。
- **第三方/整合**: Postiz（本次僅 mock session 測試，不打真實 endpoint）。無 LLM 呼叫、無影片 provider 呼叫。
- **安全/權限**: ProviderEvent 的 `request_summary`/`response_summary` 必須過濾憑證——測試要包含「傳入含 Authorization header 的 payload，斷言摘要中不含該值」。Postiz token 從設定讀取，不進 log、不進 job 檔案、不進 `repr`。job 目錄路徑必須經 `os.path.realpath` 正規化並確認在 `storage/jobs/` 之下，拒絕 `..` 逃逸與絕對路徑注入。`content_job_id` 必須是不含路徑分隔符的 opaque token。
- **邊界/效能**: JSONL 以 append-only 寫入，不做重寫。單一 job 的 scene 數上限 10（§4.4 對應 8–10 scene 設計），assets 數量無硬上限但單檔讀取需可容納於記憶體——V0 規模下不做串流。並發：V0 單使用者，不實作跨行程鎖；`JobStore` 的寫入以 `os.replace` 保證單檔原子性。

## Testing Decisions

| Module | 要測? | 測什麼外部行為 | Prior art（既有同類測試） |
|---|---|---|---|
| `app/models/content_job.py` | ✅ | 合法 payload 通過並保留全部欄位；每個 model 至少一個缺欄位／型別錯誤的拒絕案例；狀態列舉拒絕未知值 | `test/services/test_creator_profile.py` |
| `app/services/jobs/store.py` | ✅ | create→load→save→load 後內容逐欄相同（round-trip）；JSONL append 後讀回順序正確；路徑逃逸被拒；job 目錄不存在時 load 拋明確錯誤 | `test/services/test_creator_profile.py`、`test/services/test_config.py` |
| `app/services/jobs/state_machine.py` | ✅ | §5.2 每條合法轉移成功；代表性非法轉移被拒並含來源/目標狀態；`PUBLISHED` 從任何狀態皆不可達；錯誤分類三類各有案例；每次轉移在 `decisions.jsonl` 留一筆 | `test/services/test_provider_policy.py` |
| `app/services/jobs/budget.py` | ✅ | 未超標放行；超標時轉 `BUDGET_EXCEEDED` **且 provider mock 呼叫次數為 0**；重複 idempotency key 不產生第二筆計費；`actual_cost_usd` 缺漏記為 `unknown` 非 0；摘要不含憑證 | `test/services/test_provider_policy.py`、`test/services/test_loomloom.py` |
| `app/services/jobs/postiz.py` | ✅ | mock session 下建立 draft 並回傳 §6.4 形狀；`publish_now`／`auto_upload=true`／非 draft 狀態被拒且未發出 HTTP 請求；HTTP 失敗時拋錯不回傳假 draft_id；token 不出現在例外訊息或 repr | `test/services/test_loomloom.py`（mock session + 錯誤分類慣例） |

測試風格對齊既有 `test/services/`：pytest、明確 arrange/act/assert、mock 以建構子注入而非 monkeypatch 全域。既有 602 passed / 11 skipped / 4172 subtests 必須全程保持綠。

## Vertical Slices

### Slice 1 — Job 資料契約與檔案儲存
- **Type**: AFK
- **Blocked by**: None
- **User stories**: #1, #2, #3, #4, #5
- **Acceptance criteria**:
  - [ ] `app/models/content_job.py` 定義 `ContentJob`、`Script`、`Scene`、`AssetRecord`、`ProviderEvent`、`UsageLedgerEntry`、`RenderManifest` 七個 pydantic model，欄位與 SPEC-001 §4.2–§4.6 與 §8 的 JSON 範例逐欄一致
  - [ ] `ContentJob.status` 為涵蓋 SPEC-001 §5.1 全部 23 個狀態的列舉，未知值被拒絕
  - [ ] 每個 model 至少有一個合法通過案例與一個缺欄位／型別錯誤的拒絕案例，錯誤訊息指出欄位名
  - [ ] `app/services/jobs/store.py` 的 `JobStore` 提供 create／load／save／append_event／append_decision，目錄結構為 `storage/jobs/<content_job_id>/` 且檔名照 Implementation Decisions
  - [ ] create→load→save→load round-trip 後所有欄位值相同，無欄位遺失
  - [ ] JSONL append 後依寫入順序讀回
  - [ ] 含 `..`、路徑分隔符或絕對路徑的 `content_job_id` 被拒絕，不在 `storage/jobs/` 外建立任何檔案
  - [ ] 建立凍結 fixture `test/fixtures/jobs/three-scene-demo/` 與 `test/fixtures/jobs/ten-scene-demo/`，兩者皆為通過 schema 的完整 job 目錄
  - [ ] 既有 602 tests 仍全綠

### Slice 2 — Job 狀態機與決策紀錄
- **Type**: AFK
- **Blocked by**: Slice 1
- **User stories**: #6, #7, #8, #9, #10
- **Acceptance criteria**:
  - [ ] `app/services/jobs/state_machine.py` 實作 SPEC-001 §5.2 全部轉移條目，每條合法轉移各有一個通過測試
  - [ ] 代表性非法轉移（例如 `DRAFT`→`RENDERING`、`AWAITING_ASSETS`→`TECHNICAL_QA`）被拒絕，錯誤訊息含來源與目標狀態
  - [ ] `PUBLISHED` 從任一狀態皆不可達，且有一個測試逐一嘗試所有來源狀態證明之
  - [ ] `RETRYABLE_FAILED`／`MANUAL_ACTION_REQUIRED`／`BUDGET_EXCEEDED`／`CANCELLED` 可從 §5.2 定義的多個來源狀態進入
  - [ ] `classify_error` 將網路錯誤／429／timeout 分類為 retryable，將 schema／格式／權限／預算／未授權素材分類為非 retryable，各有測試
  - [ ] 每次成功轉移在該 job 的 `decisions.jsonl` 追加一筆含 from／to／reason／timestamp 的紀錄
  - [ ] 狀態機為純函式，不直接做檔案 I/O（寫入由 `JobStore` 負責）
  - [ ] 既有 602 tests 仍全綠

### Slice 3 — Budget Guard 與成本帳本
- **Type**: AFK
- **Blocked by**: Slice 2
- **User stories**: #11, #12, #13, #14, #15, #16
- **Acceptance criteria**:
  - [ ] `app/services/jobs/budget.py` 的 `check_budget` 實作 SPEC-001 §10 判定式，未超標放行、超標拒絕
  - [ ] 超標時 Job 轉為 `BUDGET_EXCEEDED`，且測試以 mock 斷言 provider 被呼叫次數為 0
  - [ ] 每次呼叫寫一筆 `ProviderEvent` 與一筆 UsageLedger 紀錄，欄位照 SPEC-001 §4.6
  - [ ] `build_idempotency_key` 產生 `<content_job_id>:<scene_id>:<operation>:attempt-<n>` 格式；相同 key 的第二次記錄不新增計費筆數
  - [ ] provider 未回報實際成本時 `actual_cost_usd` 記為 `unknown`，非 0，且估算來源保留
  - [ ] 傳入含 Authorization header／API key 的 payload 時，`request_summary`／`response_summary` 不含該值，有專門測試
  - [ ] 既有 602 tests 仍全綠

### Slice 4 — Postiz Draft Adapter
- **Type**: AFK
- **Blocked by**: Slice 3
- **User stories**: #17, #18, #19
- **Acceptance criteria**:
  - [ ] `app/services/jobs/postiz.py` 的 `PostizPublisher.create_draft` 在 mock session 下回傳 SPEC-001 §6.4 形狀（`provider`／`draft_id`／`status`／`platform`／`scheduled_at`）且 `status == "draft"`
  - [ ] 帶 `publish_now`、`auto_upload=true` 或非 `draft` 狀態的請求被拒絕，且測試斷言 mock session 未發出任何 HTTP 請求
  - [ ] HTTP 非 2xx 或回應缺 draft_id 時拋出明確錯誤，不回傳假的 draft_id
  - [ ] 成功時 Job 由 `POSTIZ_DRAFTING` 轉為 `POSTIZ_DRAFTED` 並寫回 draft_id；失敗時停在 `POSTIZ_DRAFTING` 並記錄錯誤
  - [ ] Postiz token 不出現在例外訊息、log 或物件 `repr` 中，有專門測試
  - [ ] `requests.Session` 由建構子注入，預設自建
  - [ ] 既有 602 tests 仍全綠

## Out of Scope

- 腳本生成（LLM 呼叫）與 Scene Planner——`config.toml` 目前無任何已設定的 LLM API key，驗收無法完成（PLAN-001 Q5 實測）。
- Master Voice 生成、字幕生成、Asset Import 流程、Render Manifest builder、渲染與 ffprobe QA、`run --job` 端到端 orchestrator。這些是 PLAN-001 的 issue #4–#9、#11，本次不做。
- Phase 3 的 5 支 POC——那是人工營運驗收，不進 autonomous loop（PLAN-001 Q6）。
- 任何對 `app/services/task.py` 的修改。
- 任何真實 Postiz endpoint 呼叫；本次僅 mock。
- SQLite 或任何資料庫。
- Cloudflare 相關 runtime（SPEC-001 §14 明定 V1 才評估）。
- 自動 image／video provider 呼叫；V0 全走人工匯入。
- HTTP API endpoint 與 WebUI 介面接入。

## Further Notes

- **渲染路徑已由 spike 定案**（PLAN-001 Q5 實測，2026-08-26）：`combine_videos()` 不依賴 `VideoParams`，輸出實測 `h264 / 1080x1920 / 30fps`；音訊在 `generate_video()` 那層，後者僅使用 `font_name`／`font_size`／`stroke_width`／`subtitle_enabled`／`text_background_color`／`video_aspect` 六個欄位。本次不實作渲染，但 `RenderManifest` 的欄位設計必須能餵出這六個值，以免 issue #9 時回頭改契約。
- **契約漂移是本次最大風險**（PLAN-001 Q5 風險 3）：module path 與 fixture path 已在 Modules 與 Slice 1 中寫死，後續 slice 一律引用 Slice 1 建立的 fixture，不得自建第二套測試資料。
- 本 repo 是 `harry0703/MoneyPrinterTurbo` 的 fork，未來仍會 merge 上游。所有新增碼落在新目錄，避免動到上游檔案。
- 專案為 Python，無 Node `package.json` 亦無 Composer；push-gate 的 full-test 應以 `.venv/bin/python -m pytest test -q` 為準，基準為 602 passed / 11 skipped / 4172 subtests。
- Commit 身份固定 `zhenheco <ace@zhenhe-dm.com>`。
- 本次不 ship：跑到 push-gate 後停下回報，由人決定是否推送與發 PR。
