# Handoff：V0 Job Pipeline

> 最後更新 2026-08-28。接手前先跑「驗證現況」那一節，不要相信這份文件的任何斷言 —— 它可能已經過時。

## 1. 現在在哪

V0 job pipeline 的相關合併：

| PR | commit | 內容 |
|---|---|---|
| #5 | `407d78d` | PLAN-001 issue #1 #2 #3 #10：資料契約、狀態機、預算閘門、Postiz draft adapter |
| #6 | `864963e` | issue #4：建立 Job + Script JSON 生成 |
| #7 | `ac8b15b` | 修復 issue #4 交付時未被發現的破口（見 §3，**這一條最重要**） |
| #8 | `084aa0d` | 上一版 handoff |
| 本次 | — | issue #5：Scene Planner + Generation Manifest |

PLAN-001 的 11 張 issue **做完 6 張**：#1 #2 #3 #4 #5 #10。

全部落在平行路徑 `app/services/jobs/`，**`app/services/task.py` 至今零修改**（上游熱區，侵入式修改會讓每次 merge upstream 都衝突 —— 見 PLAN-001 Q2）。

| 模組 | 職責 |
|---|---|
| `app/models/content_job.py` | SPEC-001 §4.2–§4.6、§8 的 pydantic 契約 + `JobStatus`（23 狀態） + §6.1 `GenerationManifest` |
| `app/services/jobs/store.py` | job 目錄檔案儲存，JSON 單檔 + JSONL append-only + scene 匯入目錄 |
| `app/services/jobs/state_machine.py` | §5.2 的轉移表 + §5.3 錯誤分類，純函式無 I/O |
| `app/services/jobs/budget.py` | §10 預算閘門 + 成本帳本 |
| `app/services/jobs/postiz.py` | §6.4 draft-only 發布器 |
| `app/services/jobs/pipeline.py` | issue #4：`create_job` / `start_scripting` / `generate_script` |
| `app/services/jobs/llm_adapter.py` | issue #4 修復：繞過上游 `llm.generate_script` 的 JSON 破壞（**必讀 §3**） |
| `app/services/jobs/scene_planner.py` | issue #5：`start_scene_planning` / `plan_scenes` + §6.1 generation manifest |

測試基準（2026-08-28 實測）：**1412 passed / 11 skipped / 4172 subtests**，`ruff check` 全綠。
（前一版基準是 1344；差額全部是 issue #5 新增的測試，沒有既有測試被改動。）

## 2. 驗證現況（接手第一件事）

```bash
cd ~/Documents/Claude\ Code\ Projects/MoneyPrinterTurbo
git fetch origin main
git log origin/main -1 --oneline
.venv/bin/python -m pytest test -q                    # 1412 passed / 11 skipped
.venv/bin/ruff check app cli.py main.py webui test    # All checks passed
```

**注意本地 checkout 可能落後或分歧。** 這在這台開發機上已經發生過兩次：本地 `main` 落後 origin 好幾個 commit，且帶著一個從未推上去的舊 handoff commit（`b5a411a`）。先 `git status` 與 `git log origin/main..HEAD` 確認，再決定 pull 或 reset。

CI 在 GitHub Actions（`ci.yml`）：Python 3.11 + 3.13 + Windows smoke。**Windows smoke 是逐檔列舉測試，目前沒有納入任何 job pipeline 測試**，而 `store.py` 正是全 repo 最吃 Windows 路徑語義的檔案。CI 也**沒有型別檢查**。

## 3. ⚠️ 最重要的一課：`llm.generate_script` 會摧毀 JSON

`app/services/llm.py` 的 `generate_script()` 內部 `format_response()` 有這兩行：

```python
response = re.sub(r"\[.*\]", "", response)   # llm.py:538
response = re.sub(r"\(.*\)", "", response)   # llm.py:539
```

貪婪比對，把**第一個 `[` 到最後一個 `]` 之間全部刪掉**。`Script` schema 有四個陣列欄位（`body` / `claims` / `sources` / `risk_flags`），所以任何合法的 Script JSON 都會被截成 `{"title": ..., "body": }`。

這對「純文字腳本」是刻意且正確的 markdown 清理；對 JSON 是致命的。

同一條路徑另外兩個坑：

