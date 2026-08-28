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
| #9 | `82abd8a` | issue #5：Scene Planner + Generation Manifest |
| #10 | `414c6fc` | CI：Windows smoke 納入 job pipeline 測試 |
| #11 | `f407192` | issue #6：Master Voice + 時間軸 |
| #12 | `35235c1` | issue #7：由時間軸產字幕 |
| 本次 | — | SPEC-001 §5.2 補邊 65→76 + `resume_target` 返回目標推導（見 §5） |

PLAN-001 的 11 張 issue **做完 8 張**：#1 #2 #3 #4 #5 #6 #7 #10。

全部落在平行路徑 `app/services/jobs/`，**`app/services/task.py` 至今零修改**（上游熱區，侵入式修改會讓每次 merge upstream 都衝突 —— 見 PLAN-001 Q2）。

| 模組 | 職責 |
|---|---|
| `app/models/content_job.py` | SPEC-001 §4.2–§4.6、§8 的 pydantic 契約 + `JobStatus`（23 狀態） + §6.1 `GenerationManifest` |
| `app/services/jobs/store.py` | job 目錄檔案儲存，JSON 單檔 + JSONL append-only + scene 匯入目錄 |
| `app/services/jobs/state_machine.py` | §5.2 的轉移表 + §5.3 錯誤分類 + `resume_target` 返回目標推導，純函式無 I/O |
| `app/services/jobs/budget.py` | §10 預算閘門 + 成本帳本 |
| `app/services/jobs/postiz.py` | §6.4 draft-only 發布器 |
| `app/services/jobs/pipeline.py` | issue #4：`create_job` / `start_scripting` / `generate_script` |
| `app/services/jobs/llm_adapter.py` | issue #4 修復：繞過上游 `llm.generate_script` 的 JSON 破壞（**必讀 §3**） |
| `app/services/jobs/scene_planner.py` | issue #5：`start_scene_planning` / `plan_scenes` + §6.1 generation manifest |
| `app/services/jobs/master_voice.py` | issue #6：`start_voice_generating` / `generate_master_voice` + 時間軸文件 |
| `app/services/jobs/voice_adapter.py` | issue #6：隔離 `voice.tts` 的 None-on-failure 與兩種時間軸單位（**必讀 §3.1**） |
| `app/services/jobs/captions.py` | issue #7：由時間軸產 `subtitles/captions.srt` + `captions.json` |

測試基準（2026-08-28 實測）：**1564 passed / 11 skipped / 4172 subtests**，`ruff check` 全綠。


## 2. 驗證現況（接手第一件事）

```bash
cd ~/Documents/Claude\ Code\ Projects/MoneyPrinterTurbo
git fetch origin main
git log origin/main -1 --oneline
.venv/bin/python -m pytest test -q                    # 1564 passed / 11 skipped / 4172 subtests
.venv/bin/ruff check app cli.py main.py webui test    # All checks passed
```

**注意本地 checkout 可能落後或分歧。** 這在這台開發機上已經發生過兩次：本地 `main` 落後 origin 好幾個 commit，且帶著一個從未推上去的舊 handoff commit（`b5a411a`）。先 `git status` 與 `git log origin/main..HEAD` 確認，再決定 pull 或 reset。

CI 在 GitHub Actions（`ci.yml`）：Python 3.11 + 3.13（含 redis service，跑全套）+ Windows smoke。Windows smoke 是**逐檔列舉**的，PR #10 之後納入了 7 個 job pipeline 測試檔（812 tests，實測全綠 —— 所以 `store.py` 沒有 POSIX-only 假設）。**新增 job pipeline 測試檔時要記得加進那份清單**，否則它不會在 Windows 上跑。需要 ffmpeg 的測試不要加（Windows runner 不保證有）。CI 仍然**沒有型別檢查**。

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

## 3.1 同一課的第二次：`voice.tts` 也是回傳哨兵而不是拋例外

issue #6 開工前對 `app/services/voice.py` 做了同樣的稽核。它沒有 `format_response` 那種破壞資料的後處理，但**另外兩個坑一模一樣**，而且多了一個單位陷阱。以下全是 2026-08-28 實測，不是讀碼推論：

