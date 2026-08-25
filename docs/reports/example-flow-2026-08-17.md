# Example Flow Verification — 2026-08-17

## Result

The first local final was rejected for the requested lip-sync use case: it replaced a
provider-generated silent male visual with the female Edge voice
`zh-TW-HsiaoChenNeural`. The corrected sample uses Google Flow's native male voice and
keeps that same provider audio through the local export path.

No personal voice, photo, avatar, or biometric material was used; the character and
voice are fictional/sample-only.

## Verified path

1. Codex generated a fictional 9:16 character reference image.
2. Google Flow accepted the image as an ingredient and generated one 4-second vertical
   video with Omni Flash, the complete Mandarin dialogue, and `Charon` (`Male,
   informative, lower pitch`).
3. The downloaded provider MP4 was validated as the source of both the visible speech
   motion and the native audio.
4. The native AAC track was extracted as M4A and supplied to MoneyPrinterTurbo through
   `--audio-mode native_speech_avatar --custom-audio-file`; the CLI and service guard
   reject this mode when the native audio asset is missing, so no TTS voice was generated
   for the corrected task.
5. MoneyPrinterTurbo rendered the provider visual with that same native audio into a
   1080×1920 final MP4. Subtitles were intentionally disabled in this sync verification
   so no independently timed caption layer could be mistaken for speech-sync proof.

## Evidence

- Google Flow provider asset: `storage/provider-samples/google-flow/icAn_202608171336.mp4`
- Provider asset: H.264/AAC, 720×1280, 4.010000 seconds, 1,047,679 bytes
- Provider asset post-download `ffmpeg -v error -i ... -f null -`: exit 0
- Task: `b44d2ba7-6336-473f-80ea-abe4fa561c6d`
- The first task/final above is retained only as a negative regression example; it is not
  accepted as provider lip-sync evidence.
- Corrected Google Flow provider asset:
  `storage/provider-samples/google-flow/charon-male-lipsync-202608171347.mp4`
- Corrected provider asset: H.264/AAC, 720×1280, 4.010000 seconds, 1,103,812 bytes;
  full decode exit 0
- Corrected native audio: `storage/provider-samples/google-flow/charon-male-lipsync-202608171347.m4a`,
  AAC, 48 kHz stereo, 4.010000 seconds
- Corrected task before the guard: `d8bb0e81-4fa4-4d7c-844c-44c9cd8b3d1d`
- Prior guarded corrected task: `e6a12f6d-7be0-43f8-9e7c-e9317f92c0af`
- Earlier guarded corrected final:
  `storage/tasks/e6a12f6d-7be0-43f8-9e7c-e9317f92c0af/final-1.mp4`
- Guarded rerun after Creator Profile preflight code changes:
  `64da79a1-6f73-4c21-941b-1d987605b4e2`
- Guarded rerun final:
  `storage/tasks/64da79a1-6f73-4c21-941b-1d987605b4e2/final-1.mp4`
- Guarded rerun final: H.264/AAC, 1080×1920, 4.010000 seconds, 1,116,635 bytes;
  full decode exit 0
- Earlier guarded corrected final: H.264/AAC, 1080×1920, 4.010000 seconds; full decode exit 0
- Creator Profile example preflight: exit 2 with
  `voice.consent_status must be explicit_granted`; no task was created
- Valid synthetic Creator Profile preflight: task
  `6f27ea88-13bb-4b90-86bd-b00e786f3b62`, exit 0 at script stage
- Latest full render with the valid synthetic Creator Profile:
  `ece2037a-9293-4e3c-bfb3-65f6dcae2da4`
- Latest full-render final:
  `storage/tasks/ece2037a-9293-4e3c-bfb3-65f6dcae2da4/final-1.mp4`
- Latest full-render final: H.264/AAC, 1080×1920, 4.010000 seconds, 1,116,635 bytes;
  full decode exit 0
- Visual spot check at four one-second samples: mouth state changes from closed to open
  while the character also changes expression/gesture; this supports same-source native
  speech motion but is not a phoneme-level automated sync score.

## Boundary and follow-up

This validates the browser-based Google Flow provider path, native male speech selection,
same-source audio preservation, local asset import, timeline, and final export. It does
not yet validate a provider API adapter, automated Flow session reuse, personal
likeness, personal voice, or a production-safe consent/retention policy for real-person
media. The previous FFmpeg-only sample task remains a local fallback baseline, not the
provider-generation proof for this run.