- **從不拋例外**：`llm.generate_script` 失敗回傳 `"Error: ..."` 字串（`llm.py:572-576`）；`llm._generate_response` 也一樣（`llm.py:404-405`）。下游會把傳輸層錯誤誤判成 schema 錯誤。
- **內部自行重試 5 次**：`_max_retries = 5`（`llm.py:14,550`）。呼叫端過一次預算閘門，實際授權了最多 5 次真實付費呼叫。

### 現在的正確做法

`app/services/jobs/llm_adapter.py`：直接呼叫 `llm._generate_response`，偵測 `"Error: "` 哨兵與空回應並轉成 `LlmTransportError`（依訊息分類 retryable：timeout／連線／5xx 為 `True`，`api_key is not set`／401／403 為 `False`）。

**jobs pipeline 要拿結構化 JSON，永遠不要走 `llm.generate_script`。** 已有測試 `assert_not_called` 釘住這件事，不要繞過它。**也不要去改 `llm.py`** —— 上游熱區，改了會破壞既有 `task.py` 路徑並製造每次 merge 衝突。

### 這個 bug 是怎麼溜過去的（比 bug 本身更值得記住）

issue #4 交付時：57 個測試全綠、5 輪獨立審查收斂到 0 紅、mutation 概念上也覆蓋了。但它**上線就是壞的，真實呼叫 100% 失敗**。

原因只有一個：**每一個測試都 mock 掉 `llm.generate_script`**，所以那個函式從來沒有被執行過。驗收條件寫的是「從 topic 產出通過 schema 的 `script.json`」，但沒有任何一條測試真的從 topic 跑到 `script.json`。

連帶的錯誤：當時為了消除 PLAN-001 Q5 風險 2，量測了「Script schema 命中率 3/3」—— 但量的是 `llm._generate_response`，而 pipeline 走的是 `llm.generate_script`。**驗證了一個不會被用到的函式。**

修復後的測試把 mock 邊界下移到 **provider SDK client 層**（`patch("app.services.llm.OpenAI")`），讓 `_generate_response` 與 adapter 都真的跑過，只擋掉網路。目前有 3 個這種層級的測試，其中一個在舊實作下會紅。

**接手守則：mock 掛在哪一層，就等於宣告哪一層以下你不驗。跨越模組邊界的驗收條件，至少要有一條測試真的走完全程。**

## 4. 已交付 API 的坑（誤用會靜默毀資料）

`JobStore` 的兩個寫入方法**語意不同**：

```python
store.save(job: ContentJob)      # 只重寫 job.json — 狀態轉移後存回用這個
store.replace(record: JobRecord) # 全量取代，會刪除不在 record 內的 scene/script/manifest
```

`JobRecord(job=job)` 的預設值（`scenes=[]`、`script=None`）語意等同「刪光」，而 `create()` 內部正是用這個寫法 —— 照抄到 `replace()` 會清空整個 job。

狀態機：

```python
transition(job, to_status, reason, now="")   # reason 必須是非空字串，None/0/[] 都會被拒
decision_record(from_status, job, reason)    # 時間戳的 key 是 "at"，不是 "timestamp"
classify_error(exc)                          # 回傳物件，讀 .is_retryable / .value
```

`TRANSITIONS` 是唯讀 `MappingProxyType`，有窮舉 23×23 測試鎖住整張表，**多一條或少一條邊都會失敗**。

預算：

```python
check_budget(job, estimated_cost_usd, store=None)  # 超標 raise BudgetExceededError
record_usage(store, job, event, ...)               # 寫 ledger + event，並回寫 job.actual_cost_usd
```

**`check_budget` 傳了 `store` 時會自己從磁碟重讀 job**，不信任呼叫端傳進來的物件。

**`SCRIPT_LLM_CALL_COST_CEILING_USD = 0.05`（`pipeline.py`）是保守佔位天花板**，不是實際計價。SPEC 預設 `budget_limit_usd = 3` 之下等於約 60 次等價呼叫。`llm.py` 沒有提供單價或 token 用量，所以每筆 `ProviderEvent.actual_cost_usd` 都是 `"unknown"`，`job.actual_cost_usd` 100% 由這個天花板估算堆成，且 `job.json` 上沒有標記說明這是估算值。接真實計價時改這裡，**永遠不要改回 0 或 unknown sentinel**。

`redact()` 只套用在 **provider 控制的欄位**（`model`、`request_id`、`external_job_id`、兩個 summary、`error_class`）。本地產生的識別碼（`content_job_id`、`scene_id`、`idempotency_key`）**不可以** redact —— 曾經因為這個把 idempotency key 打成 `<redacted>`，去重永遠比對不上、重複計費。