| 量測 | 結果 |
|---|---|
| `tts()` 缺憑證 / 錯憑證 / 斷網 | 三種都回 **`None`**，一次都沒拋例外 |
| `populate_legacy_submaker_with_full_text(SubMaker(), "...", 3.0)` | `offset == [(0, 15000000), (15000000, 30000000)]` |
| edge 路徑的 `cues[0].start` | `datetime.timedelta` |
| 斷網時 edge 路徑耗時 | 約 **90 秒**（3 次 × 30 秒 timeout）才回 None |

三件事會咬人：

1. **`None` 是它唯一的失敗訊號。** 把回傳當成「可選」的呼叫端會寫出 0 byte 音檔、記一筆 `bytes: 0` 的 AssetRecord、然後把 job 推進 `AWAITING_ASSETS`，彷彿成功了 —— 正是 SPEC-001 §7 第 6 條「確認素材不是空檔或不完整下載」禁止的事。
2. **`offset` 是 100 奈秒 tick，不是毫秒也不是秒**（1 秒 = 10,000,000）。除以 1000 會把 3 秒錄成 30000 毫秒，而且 issue #7 的「字幕不超出 voice 長度」會對著一個放大 10 倍的天花板通過。edge 路徑的 `cues` 又是 `timedelta` —— **同一個 SubMaker 物件上有兩套並存的單位**。
3. **每個 provider（Gemini 除外）內部自行重試 3 次。** 一次 `tts()` 呼叫等於授權最多 3 次真實 provider 請求，而它不回報打了幾次。預算天花板必須以「整次呼叫」為單位，不是「一次請求」。

另外兩個讀碼發現、未實測但程式碼明確的地雷：`azure_tts_v1` 的成功判定**只看字幕串流非空**，從不檢查有沒有收到音訊 chunk（`voice.py:802`）；`siliconflow` 在解不開下載內容時會**捏造一條 1 秒的時間軸**（`voice.py:935-947`）並回報成功。

### 現在的正確做法

`app/services/jobs/voice_adapter.py` 是隔離層，`master_voice.py` 只透過它碰 provider：

- `None` → `VoiceTransportError`，依訊息分 retryable。
- 兩種時間軸統一正規化成**整數毫秒**，下游不必知道 `voice.py` 有一半在講 tick。
- 交件前強制驗 `size > 0`、時間軸非空、`total_duration_ms > 0`；**而且在能解碼出時長時比對時間軸與實際音訊，差距超過 25% 就拒收** —— 那正是 siliconflow 捏造時間軸的特徵。
- **解不出時長要先問「這台機器有 decoder 嗎」再決定。** `_measure` 用的是 provider 內部失敗的同一個 decoder，所以「非空但解不開」的檔案也回 0.0 —— 早期版本把它當成「量不到」而採信 provider 的時間軸，等於讓捏造的時間軸原樣過關（審查實跑重現：4100 bytes 截斷 MP3 + `offset=[(0,10000000)]` → 得到 `duration_ms=1000, duration_source="timeline"`）。現在：有 decoder 卻讀不出來 → **拒收**；真的沒有 decoder → 記 `duration_source: "timeline"`，讓下游知道這份時長沒被證實。
- **segment 的邊界會被夾到最終時長，但一個都不會被刪。** 容許 25% 漂移後 `total_duration_ms` 取實測值，所以一個合法的 take 也可能有 segment 結束在總長之後；不夾的話 timeline 文件自己前後矛盾，而 #7 的字幕直接吃它。**「夾」不等於「丟」** —— 第一版用 `break` 把起點超過總長的 segment 整個刪掉，實測 3.0 秒音訊配 3.1 秒時間軸（漂移 3.2%，遠在容許範圍內）就會讓最後一個詞從文件裡無聲消失。被 provider 標在音訊結束之後的那個詞，寧可寫成零寬度區間（意思是「有這句、但沒聽到」），也不要刪到什麼痕跡都不剩。

