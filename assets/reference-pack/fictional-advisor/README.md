# Fictional Advisor Reference Pack v1

This pack is a Codex-generated visual reference set for the short-video POC.
It depicts a fictional character and is not a likeness of the user.

## Assets

- `character-reference-v1.png` — identity anchor: face, hair, clothing, and overall appearance.
- `scene-laptop-v1.png` — scene reference: the advisor working on a laptop in a Taiwanese office.
- `scene-presenter-v1.png` — scene reference: the advisor speaking to camera in a local shop.

## Video handoff

1. Treat `character-reference-v1.png` as the identity anchor for any downstream provider.
2. Use the two scene images as composition and lighting references; they are not final video frames.
3. Generate short 4–6 second MP4 scenes with the selected video/avatar provider.
4. Import the returned MP4 through the local material-upload path before MoneyPrinterTurbo composition.
5. For still-image scenes, use one approved Master Voice asset. For a provider talking-head
   scene with native speech, keep that provider MP4's original audio and mark the scene
   `native_speech_avatar`; do not replace it with another TTS voice.

## Data boundary

This synthetic pack contains no user biometric data. Personal voice, photos, and talking-head
video must remain local-only unless the user explicitly approves a named provider and usage scope.