`generate_script()` 的重試語意：attempt 編號**從既有 ledger 推導**（解析 idempotency key），跨呼叫合計上限 2 次；已有 `script` 時直接回傳（冪等短路）；狀態守衛**重讀 store**，不看記憶體裡的 job 物件。

Scene Planner（issue #5）：

```python
start_scene_planning(job, store)   # SCRIPTING -> SCENE_PLANNING，寫 decisions.jsonl
plan_scenes(job, store)            # 8~10 個 Scene + 匯入目錄 + generation_manifest.json
```

- **兩者都重讀 store**，不信任傳進來的 `ContentJob`。傳一個過期的物件不會重跑一個階段，會拿到 `ScenePlanError`。
- **`plan_scenes` 完全不呼叫 provider、不動預算。** 輸出是 Script 與 job 上限的純函式，同一份 script 永遠規劃出同一組 scene —— 這是需求不是巧合：重跑若洗牌，人工已經照舊 manifest 生好的素材就對不上了。
- **冪等但會補寫，而且「已有 scenes」不等於「規劃完成」。** 因為規劃是純函式，job 還在 `SCENE_PLANNING` 時 `plan_scenes` 會**重算整份計畫並逐項比對**磁碟上的 scene；相符才短路，不符就重建。**只數檔案不夠** —— 一個 10 場景計畫崩到剩 8 個檔案，數量本身完全合理。過了 `SCENE_PLANNING` 之後計畫凍結、不再重算，但仍會檢查結構不變式（8~10、`scene_index` 連續、id 唯一、歸屬相符）；壞掉就報錯，不會拿殘骸去產一份只有一個 entry 的 manifest。匯入目錄每次補齊；**manifest 每次都跟 scene 逐欄比對**（只豁免 `created_at`），不一致就重寫 —— 不是比對「這次有沒有重建」，因為 scene 寫完、manifest 還沒寫就崩的話，下一次看到的 scene 已經跟腳本一致了。
- **`visual_prompt` 的 script 片段都過 `budget.redact()`。** PRD-001 FR-004A 與 SPEC-001 §12 / §14 用同一句話寫死：`secret、credential 不得寫入 log、audit 摘要或 prompt`。Script 是模型從使用者主題產生的文字，夾帶憑證是真的會發生。**只過 script 片段，不過整串** —— `scene_id` 與固定標籤是本地識別碼，這個 repo 已經因為把識別碼丟進憑證過濾器而炸過一次（job id 含 `session` → idempotency key 變 `<redacted>` → 重複計費）。`Scene.narration` 也不過濾：那是 voice 階段要念的文字，不是 prompt。
- **`plan_scenes` 不會把「已停在 `MANUAL_ACTION_REQUIRED` 的舊計畫」交回去。** `_FROZEN_PLAN_STATUSES` 只列規劃成功之後的階段，不含失敗／人工／預算／取消狀態；被 park 的 job 帶著舊 scenes 再呼叫會直接報錯，而不是若無其事重發一份 manifest 把該狀態蓋掉。
- **manifest 的 `accepted_mime_types` 每種副檔名只列一個 MIME。** SPEC §7 是「MIME sniffing 與副檔名雙重驗證」，所以不能一邊要求檔名 `scene-001.png`、一邊宣告接受 `image/jpeg` —— 那是叫操作者產出匯入階段會拒收的檔案。要放寬就得連檔名規則一起放寬。
- **改 `script.json` 再跑 `plan_scenes` 就是 replan**（前提是 job 還在 `SCENE_PLANNING`）：scene 與 manifest 一起換新。**但已經人工匯入的素材不會被動**，而 scene id 會被重用 —— 也就是說 `scenes/scene-003/images/` 裡的舊圖會留在原地，卻對應到新的 narration。改稿重跑之後要自己清或重生受影響的素材。issue #8 接匯入驗證時要考慮這件事。
- **`generation_manifest.json` 與 `scenes/<scene_id>/<kind>/` 刻意不在 `JobRecord` 裡**，所以 `replace()` 永遠刪不到它們（那些目錄可能已經有人工放進去的檔案）。走 `store.write_generation_manifest()` / `store.scene_media_dir()`，**不要**把它們塞進 `JobRecord`。
- 匯入目錄是 **SPEC-001 §3.2 的形狀**：`scenes/{scene_id}/images/` 與 `scenes/{scene_id}/videos/`，依 `visual_type` 決定。PLAN-001 Q9 明文指名這個路徑。注意 `store.py` 其餘的目錄配置（扁平的 `scene-NNN.json`、沒有 `audit/`）**本來就跟 §3.2 不一致**，那是 issue #1 的既有偏差，#5 沒有擴大也沒有修它。
- manifest 的 `import_dir` 是**相對於 job 目錄**的 POSIX 路徑（`scenes/scene-001/images`）。PLAN-001 Q9 原本寫「印出絕對路徑」，**已於 2026-08-28 改成相對路徑並在該檔記錄原因**：manifest 是會跟著 job 目錄搬的持久檔，絕對路徑一換機器就失效。要給人看的絕對路徑由印出的那一層自己接。
- `store.scene_media_relative_dir()` 什麼都不建立，但它產生的字串會進 manifest，所以它跟 `scene_media_dir()` **做同一套 scene_id 與 kind 驗證**。新增呼叫端時不要繞過它自己拼字串。
- **manifest 的 `created_at` 是「這份 manifest 何時被寫出」，不是決定性的。** 同一個 job 正常只會寫一次，但 manifest 被刪掉後重跑會補一份新的、帶新時間戳。scene 內容仍然完全決定性；不要拿整份 manifest 做 byte 比對當回歸測試，要比就比 `entries`。
- `generated_video` 名額 = `min(job.max_generated_video_scenes, 3, body scene 數 // 3)`，取 narration 最長的 body scene，同長度時取 index 小的。**上限是天花板不是配額**，所以典型 V0 job 只會拿到 1~2 個 AI 影片。