**不要從 stage 直接呼叫 `voice.tts`。** 也不要照抄它的參數順序：`voice_file` 在 7 個 provider 裡有的是第 3 個位置參數、有的是第 4 個，位置呼叫會把輸出路徑跟語速對調。一律用關鍵字。

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
- **憑證只從 prompt 擋，不動 narration。** PRD-001 FR-004A 與 SPEC-001 §12 / §14 用同一句話寫死：`secret、credential 不得寫入 log、audit 摘要或 prompt`。Script 是模型從使用者主題產生的文字，夾帶憑證是真的會發生。做法分兩種，因為欄位性質不同：
  - `title` / `core_message` 是**整段引用**，所以直接 `budget.redact()`，精確。
  - narration 是**欄位的切片**，改成在切分前用 `redact` 去**測**整個來源欄位，把「可疑」旗標帶到每個切片上；可疑的切片，prompt **整句略去**而不是改寫。理由是實測：`api_key=sk-x,abcdefghijklmnopqrst` 用 ASCII 逗號切開後會變成 `<redacted>` 加上一個**沒有任何 pattern 抓得到的裸 token**。逐片過濾擋不住，測整段才擋得住。
  - **`Scene.narration` 永遠是原文。** `budget.redact` 是為 provider 摘要設計的貪婪過濾器，實測會把 `token economy 正在改變創作者的收入結構` 整句吃成 `<redacted> 正在改變...`、把 `先講 session 管理，再講快取` 吃成 `先講 <redacted>`。narration 是 voice 階段要念的字，過濾它等於毀掉合法腳本 —— 這跟當年把 idempotency key 過濾掉是同一個錯誤。有測試同時釘住「憑證不進 prompt」與「合法英文字不毀 narration」兩邊。
  - **識別碼不過濾**：`scene_id`、`import_dir` 是本地產生的。
  - 代價：腳本正常提到 `token`／`session`／`key` 等字時，那些場景的 prompt 會少掉旁白那一句（換成一句說明，請人確認是不是誤判）。這是**保守側的誤判**，不是漏擋。要降低誤判率就得寫一支專屬的 sanitizer，那超出 #5 範圍。
- **`plan_scenes` 不會把「已停在 `MANUAL_ACTION_REQUIRED` 的舊計畫」交回去。** `_FROZEN_PLAN_STATUSES` 只列規劃成功之後的階段，不含失敗／人工／預算／取消狀態；被 park 的 job 帶著舊 scenes 再呼叫會直接報錯，而不是若無其事重發一份 manifest 把該狀態蓋掉。
- **manifest 的 `accepted_mime_types` 每種副檔名只列一個 MIME。** SPEC §7 是「MIME sniffing 與副檔名雙重驗證」，所以不能一邊要求檔名 `scene-001.png`、一邊宣告接受 `image/jpeg` —— 那是叫操作者產出匯入階段會拒收的檔案。要放寬就得連檔名規則一起放寬。
- **改 `script.json` 再跑 `plan_scenes` 就是 replan**（前提是 job 還在 `SCENE_PLANNING`）：scene 與 manifest 一起換新。**但已經人工匯入的素材不會被動**，而 scene id 會被重用 —— 也就是說 `scenes/scene-003/images/` 裡的舊圖會留在原地，卻對應到新的 narration。改稿重跑之後要自己清或重生受影響的素材。issue #8 接匯入驗證時要考慮這件事。
- **`generation_manifest.json` 與 `scenes/<scene_id>/<kind>/` 刻意不在 `JobRecord` 裡**，所以 `replace()` 永遠刪不到它們（那些目錄可能已經有人工放進去的檔案）。走 `store.write_generation_manifest()` / `store.scene_media_dir()`，**不要**把它們塞進 `JobRecord`。
- 匯入目錄是 **SPEC-001 §3.2 的形狀**：`scenes/{scene_id}/images/` 與 `scenes/{scene_id}/videos/`，依 `visual_type` 決定。PLAN-001 Q9 明文指名這個路徑。注意 `store.py` 其餘的目錄配置（扁平的 `scene-NNN.json`、沒有 `audit/`）**本來就跟 §3.2 不一致**，那是 issue #1 的既有偏差，#5 沒有擴大也沒有修它。
- manifest 的 `import_dir` 是**相對於 job 目錄**的 POSIX 路徑（`scenes/scene-001/images`）。PLAN-001 Q9 原本寫「印出絕對路徑」，**已於 2026-08-28 改成相對路徑並在該檔記錄原因**：manifest 是會跟著 job 目錄搬的持久檔，絕對路徑一換機器就失效。要給人看的絕對路徑由印出的那一層自己接。
- `store.scene_media_relative_dir()` 什麼都不建立，但它產生的字串會進 manifest，所以它跟 `scene_media_dir()` **做同一套 scene_id 與 kind 驗證**。新增呼叫端時不要繞過它自己拼字串。
- **manifest 的 `created_at` 是「這份 manifest 何時被寫出」，不是決定性的。** 同一個 job 正常只會寫一次，但 manifest 被刪掉後重跑會補一份新的、帶新時間戳。scene 內容仍然完全決定性；不要拿整份 manifest 做 byte 比對當回歸測試，要比就比 `entries`。
- `generated_video` 名額 = `min(job.max_generated_video_scenes, 3, body scene 數 // 3)`，取 narration 最長的 body scene，同長度時取 index 小的。**上限是天花板不是配額**，所以典型 V0 job 只會拿到 1~2 個 AI 影片。

