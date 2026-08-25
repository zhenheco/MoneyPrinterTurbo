# Real-Person Local Media Demo — 2026-08-20

## Result

The supplied `IMG_1966.MOV` was processed locally into a sanitized reference pack and
successfully rendered through MoneyPrinterTurbo. No personal media was uploaded to an
external provider, committed to Git, or sent through TTS.

## Input assessment

- Source: user-provided iPhone MOV, 65.636667 seconds.
- Source video: HEVC Main 10, 3840×2160, 30 fps, with portrait display rotation.
- Source audio: AAC, 48 kHz, stereo, 65.635 seconds.
- The source contained location, device, and creation-time metadata; the derived files
  remove it.
- Sampled frames showed a clear, front-facing speaker with visible mouth movement. Glasses
  and bright overhead lighting remain part of the reference and may affect provider quality.

## Derived local artifacts

- `storage/local_creator_media/demo-20260820/real-person-demo-10s.mp4` — 10-second,
  1080×1920 H.264/AAC demo clip with metadata removed.
- `storage/local_creator_media/demo-20260820/real-person-demo-10s.m4a` — audio extracted
  from the same demo clip for native speech preservation.
- `storage/local_creator_media/demo-20260820/avatar-reference.jpg` — 720×1280 face
  reference frame extracted from the demo clip.
- `storage/local_creator_media/demo-20260820/voice-reference-full-normalized.m4a` —
  65.7-second normalized voice reference, kept separate from the native-sync audio.

## MoneyPrinterTurbo proof

- Task: `46033309-50df-4c14-819f-4065465d184f`
- Mode: `native_speech_avatar`
- Audio input: the M4A extracted from the same local demo clip; no TTS was generated.
- Final: `storage/tasks/46033309-50df-4c14-819f-4065465d184f/final-1.mp4`
- Final media: H.264/AAC, 1080×1920, 10.000 seconds; full FFmpeg decode exit 0.
- Final container metadata contains only generic encoder/container fields; no source GPS
  or iPhone location metadata was carried into the final.

## Boundary

This proves local sanitization, face/audio extraction, same-source audio import, and local
composition. It does not prove voice cloning, avatar cloning, provider upload, or new
generated scenes using the user's likeness. Those require a separately named provider,
explicit usage scope, and a new approval step.