凍結 fixture：`test/fixtures/jobs/three-scene-demo/`、`ten-scene-demo/`。後續 slice 一律對這兩組驗收，**不要自建第二套測試資料**。但注意：**這兩組 fixture 沒有任何媒體 bytes**，`sha256` 是 `0001`~`0010` 的流水佔位值，issue #8 / #9 的驗收需要真實素材時要另外處理。另外，**這兩組 fixture 的 `scenes/` 是手寫的，不是 planner 產出的** —— 拿它們的 script 跑 `plan_scenes` 都會得到 8 個 scene，而 `ten-scene-demo` 的目錄裡是 10 個。不要把兩者當成同一回事。

## 5. 需要人拍板的規格缺口（擋 issue #11）

SPEC-001 §5.2 的轉移表有洞。實測 `app/services/jobs/state_machine.py` 的 `TRANSITIONS`：

- **沒有任何一條邊指向 `FAILED`** —— 失敗的 job 永遠不會終結
- **`MANUAL_ACTION_REQUIRED` 是單向黑洞**，唯一出邊是 `CANCELLED` —— 人補完素材後無法回到流程
- **`BUDGET_EXCEEDED` 的出邊只有 `CANCELLED` 與 `MANUAL_ACTION_REQUIRED`** —— 兩條都不通往生成階段，所以提高預算也救不回 job
- `RESEARCHING` / `IMAGE_GENERATING` / `VIDEO_GENERATING` 三個階段狀態**入邊為 0**

但 §5.3 要求「單一 AI 影片 Scene 最多重試一次，第二次失敗後轉成 image_motion fallback」—— 狀態機沒有路徑回到失敗的階段。

**這個缺口現在是承重的。** `pipeline.py` 的 `_persist_failed_status()` 對 retryable 失敗**刻意不轉移狀態**（留在 `SCRIPTING`），因為 `RETRYABLE_FAILED` 回不到生成階段，轉過去會讓可重試的失敗變成不可恢復。程式碼裡有註解標明這是產品決定。

**動 issue #11 之前必須先補 §5.2，這是產品決定，不要讓 agent 自己發明邊。** 補邊必須同步改規格、狀態機與那組 23×23 窮舉測試。

## 6. 剩下的 issue

| # | 標題 | 大小 | 卡在哪 |
|---|---|---|---|
| 6 | Master Voice + 時間軸 | M | **無阻塞，可立刻開工**（#5 已完成） |
| 7 | 字幕生成 | S | 依賴 #6 |
| 8 | Asset Import + Creator Profile preflight | L | **無阻塞**（#5 已完成） |
| 9 | Render Manifest + Renderer + ffprobe QA | L | 依賴 #6 #7 #8 |
| 11 | `run --job` 端到端 + golden fixtures | M | 依賴全部 + §5 的規格缺口 |
| — | Phase 3 POC 操作 runbook（PLAN-001 Q6 提到的 S 號 docs issue） | S | 無阻塞，尚未建立 |