Master Voice（issue #6）：

```python
start_voice_generating(job, store)   # SCENE_PLANNING -> VOICE_GENERATING
generate_master_voice(job, store)    # 一次 TTS -> audio/ + 時間軸 + AssetRecord -> AWAITING_ASSETS
```

- **一個 job 只有一個 Master Voice**（SPEC §6.3、PRD FR-004A）。這由 stage 自己的短路強制，**不是** store：`assets.jsonl` 是 append-only、完全不去重，`append_event` 只是加一行。
- **短路條件是「完整」不是「存在」**：AssetRecord、音檔 bytes、時間軸文件三者齊備才算做完；缺任何一個都會報錯而**不是**再合成一次。（這是 #5 教訓 5 的同型防護。）
- **`audio/` 與時間軸刻意不在 `JobRecord` 裡**，所以 `replace()` 刪不到它們 —— 那些 bytes 是花 provider 呼叫換來的。走 `store.master_voice_path()` / `write_master_voice_timestamps()`。
- **`audio/master-voice-timestamps.json` 的 schema 是這裡發明的**，規格沒有定義：`{content_job_id, master_voice_asset_id, total_duration_ms, duration_source, segments:[{index,text,start_ms,end_ms}]}`，全部整數毫秒。#7 直接吃這份；要改形狀請連 #7 一起改。
- **`duration_source`** 是 `"measured"`（解碼實際音訊得出）或 `"timeline"`（host 沒有 decoder，只能採信 provider 自報）。#9 的 QA 若要斷言時長，只有 `"measured"` 才算證實過。
- **免費 provider 記真實 0.0，付費 provider 記天花板 + `"unknown"`。** edge-tts 與 no-voice 不計費，給它們記 0.05 會讓每個 V0 job 平白吃掉 1.7% 預算，而且讓 `actual_cost_usd` 往另一個方向錯 —— §10 禁止把「未知」寫成 0，但把「已知的 0」寫成一個發明的非零數同樣不誠實。
- **語音選擇看 `job.language`**（`zh-TW` 會拿到台灣配音，不是大陸配音），`config.app["voice_name"]` 覆寫它。SPEC §3.1 的請求契約沒有語音欄位、`ContentJob` 又是 `extra="forbid"`，所以 job 目前帶不了自己的語音設定 —— 見 §7。
- **合成語音的 consent 欄位**：`consent_status="not_applicable"`、`manual_review_status="not_required"`、`license_or_consent="synthetic_tts_no_creator_reference"`。SPEC §6.3 的同意規範管的是「引用真人聲音」，這條路徑不 clone 也不模仿任何人；FR-005 的 preflight 同樣明文只針對真人 voice/avatar。**#8 要把這個範圍寫死**，否則合成語音會卡在一個為真人設計的人工審核閘門上。

字幕（issue #7）：

```python
generate_captions(job, store)   # 在 AWAITING_ASSETS 內執行，不改狀態
```

