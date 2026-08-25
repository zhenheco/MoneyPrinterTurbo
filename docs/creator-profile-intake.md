# Creator Media Intake

這份流程是給真人 voice／avatar 素材進入本機 POC 前使用。Creator Profile 只保存
asset reference 與授權 metadata，不保存照片、聲音原檔、base64、credential 或 token。

## 要提供的素材

1. Voice：一個乾淨的 WAV、MP3 或 M4A 錄音；建議單人、低背景噪音、不要包含其他人的聲音。
2. Avatar：一張正面 PNG/JPG，或已由核准 provider 產生的 talking-head MP4。
3. `creator-profile.json`：從
   `assets/reference-pack/fictional-advisor/creator-profile.example.json` 複製後填入，
   只放 `asset_ref`、consent、usage scope、source、expiry／revocation 與 manual review。
   本機 synthetic smoke 可直接參考已核准的
   `assets/creator-profiles/fictional-advisor.sample.json`；它不代表真人授權。

真人檔案先放在本機受控目錄，不要放進 Git；profile 的 `asset_ref` 不是檔案路徑，不能使用
`../../`、`storage_path`、base64 或 provider credential。

## 兩種影音模式

- 只有照片＋真人錄音：使用 `master_voice`。這是旁白後製，照片不會自動產生逐字嘴型。
- provider 已輸出 talking-head MP4＋其原生對白：使用 `native_speech_avatar`，並把同一支
  provider MP4 的音訊抽出後以 `--custom-audio-file` 傳入；不能再接另一個 TTS。

## 先做 metadata preflight

~~~bash
uv run python cli.py \
  --video-script "測試授權 metadata，不生成影片" \
  --creator-profile "assets/creator-profiles/<profile-id>.json" \
  --stop-at script
~~~

只有 `consent_status=explicit_granted`、未過期、未撤回且
`manual_review_status=approved` 才能進入 render；缺少任何一項會在 LLM／TTS／素材處理前失敗。

## Native speech avatar render

~~~bash
uv run python cli.py \
  --video-script "完整且已核准的旁白文字" \
  --video-source local \
  --video-materials "path/to/provider-talking-head.mp4" \
  --audio-mode native_speech_avatar \
  --custom-audio-file "path/to/the-same-provider-audio.m4a" \
  --creator-profile "assets/creator-profiles/<profile-id>.json" \
  --no-subtitle-enabled \
  --bgm-type none \
  --stop-at video
~~~

輸出前仍需人工播放檢查 voice／角色、嘴型、字幕與畫面；`ffprobe`／完整解碼通過不等於
逐音素對嘴 QA 通過。若只有 still image 與錄音，請改用 `master_voice`，不要把它宣稱為
lip-sync 影片。

## Revocation

一旦 consent 過期或撤回，將 profile 的 `revoked_at` 填入時間並停止 render；不要刪掉 audit
metadata。原始真人檔案的 retention／deletion 由人工決定，本機 POC 不會自動送到 provider。