**#4 與 #5 之間那段斷點已補上**：`start_scene_planning()` 把 `SCRIPTING` 推進 `SCENE_PLANNING` 並寫 `decisions.jsonl`。**#5 與 #6 之間現在是同一種斷點**：`plan_scenes()` 結束後 job 停在 `SCENE_PLANNING`，沒有任何程式碼把它推進 `VOICE_GENERATING`（這條邊在轉移表裡合法）。#6 開工第一件事要補這一步 —— 照 `start_scene_planning` 的形狀寫（重讀 store、檢查前置文件存在、transition + save + append_decision）。

**#8 需要的東西 #5 已經備好**：每個 scene 的匯入目錄（`scenes/{scene_id}/images/` 或 `videos/`，SPEC §3.2 形狀）與 `generation_manifest.json` 的 `import_dir` / `expected_filename` / `accepted_mime_types`。#8 的驗證應該對照 manifest，不要另外定義一套檔名或路徑規則。

**LLM API key 不再是阻塞。** DeepSeek 已接線並實測可用：`llm_provider = "deepseek"`、`deepseek_base_url = "https://api.deepseek.com"`、`deepseek_model_name = "deepseek-v4-pro"`，key 從 `op://Dev/DEEPSEEK_API/credential` 取出寫進 `config.toml`（該檔在 `.gitignore` 第 2 行）。

修復後的真實端到端實跑（2026-08-27，兩個不同主題各一次）：從 topic → 一次 provider 呼叫 → 通過 `Script` schema 的 `scripts/script.json` 落地 → job 停在 `SCRIPTING`、1 筆 ProviderEvent、1 筆 UsageLedger、`actual_cost_usd = 0.05`。單次呼叫延遲實測 **42–57 秒**，不要沿用「LLM 呼叫是秒級」的假設。

**注意 `app/config/config.py` 只對 `REDIS_HOST` 讀 env，LLM key 沒有 env override**，所以不能用 `op run` 注入，只能落在 gitignored 的 `config.toml`。CI 或另一台機器要跑真實 LLM 驗收時會踩到這點。

**issue #9 的渲染路徑已由 spike 定案**（PLAN-001 Q5 實測，不必重新探索）：

- `combine_videos()` 不依賴 `VideoParams`，可由 RenderManifest 直接餵；輸出實測 `h264 / 1080x1920 / 30fps`
- 但它**只產 video stream，沒有音訊** —— 音訊／字幕／BGM 在 `generate_video()` 那層
- `generate_video()` 雖吃 `VideoParams`，實際只讀 6 個欄位：`font_name`、`font_size`、`stroke_width`、`subtitle_enabled`、`text_background_color`、`video_aspect`
- 所以 #9 = manifest → `combine_videos`（視覺）→ 由 manifest 建最小 `VideoParams` → `generate_video`（音訊+字幕）→ ffprobe QA

## 7. 已知限制（審查有案，刻意未修）