- **PLAN row 7 括號裡的「沿用 subtitle.py」是錯的機制，不要照做。** 實測三件事：`subtitle.create()` 是 faster-whisper **ASR**，缺套件回 `""`、**成功與失敗都回 `None`**、空轉錄時寫一個 1 byte 的 `"\n"` 檔還記 log 說建立成功；`subtitle.correct()` 餵空 SRT 會**捏造**一行 `00:00:00,000 --> 00:00:00,000` 給每個腳本句，然後通過舊 pipeline 唯一的檢查；`voice.create_subtitle()` 需要 `voice_adapter` 已經丟掉的 `SubMaker`，而且講 100 奈秒 tick。row 7 自己的 scope 條文「由 master voice timestamps 產」才是對的路徑。
- **`utils.text_to_srt` / `time_convert_seconds_to_hmsm` 也不能用。** 實測：吃 float 秒、**會截斷**（8123ms → `00:00:08,122`）、每個 block 尾端多 8 個空格、負值沒防呆（-3 秒 → `-1:59:57,000`）。`captions.srt_timestamp()` 用整數 divmod 自己算，四行。
- **一個 scene 一條字幕**，文字是 `scene.narration` 逐字。**不要一個 timeline segment 一條** —— edge 路徑的 segment 是**詞邊界**，那會產出一個字一條的中文字幕，而且 `caption_ref` 無法跟 scene 保持 1:1（兩份凍結 render manifest 都釘死 `caption-001..00N` 對 scene）。
- **必須夾上限，這不是防禦性寫法。** `voice_adapter.synthesize` 容許 25% 漂移後把 `total_duration_ms` 設成**實測值**，所以 `segments[-1].end_ms` 合法地可能超過它。不夾的話，「字幕不超出 voice 長度」會在一個完全正常的 take 上失敗。
- scene 邊界怎麼推：timeline **沒有 scene_id**（`narration_text` 是無分隔字串拼接），所以先按旁白**字元比例**算邊界，再**在半個相鄰間距以內**吸附到真實 segment 起點。**那個距離上限是重點** —— Edge 路徑的 segment 是詞邊界、最近的一個只差幾毫秒，吸附確實改善；但其他所有 provider 的 segment 是**子句級**，最近的子句起點可能在幾秒外，硬吸過去等於把這個 scene 的字元佔比換成那個子句的佔比，沒有增加任何資訊只是把邊界搬走。無上限版本用真實鏈路 fuzz 3000 份腳本，最壞把邊界搬了 10.25 秒、讓一個 26 字的 scene 只在畫面上停 36 毫秒。
- **cue 文字會做行正規化。** 空行在 SubRip 裡是 cue 分隔符，所以旁白裡夾一個空行會讓那一條字幕**提早截斷** —— 讀取端只拿到前半，而同一個 stage 寫的 `captions.json` 仍然完整，兩份檔案自相矛盾，AssetRecord 的 sha256 還會把壞掉的位元組認證成好的。`Scene.narration` 是 LLM 來的未驗證字串，`scene_planner._sentences` 只 strip 外圍空白，所以「兩段式開場白」會原樣抵達這裡。`_cue_body()` 逐行 strip 並丟掉空行（`splitlines` 一次涵蓋 `\n` / `\r` / `\r\n` 與 vertical-tab 家族）。
- **SRT 結尾一定有一個空行。** 好幾種 SubRip 讀取器（含 moviepy 的 `file_to_subtitles`，也就是 #9 會餵這個檔的那個）只在遇到空行時才把手上那條 cue 收下，少了它就**丟掉最後一條**。
- **`voice_duration_source` 原樣傳進 `captions.json`**：`"timeline"` 代表上限只是 provider 自報、沒被解碼證實。#7 **不硬停**（跟 #6 一致：記錄而非拒絕），但 #9 的 ffprobe QA 要據此區分「已證實」與「僅宣稱」。
- **#7 不改狀態、成功路徑不寫 decision、不寫 ProviderEvent／ledger、不過預算閘門。** `AWAITING_ASSETS → READY_TO_RENDER` 是 #8 的邊。只有非可重試失敗才 park 進 `MANUAL_ACTION_REQUIRED`。
- **`asset_type` 是 `"subtitle"` 不是 `"audio"`。** #6 用 `asset_type == "audio"` 當唯一性鍵，寫錯會讓之後每次 `generate_master_voice` 都報「job carries 2 voice assets」。

凍結 fixture：`test/fixtures/jobs/three-scene-demo/`、`ten-scene-demo/`。後續 slice 一律對這兩組驗收，**不要自建第二套測試資料**。但注意：**這兩組 fixture 沒有任何媒體 bytes**，`sha256` 是 `0001`~`0010` 的流水佔位值，issue #8 / #9 的驗收需要真實素材時要另外處理。另外，**這兩組 fixture 的 `scenes/` 是手寫的，不是 planner 產出的** —— 拿它們的 script 跑 `plan_scenes` 都會得到 8 個 scene，而 `ten-scene-demo` 的目錄裡是 10 個。不要把兩者當成同一回事。