- **`app/services/jobs/` 在 production 端零呼叫者** —— 只有 pytest 進得去。SPEC §9 的 7 個入口（create / plan-assets / import-assets / render / qa / upload / postiz-draft / `run --job`）一個都沒接，`cli.py` 沒有任何 job 相關 subcommand。issue #4 範圍字面上寫了「CLI/API create」，但交付的只有 `pipeline.create_job()` 這個 Python 函式。
- **`create_job` 把 `creator_profile_id` 硬填空字串**，而 SPEC §3.1 的輸入契約根本沒有這個欄位。每個 V0 job 都無法連回 consent 記錄，這會擋到 #6 / #8 / #9。
- **`ContentJob.estimated_cost_usd` 永遠是 0**：建立時寫死，之後沒有任何地方更新它。
- **`ProviderEvent` 的 `request_id` / `external_job_id` 一律空字串** —— LLM 呼叫無法對回 provider 端紀錄。
- **`postiz` 完全繞過 `record_usage`**（0 次呼叫、2 次直接 `append_event`）—— 沒有去重、不寫 ledger、不回寫花費。草稿成本是 0 所以目前無害，但兩條寫入路徑的保證不同，加新欄位時容易漏。同一 attempt 重播會在 Postiz 上產生第二份草稿。draft_id 也只活在 `provider_events.jsonl` 的 `external_job_id`，沒寫回 job。
- **`ContentJob.content_job_id` 與 `Scene.scene_id` 本身無格式驗證** —— 驗證只在 `JobStore` 層。繞過 store 直接建構含冒號的物件交給 `create_draft`，`build_idempotency_key` 會在 HTTP **送出之後**才拋錯，產生 orphan draft。V0 無此呼叫路徑，但**接 CLI/API 層時很容易踩到**。
- `JobStore.create()` 非原子：mkdir 成功後若寫檔失敗，會留下沒有 `job.json` 的半成品目錄，重試 `create()` 會被「already exists」擋死。issue #4 之後它進了活的呼叫路徑。
- `JobStore` 沒有 `DEFAULT_ROOT` 常數，`storage/jobs` 只出現在 docstring，實際路徑靠呼叫端自律。
- `_utc_now()` 仍有三份逐字複製（`postiz.py`、`state_machine.py`、`pipeline.py`），格式一致但沒有測試釘住。issue #5 沒有再複製第四份：`state_machine` 的那份已改成公開的 `utc_now()`（`_utc_now` 留成別名），`scene_planner` 直接引用它。**另外兩份沒有動** —— 那是本次範圍外的重構。
- **Scene Planner 是純詞法切分，沒有語意理解。** 場景邊界只看句號與逗號，`visual_type` 只看 `semantic_purpose` 與 narration 長度，`visual_prompt` 是樣板字串。產出的 prompt 可以直接拿去生圖，但**不會比腳本本身更聰明**；要更好的分鏡就得引入一次 LLM 呼叫，那會連帶需要預算閘門與 ProviderEvent（目前完全沒有）。
- **太瘦的腳本會被從中間硬切，narration 可能斷在詞組中間。** 切分優先用句號、再用逗號；標點用完還湊不到 8 個場景時，最後手段是**從最接近中點的可用位置切**（兩半都要 ≥6 字且都要有非空白字元）。實測 body 只有一段的腳本會產出 `第 1 個問` / `題是節奏太平，` 這種切法。取捨理由是 PLAN-001 row 5 的驗收條件寫「8~10 scene 恆成立」，而硬切是無損的（narration 拼回來等於原腳本，有測試釘住），人改稿重跑就能修好。**#6 / #7 要知道 narration 可能是半個詞組。** 「無損」的精確意思是**在句子邊界的前後空白被正規化之後**相等：`_sentences` 會 strip 每一段，所以 `hook` 前後多打的空白不會被保留。這是刻意的 —— 不 strip 的話一整串空白會自己變成一個空白場景。
- 真的短到連 8 段 ≥6 字都湊不出來時才 `ScenePlanError`，而且 **job 會被轉成 `MANUAL_ACTION_REQUIRED`**（`SCENE_PLANNING → MANUAL_ACTION_REQUIRED` 在 §5.2 是合法邊，`classify_error` 把 `ValueError` 歸為 non-retryable，形狀跟 `pipeline._persist_failed_status` 一致）。**這裡曾經寫錯過**：初版註解宣稱「§5.2 沒有從 SCENE_PLANNING 出去的失敗邊」，實測 `TRANSITIONS[SCENE_PLANNING]` 是 `BUDGET_EXCEEDED / CANCELLED / MANUAL_ACTION_REQUIRED / RETRYABLE_FAILED / VOICE_GENERATING`。要引用轉移表就去跑它，不要憑印象。
- **replan 只在 `SCENE_PLANNING` 內有效，而且不會清理舊素材。** 一旦 job 進到 `VOICE_GENERATING` 之後才改 `script.json`，`plan_scenes` 不會（也不該）重新分鏡。即使在 `SCENE_PLANNING` 內 replan，scene id 會被重用而既有匯入檔案原封不動，所以舊素材會對應到新旁白。issue #11 若需要「改稿後重跑並自動失效舊素材」，那是另一條要明確設計的路徑。
- 從中間切開的 scene，narration 會以「，」結尾（例如 `錯誤三：沒有驗收標準，`）。對 TTS 是合法的停頓，但**不要**在後續階段順手 strip 掉 —— 那會讓 narration 不再能拼回原腳本。
- `budget.redact()` 只認得有標記或有固定前綴的憑證形狀，**裸 hex/UUID token 認不出來**。`postiz` 因此另外用自己知道的 token 值做明確比對（`_scrub`）。新增 provider adapter 時要沿用這個做法。
- 去重是「先讀後寫」，兩個並行程序可各自通過檢查。V0 單程序檔案儲存，無此情境。
- **SPEC §12 的 Contract 測試分類是零**，§6 的 Provider Protocol 一行都沒有，而且沒有任何 issue 擁有它。
- **§12 要求 6 組 golden fixture，只存在 2 組**：缺 `missing-asset`、`video-provider-timeout`、`budget-exceeded`、`render-failure`。
- **SPEC §13 Phase 0 的核准 gate 從未關閉**：PRD-001 仍標「Draft，等待使用者核准」、SPEC-001 仍標「Draft，等待 PRD-001 核准」、ADR-001 仍是 Proposed，但 6 張 issue 的 code 已經合併進 main。

## 8. 環境

- **兩個既有 flaky 測試**（非本次造成）：`test/services/test_webui_startup.py::TestWebuiStartup::test_external_directory_prefers_project_app_package`（Streamlit AppTest 30 秒 timeout）、`test/services/test_video.py::TestVideoService::test_preprocess_video`。高負載時偶發失敗，低負載連跑多次全綠。**不要把它們的失敗當成自己的回歸** —— 但也不要因此忽略真回歸。
- **branch protection 仍未設定。**
- **`docker-ghcr.yml` 每次 push main 都會紅** —— 沿用上游 GHCR namespace，這個 fork 沒有推送權限。要嘛換 namespace，要嘛刪掉這個 workflow。
- 測試中的憑證佔位值**不得含** `budget._CREDENTIAL_WORDS` 裡的字（`token`、`key`、`secret`、`bearer`、`cookie`、`session`），否則通用 pattern 會命中，測試在未修復的碼上也會通過。逼真的 base64/`sk-` 形狀會被 pre-commit 的 gitleaks 擋下 commit。
- repo 與 CI **沒有 secret scan**；gitleaks 只是開發機的 global hook。

## 9. 給下一個接手的人：四個教訓

這批程式碼每張 issue 都經過獨立 reviewer 多輪審查。以下是那些流程**沒抓到**、靠別的方式才發現的：

1. **mock 掛太高，等於宣告那一層以下都不驗。** issue #4 的核心驗收條件在真實呼叫下 100% 失敗，卻通過了 57 個測試與 5 輪 0 紅審查 —— 因為所有測試都 mock 掉了那個會摧毀資料的函式。**跨模組邊界的驗收條件，至少要有一條測試真的走完全程。**

2. **量測要對準實際會被呼叫的那一層。** 「Script schema 命中率 3/3」量的是 `_generate_response`，但 pipeline 走的是 `generate_script`。數字沒錯，結論全錯。

3. **mutation testing 證明不了「少了一整步」。** 預算閘門在這個 repo 已經以**四種不同形式**失效過：估算恆為 0、沒有人回寫已花費金額、重試時 idempotency key 碰撞導致不計費、上游函式內部自己重試 5 次。每一次單元測試都全綠。**寫驗收測試時要問「這條路真的有人走完嗎」，不只是「這個函式對嗎」。**

4. **合理的論證不等於正確的論證。** 修憑證外洩時採用了「統一讓所有欄位過 redact」這個聽起來更一致的做法，結果把合法 job id 的 idempotency key 吃掉、去重失效、重複計費。**聽起來合理的說法要實測，尤其是它推翻了某個 per-case 處理的時候。**

5. **「冪等」的短路條件必須涵蓋每一次寫入，而且要判斷「完整」不是「存在」。** issue #5 的 `plan_scenes` 一開始用「已有 scenes 就直接回傳」當冪等，但它其實做多次獨立寫入（每個 scene 一個檔案 → 匯入目錄 → manifest）。崩在中途重跑會短路，manifest 永遠補不回來；更糟的是 `JobStore.replace` 崩在寫檔迴圈中間會留下 scene 前綴，那個前綴會被當成「已規劃」，產出少於 8 個 entry 的 manifest 並讓 job 永久卡住。兩個缺陷的測試都會全綠，因為沒有一條測試模擬過中途崩潰。**多步驟寫入的冪等要用「缺什麼補什麼」，短路條件要驗完整性（數量、連續性、歸屬），並且要有測試真的把中間產物刪掉再重跑。**（第二個缺陷是獨立 reviewer 抓到的，不是測試抓到的。）