## 5. §5.2 補邊：停駐的 job 現在回得來

**這一節先前記的缺口已經補上。** §5.2 從 65 條邊補到 **76 條**，補的全是「停下來的 job 怎麼回到流程」，沒有新的生成路徑：

| 類別 | 條數 | 內容 |
|---|---|---|
| 1 | 6 | `RETRYABLE_FAILED` → 6 個可恢復階段（可重試階段扣掉 `POSTIZ_DRAFTING` 與 `SCENE_PLANNING`） |
| 2 | 1 | `RETRYABLE_FAILED` → `FAILED`。`FAILED` 先前入邊為 0，job 層級重試耗盡才走這條 |
| 3 | 4 | `MANUAL_ACTION_REQUIRED` → `SCRIPTING` / `VOICE_GENERATING` / `AWAITING_ASSETS` / `READY_TO_RENDER` |
| 4 | 0 | `BUDGET_EXCEEDED` **刻意沒有返回邊** |

`BUDGET_EXCEEDED` 的恢復是既有的兩跳：先 `→ MANUAL_ACTION_REQUIRED`，再由那裡返回。這是產品決定（用狀態機把人工檢查點做成結構性的），**不是**從 FR-007「不得自動繼續生成」推導出來的 —— 該條禁的是自動續跑，不是返回邊本身。返回之後閘門仍會重跑：真正擋住支出的是閘門，不是這張表。閘門目前**實際只有兩處**（`pipeline` 從 `SCRIPTING`、`master_voice` 從 `VOICE_GENERATING`），`READY_TO_RENDER` 那處是 §5.2 已寫入、尚未實作的第三處；三者都在返回集合內，所以兩跳對每個閘門位置都收斂。

### 返回目標由 `decisions.jsonl` 推導，不由呼叫端挑

`state_machine.resume_target(status, decisions)` 回答「回哪裡」，**不回答「可不可以回」** —— 後者是呼叫端的事：`RETRYABLE_FAILED` 可自動 resume（受 §5.3 重試上限與 §10 閘門節制），`MANUAL_ACTION_REQUIRED` 必須人工觸發，那正是該狀態名稱的意思。對這兩條返回列，§5.2 的表是**必要條件而非充分條件**：呼叫端必須先推導，不得自己挑一個合法目標。

規則：由後往前走 `decisions.jsonl`，**取檔案順序**。每筆記錄若 `to` 等於目前狀態就取它的 `from`，否則取它的 `to`；`from == to` 的是拒絕留痕、不是移動，略過；候選落在三個停駐狀態是連鎖停駐，繼續往前走。

兩條看起來可以簡化、實際不行的規則：

- **不能改成用時間欄位 `at` 排序。** `at` 是 `job.updated_at`，而 `transition(now=)` 讓呼叫端能自己蓋時間戳，所以它不保證單調。（先前這裡的理由寫「兩份 fixture 有三行 `at` 相同，排序會打亂歷史」，**那是錯的** —— Python 的排序穩定，實測兩份 fixture 依 `at` 排序都等於檔案順序。理由換掉了，規則沒變，並補了一條會抓到排序寫法的測試。）
- **候選要取停駐那一行的 `from`，不是前一行的 `to`。** 每個寫入端都是先 `store.save(job)` 再 `store.append_decision(...)`，崩在兩者之間會留下「`job.json` 已前進、對應的決策行沒寫」。讀前一行的 `to` 會安靜地回答一個較早的階段 —— 合法邊，錯答案。

拒絕而不猜（每一條都有測試）：輸入不是停駐狀態；輸入是 `BUDGET_EXCEEDED`（錯誤訊息指向兩跳路徑）；決策紀錄為空（`DRAFT → MANUAL_ACTION_REQUIRED` 是合法邊，而只有 `start_scripting` 會寫第一行，所以真的到得了）；記錄缺 `from`／`to` 或帶未知狀態（**拒絕，不是略過**）；走完整份紀錄仍找不到非停駐階段；推導出的階段不在該狀態的**返回集合**內。

最後一項刻意比對返回集合而不是 `TRANSITIONS[status]`：後者還含 `CANCELLED`（`RETRYABLE_FAILED` 還含 `FAILED`），是合法邊但不是「可以回去的階段」。回傳它等於讓「要求重啟」變成「安靜地取消」—— 這個破口在審查時實測到過，已修。

### 兩個刻意的排除，以及各自的解除條件

- **`POSTIZ_DRAFTING` 不是返回目標。** §5.3 要求 resume 走 idempotency key，而 `postiz.py` 從頭到尾沒讀過 idempotency key（也不走 `record_usage`，見 §7），重新進入會產生第二份草稿。**解除條件**：發布路徑先讀取並比對 idempotency key。
- **`SCENE_PLANNING` 兩個返回集合都不在。** `plan_scenes` 在既有 scene 與重新推導的結果不一致時整批 `store.replace`，而 `_build_scenes` 把 `reference_assets` 硬填 `[]`，人工補入的素材參照會被抹掉 —— 那正是這兩條返回列要保住的東西。**危險在 planner，不在哪一條邊通向它**，所以兩邊一起排除；初版只擋了人工那條、留著 `RETRYABLE_FAILED → SCENE_PLANNING`，審查時被指出同一機制在那條上照樣成立，因此收掉（77 → 76）。**解除條件**：planner 改成合併而非替換（scene id 是決定性的 `scene-NNN`，可依 id 帶過舊的 `reference_assets`），屆時兩邊一起加回。

### 還沒關的（動重試 runner 之前先看這裡）

- **`SCENE_PLANNING` 也會停到 `MANUAL_ACTION_REQUIRED`**（`scene_planner._persist_unplannable`，腳本太短規劃不出 8 段時），但它不是返回目標，所以那類 job 目前**只能取消**。操作員唯一像樣的替代路線是走新的 `MANUAL_ACTION_REQUIRED → SCRIPTING` 重寫腳本，而那條路仍然會重跑 `plan_scenes`、仍然會抹掉 `reference_assets`。planner 改成合併同時關掉這兩條。
- **resume 不收斂。** 推導是全函式但不終止：打到重試上限的 job 每次 resume 都拿到同一個階段、立刻再停駐，每輪往 `decisions.jsonl` 追加兩行。實測 7 輪零進展、零額外支出、每輪 +302 bytes。「log 有沒有長大」**不是**停止條件（每輪都會長）；runner 要停在「上一次 resume 只追加了 `park → stage` 與緊接著的 `stage → park`」。
- **停駐來源的覆蓋率只有 4/19。** 推導答得出 `SCRIPTING`／`VOICE_GENERATING`／`AWAITING_ASSETS`／`READY_TO_RENDER`；`RENDERING`、`TECHNICAL_QA`、`CONTENT_QA`、`READY_FOR_REVIEW`、`POSTIZ_DRAFTING`、`POSTIZ_DRAFTED` 一律拒絕。所以「草稿建好了、改個文案再繼續」這種流程目前沒有自動路徑，接發布流程的那張 issue 要正面處理。
- **§15 新增的未決項**：人工核可的 resume 是否重置該階段的重試計數、若重置由什麼 audit trail 記錄。現在的計數是從持久化的 idempotency key 重建的，等於 job 終身累計；若不重置，class 1 的返回列一走就立刻再次停駐，等於沒有作用。

補邊同步改了規格（§5.2 表 + 類別列舉 + §12 + §15）、狀態機與那組窮舉測試，並修正了三個宣稱「`RETRYABLE_FAILED` 沒有出路」的過期註解（`pipeline` / `master_voice` / `captions` 的 park 行為**沒有動** —— 換成 park-and-resume 是另一張 slice）。**窮舉測試是手抄規格表的**，刻意不從被測模組 import 返回集合 —— 那才抓得到實作寫錯。測試基準線：**1564 passed / 11 skipped / 4172 subtests**（補邊前 1521）。

## 6. 剩下的 issue

| # | 標題 | 大小 | 卡在哪 |
|---|---|---|---|
| 8 | Asset Import + Creator Profile preflight | L | **無阻塞**（#5 已完成；`AWAITING_ASSETS` 的人工停駐現在有返回邊，見 §5） |
| 9 | Render Manifest + Renderer + ffprobe QA | L | 依賴 #6 #7 #8 |
| 11 | `run --job` 端到端 + golden fixtures | M | 依賴全部；§5.2 的返回邊已補，剩 §5「還沒關的」那幾條 |
| — | Phase 3 POC 操作 runbook（PLAN-001 Q6 提到的 S 號 docs issue） | S | 無阻塞，尚未建立 |

**階段之間的斷點目前都接上了**：`start_scripting()`（#4）→ `start_scene_planning()`（#5）→ `start_voice_generating()`（#6）。三個形狀一致：重讀 store、檢查前置文件存在、`transition` + `save` + `append_decision`。**`generate_master_voice()` 走到底會把 job 推進 `AWAITING_ASSETS`**，所以 #7 / #8 的入口狀態是那裡，不需要再補一條 stage 起手邊 —— `AWAITING_ASSETS → READY_TO_RENDER` 由 #8 擁有。

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
- **`create_job` 把 `creator_profile_id` 硬填空字串**，而 SPEC §3.1 的輸入契約根本沒有這個欄位。每個 V0 job 都無法連回 consent 記錄。**這條先前寫「擋到 #6」，實測不成立** —— #6 合成的是 TTS，不引用任何真人聲音，所以不需要 creator profile（見 §4 的 consent 欄位）。它仍然擋 #8 / #9 的**真人素材**路徑。
- **job 帶不了自己的語音設定。** SPEC §3.1 的請求契約沒有語音欄位，`ContentJob` 是 `extra="forbid"`，而 repo 裡唯一的 `voice_name` 設定住在 `[ui]`（WebUI 自己的偏好儲存）。#6 因此讀 `config.app["voice_name"]`、否則依 `job.language` 選一個。**同一台機器上跑兩個不同語言的 job 會共用同一個設定值**，這在接 CLI/API 層時要解決。
- **音檔容器是寫死的 `.mp3` / `audio/mpeg`。** 預設 edge-tts 產出就是 MP3、逐位元組原樣寫入，所以目前正確；但 MiniMax 可設成回傳 WAV，`mimo` 又是唯一會照副檔名走的 provider。要正名就得嗅探容器，那屬於 #8（Asset Import）的驗證範圍。
- **`Scene.caption` 沒有任何人讀。** `scene_planner` 為每個 scene 產了一個 ≤20 字的 `caption`（例如旁白「九成企業導入 AI 的第一步就走錯了。」對應 caption「九成企業第一步就走錯」），但 #7 的 SRT 寫的是 **`narration` 逐字** —— 字幕軌要跟語音說的一致，說一套寫一套是錯的。`caption` 看起來是為「燒進畫面的短標」設計的，那是另一個產物。**沒有規格條文決定這件事**（PRD FR-004 只說「旁白句子或單字時間軸」皆可），要改是產品決定，而且會改動 #7 的驗收測試。
- **字幕可讀性規則完全未定義。** PRD/SPEC/PLAN 都沒有每行字數、每條行數、最短/最長秒數。目前一個 scene 一條，在 three-scene-demo 是 17–26 個中文字撐 6–22 秒 —— 對直式短影音的燒錄字幕偏長。要切短就得決定切分規則，而且**切開後每段是否各自擁有 `caption_ref` 會打破兩份凍結 render manifest 釘死的 1:1 對映**。
- **fixture 的 voice asset 路徑與 #6 產出不一致。** 兩組凍結 fixture 記的是 `assets/asset-voice-001.wav`，#6 依 SPEC §3.2 與 PLAN row 6 的驗收欄寫 `audio/master-voice.mp3`。目前沒有任何測試斷言 `storage_key`，所以兩者並存不會紅；但 **#9 讀的是 AssetRecord、不是 fixture**，要不要把 fixture 重新凍結成新路徑是個未決的小決定。
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
- **改稿改到「規劃不出來」時，舊的 `generation_manifest.json` 會留著。** job 被 park 進 `MANUAL_ACTION_REQUIRED`，但 manifest 檔案還在，而且它描述的是**磁碟上那組舊 scene**（因為新計畫根本沒寫成功），所以 manifest 與 scene 仍然互相一致，不是壞資料。權威訊號是 `job.json` 的 status。刻意不刪：人可能正拿著那份 manifest 在產素材，刪掉比留著糟。
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
