"""Build and validate the SPEC-001 §8 Render Manifest.

PLAN-001 issue #9, first half. One entry point::

    manifest = build_render_manifest(job, store)   # runs at READY_TO_RENDER

§8 is one JSON example and two sentences (``SPEC-001:510-552``). The load-bearing
one is ``:512`` 「Render Manifest 必須是可重現資料，不依賴目前 WebUI 的暫存狀態」,
so every field below is derived from documents already on disk — the job, its
scenes, its asset records, and the ``subtitles/captions.json`` timeline issue #7
wrote. Nothing is read from the WebUI, from ``config.toml`` or from a task id.

**The pydantic model validates nothing but types.** Measured 2026-08-29 against
:class:`~app.models.content_job.RenderManifest`: ``end_ms`` 1000 before
``start_ms`` 9000, negative milliseconds, ``motion.type`` ``"totally_made_up"``,
a 7x3 canvas at 0 fps in pixel format ``"nonsense"``, ``audio.mode``
``"not_a_mode"`` at ``sample_rate`` -1, and ``output.video_codec`` ``"zzz"``
were **all accepted**. ``extra="forbid"`` is the only real guard the model has.
So SPEC-001:625 「Render Manifest schema 有通過與拒絕案例」 is satisfied by
:func:`validate_render_manifest`, not by the model, and that is where the
rejection cases live.

``native_speech_avatar`` is refused in V0, and the refusal is the point
--------------------------------------------------------------------

SPEC-001:405 requires that when a talking-head/avatar scene's material carries
the provider's own lip-synced dialogue, ``audio.mode`` becomes
``native_speech_avatar`` and 「Renderer 不得用另一條 TTS／錄音覆蓋它」.

That rule cannot be expressed in the current data contract, measured:

* the decision is **per Scene** (SPEC-001:405, PRD-001:142) but §8 puts ``mode``
  on the **manifest-level** ``audio`` object, and
  :class:`~app.models.content_job.RenderSceneEntry` has no audio field at all;
* :class:`~app.models.content_job.RenderAudio.master_voice_asset_id` is
  **required and not Optional**, so ``mode="native_speech_avatar"`` still forces
  naming the very track that must not be used;
* nothing links the audio to the same provider asset, so SPEC-001:624's
  「video／audio 來自同一 provider asset」 has nothing to assert against.

Adding a per-scene audio field would change ``RenderManifest`` and break the
byte-identical round-trip of both frozen fixtures, and SPEC-001:405 additionally
makes 影音 QA 通過 a precondition that V0 has no human A/V QA step to satisfy.

So this module implements the half of the rule that *is* expressible today: when
an avatar scene's imported asset carries an audio stream, building the manifest
is **refused**, naming the scene. Silently rendering it in ``master_voice`` mode
would overwrite that dialogue, which is exactly what :405 forbids; refusing
honours the rule, and SPEC-001 §15 now carries the open question of how to
express it properly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.models.content_job import (
    AssetRecord,
    ContentJob,
    RenderAudio,
    RenderCanvas,
    RenderManifest,
    RenderMotion,
    RenderOutput,
    RenderSceneEntry,
    Scene,
)
from app.services.jobs import render_adapter
from app.services.jobs.store import JobStore

#: PLAN-001 row 9 and the §8 example: the one render target V0 has.
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
CANVAS_FPS = 30
PIXEL_FORMAT = "yuv420p"
CONTAINER = "mp4"
VIDEO_CODEC = "h264"
AUDIO_CODEC = "aac"

#: ``app/models/schema.py:34`` already has an (unused) ``VideoAudioMode`` enum
#: with these two members; the string values are repeated rather than imported
#: because importing ``schema`` pulls in ``config`` and writes ``config.toml``.
AUDIO_MODE_MASTER_VOICE = "master_voice"
AUDIO_MODE_NATIVE_SPEECH_AVATAR = "native_speech_avatar"
#: ``master_voice`` is the only mode V0 builds; see the module docstring.
SUPPORTED_AUDIO_MODES = frozenset({AUDIO_MODE_MASTER_VOICE})

MOTION_NONE = "none"
MOTION_KEN_BURNS = "ken_burns"
KNOWN_MOTION_TYPES = frozenset({MOTION_NONE, MOTION_KEN_BURNS})
#: The §8 example's own Ken Burns numbers, and both frozen fixtures'.
KEN_BURNS_SCALE_START = 1.0
KEN_BURNS_SCALE_END = 1.08

#: ``AssetRecord.asset_type`` values this module resolves against, all written
#: by earlier stages: ``asset_import`` (image/video), ``master_voice`` (audio),
#: ``captions`` (subtitle).
IMAGE_ASSET_TYPE = "image"
VIDEO_ASSET_TYPE = "video"
AUDIO_ASSET_TYPE = "audio"
SUBTITLE_ASSET_TYPE = "subtitle"
SCENE_ASSET_TYPES = frozenset({IMAGE_ASSET_TYPE, VIDEO_ASSET_TYPE})

#: SPEC-001:405's 「talking-head／avatar」 — the ``Scene.visual_type`` values the
#: native-speech rule is scoped to. ``VisualType`` has exactly one of them.
AVATAR_VISUAL_TYPES = frozenset({"avatar"})

#: The rate the renderer actually produces, and therefore the only rate the
#: manifest may declare. ``video.generate_video`` passes the source clip's own
#: ``fps`` to the muxer — but measured 2026-08-30, moviepy's ``AudioFileClip``
#: reports ``fps == 44100`` for a 48 kHz WAV, because its reader resamples to
#: its own 44100 default. So every V0 render comes out at 44100 whatever the
#: Master Voice was recorded at, and a manifest that declared the source rate
#: (48000, as SPEC-001 §8's hand-written example and both frozen fixtures do)
#: would be a number technical QA could only ever fail against.
AUDIO_SAMPLE_RATE = 44100


class RenderManifestError(ValueError):
    """The §8 manifest cannot be built, or does not describe a renderable job.

    A ``ValueError``, so ``state_machine.classify_error`` calls it
    non-retryable: a manifest that does not describe the job on disk does not
    start describing it on a second attempt.
    """


def _one_asset(assets: Sequence[AssetRecord], asset_type: str, label: str) -> AssetRecord:
    matches = [asset for asset in assets if asset.asset_type == asset_type]
    if not matches:
        raise RenderManifestError(f"the job has no {label} asset record")
    if len(matches) > 1:
        raise RenderManifestError(
            f"the job carries {len(matches)} {label} asset records; one is expected"
        )
    return matches[0]


def _scene_asset(assets: Sequence[AssetRecord], scene_id: str) -> AssetRecord:
    matches = [
        asset
        for asset in assets
        if asset.scene_id == scene_id and asset.asset_type in SCENE_ASSET_TYPES
    ]
    if not matches:
        raise RenderManifestError(
            f"scene {scene_id} has no imported image or video asset; run "
            f"import_assets first"
        )
    if len(matches) > 1:
        raise RenderManifestError(
            f"scene {scene_id} carries {len(matches)} media assets; one is expected"
        )
    return matches[0]


def _captions(store: JobStore, job_id: str) -> List[Dict[str, Any]]:
    """The §7 caption cues, which are also the manifest's scene timeline.

    The cues are the only reproducible timeline on disk: they were derived from
    the Master Voice timestamps, so a manifest built from them lines every scene
    slot up with the audio it is captioning. ``Scene.duration_target_ms`` is a
    *target* the synthesised voice does not have to hit and is deliberately not
    used here.
    """
    document = store.read_captions_document(job_id)
    if document is None:
        raise RenderManifestError(
            "the render manifest needs subtitles/captions.json; run "
            "generate_captions first"
        )
    cues = document.get("captions")
    if not isinstance(cues, list) or not cues:
        raise RenderManifestError("subtitles/captions.json carries no cues")
    ordered: List[Dict[str, Any]] = []
    for cue in cues:
        if not isinstance(cue, Mapping):
            raise RenderManifestError("a caption cue is not an object")
        ordered.append(dict(cue))
    ordered.sort(key=lambda cue: cue.get("srt_index", 0))
    return ordered


def _motion(asset: AssetRecord) -> RenderMotion:
    """Still images get the §8 example's Ken Burns move; footage moves already."""
    if asset.asset_type == IMAGE_ASSET_TYPE:
        return RenderMotion(
            type=MOTION_KEN_BURNS,
            scale_start=KEN_BURNS_SCALE_START,
            scale_end=KEN_BURNS_SCALE_END,
        )
    return RenderMotion(type=MOTION_NONE, scale_start=None, scale_end=None)


def _refuse_native_speech_avatar(
    store: JobStore,
    job_id: str,
    scenes: Mapping[str, Scene],
    entries: Sequence[RenderSceneEntry],
    assets: Mapping[str, AssetRecord],
) -> None:
    """SPEC-001:405's safety half. See the module docstring for why it refuses."""
    for entry in entries:
        scene = scenes.get(entry.scene_id)
        if scene is None or scene.visual_type not in AVATAR_VISUAL_TYPES:
            continue
        asset = assets[entry.asset_id]
        if asset.asset_type != VIDEO_ASSET_TYPE:
            continue
        path = store.asset_path(job_id, asset.storage_key)
        if not path.is_file():
            raise RenderManifestError(
                f"scene {entry.scene_id} is an avatar scene and its asset file is "
                f"missing, so whether it carries native speech cannot be decided"
            )
        if render_adapter.has_audio_stream(path):
            raise RenderManifestError(
                f"scene {entry.scene_id} is an avatar scene whose asset carries its "
                f"own audio track. SPEC-001:405 makes that track authoritative and "
                f"forbids the renderer overwriting it, but §8 puts audio.mode on the "
                f"manifest rather than the scene and RenderAudio.master_voice_asset_id "
                f"is required, so native_speech_avatar cannot be expressed for one "
                f"scene in V0. The scene must fall back to visual-only material "
                f"(SPEC-001:405, PRD-001:152)"
            )


def build_render_manifest(job: ContentJob, store: JobStore) -> RenderManifest:
    """Derive the §8 manifest for ``job`` from the documents already on disk.

    Pure derivation: nothing is persisted here and no state changes. The caller
    (:mod:`app.services.jobs.renderer`) writes it. Raises
    :class:`RenderManifestError` for anything the job cannot support.
    """
    job_id = job.content_job_id
    record = store.load(job_id)
    scenes = {scene.scene_id: scene for scene in record.scenes}
    if not scenes:
        raise RenderManifestError("the job has no planned scenes to render")

    voice = _one_asset(record.assets, AUDIO_ASSET_TYPE, "Master Voice")
    subtitle = _one_asset(record.assets, SUBTITLE_ASSET_TYPE, "subtitle")

    entries: List[RenderSceneEntry] = []
    used: Dict[str, AssetRecord] = {}
    for cue in _captions(store, job_id):
        scene_id = cue.get("scene_id")
        if scene_id not in scenes:
            raise RenderManifestError(
                f"caption cue names scene {scene_id!r}, which is not a scene of "
                f"this job"
            )
        asset = _scene_asset(record.assets, scene_id)
        used[asset.asset_id] = asset
        entries.append(
            RenderSceneEntry(
                scene_id=scene_id,
                asset_id=asset.asset_id,
                start_ms=_cue_time(cue, "start_ms", scene_id),
                end_ms=_cue_time(cue, "end_ms", scene_id),
                motion=_motion(asset),
                caption_ref=cue.get("caption_ref"),
            )
        )

    manifest = RenderManifest(
        content_job_id=job_id,
        canvas=RenderCanvas(
            width=CANVAS_WIDTH,
            height=CANVAS_HEIGHT,
            fps=CANVAS_FPS,
            pixel_format=PIXEL_FORMAT,
        ),
        audio=RenderAudio(
            mode=AUDIO_MODE_MASTER_VOICE,
            master_voice_asset_id=voice.asset_id,
            sample_rate=AUDIO_SAMPLE_RATE,
            codec=AUDIO_CODEC,
        ),
        scenes=entries,
        subtitle_asset_id=subtitle.asset_id,
        output=RenderOutput(
            container=CONTAINER, video_codec=VIDEO_CODEC, audio_codec=AUDIO_CODEC
        ),
    )
    _refuse_native_speech_avatar(store, job_id, scenes, entries, used)
    validate_render_manifest(manifest, record)
    return manifest


def _cue_time(cue: Mapping[str, Any], key: str, scene_id: str) -> int:
    value = cue.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RenderManifestError(
            f"caption cue for scene {scene_id} has a non-integer {key}: {value!r}"
        )
    return value


def validate_render_manifest(manifest: RenderManifest, record) -> RenderManifest:
    """Everything §8 requires that the pydantic model does not check.

    ``record`` is a :class:`~app.services.jobs.store.JobRecord`, so every id in
    the manifest is checked against the documents it claims to describe rather
    than against itself. Returns the manifest so a caller can inline the call;
    raises :class:`RenderManifestError` on the first thing that is wrong.
    """
    if manifest.content_job_id != record.job.content_job_id:
        raise RenderManifestError(
            f"the manifest belongs to {manifest.content_job_id!r}, not to "
            f"{record.job.content_job_id!r}"
        )

    canvas = manifest.canvas
    if (canvas.width, canvas.height) != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RenderManifestError(
            f"the render target is {CANVAS_WIDTH}x{CANVAS_HEIGHT}, not "
            f"{canvas.width}x{canvas.height}"
        )
    if canvas.fps <= 0:
        raise RenderManifestError(f"the canvas fps must be positive: {canvas.fps}")
    if canvas.pixel_format != PIXEL_FORMAT:
        raise RenderManifestError(
            f"the canvas pixel format must be {PIXEL_FORMAT!r}: "
            f"{canvas.pixel_format!r}"
        )

    output = manifest.output
    if (output.container, output.video_codec, output.audio_codec) != (
        CONTAINER,
        VIDEO_CODEC,
        AUDIO_CODEC,
    ):
        raise RenderManifestError(
            f"V0 renders {CONTAINER}/{VIDEO_CODEC}/{AUDIO_CODEC}, not "
            f"{output.container}/{output.video_codec}/{output.audio_codec}"
        )

    if manifest.audio.mode not in SUPPORTED_AUDIO_MODES:
        raise RenderManifestError(
            f"audio.mode {manifest.audio.mode!r} is not supported in V0 "
            f"(expected one of {sorted(SUPPORTED_AUDIO_MODES)})"
        )
    if manifest.audio.sample_rate <= 0:
        raise RenderManifestError(
            f"audio.sample_rate must be positive: {manifest.audio.sample_rate}"
        )
    if manifest.audio.codec != AUDIO_CODEC:
        raise RenderManifestError(
            f"audio.codec must be {AUDIO_CODEC!r}: {manifest.audio.codec!r}"
        )

    assets = {asset.asset_id: asset for asset in record.assets}
    voice = assets.get(manifest.audio.master_voice_asset_id)
    if voice is None or voice.asset_type != AUDIO_ASSET_TYPE:
        raise RenderManifestError(
            f"audio.master_voice_asset_id {manifest.audio.master_voice_asset_id!r} "
            f"does not resolve to an audio asset of this job"
        )
    if manifest.subtitle_asset_id is not None:
        subtitle = assets.get(manifest.subtitle_asset_id)
        if subtitle is None or subtitle.asset_type != SUBTITLE_ASSET_TYPE:
            raise RenderManifestError(
                f"subtitle_asset_id {manifest.subtitle_asset_id!r} does not resolve "
                f"to a subtitle asset of this job"
            )

    if not manifest.scenes:
        raise RenderManifestError("the manifest has no scenes to render")
    scene_ids = {scene.scene_id for scene in record.scenes}
    seen: set = set()
    previous_end = 0
    for position, entry in enumerate(manifest.scenes):
        if entry.scene_id not in scene_ids:
            raise RenderManifestError(
                f"the manifest names scene {entry.scene_id!r}, which is not a scene "
                f"of this job"
            )
        if entry.scene_id in seen:
            raise RenderManifestError(
                f"the manifest names scene {entry.scene_id!r} more than once"
            )
        seen.add(entry.scene_id)
        if entry.start_ms < 0 or entry.end_ms < 0:
            raise RenderManifestError(
                f"scene {entry.scene_id} has a negative slot: "
                f"{entry.start_ms} -> {entry.end_ms}"
            )
        if entry.end_ms <= entry.start_ms:
            raise RenderManifestError(
                f"scene {entry.scene_id} ends before it starts: "
                f"{entry.start_ms} -> {entry.end_ms}"
            )
        # Contiguous and non-overlapping: §8's scenes tile one timeline, so a
        # gap is a stretch of video with no scene behind it and an overlap is
        # two scenes claiming the same frames.
        if entry.start_ms != previous_end:
            raise RenderManifestError(
                f"scene {entry.scene_id} starts at {entry.start_ms} ms but the "
                f"{'timeline starts at 0' if position == 0 else 'previous scene ends'}"
                f" at {previous_end} ms; the slots must tile the timeline"
            )
        previous_end = entry.end_ms
        asset = assets.get(entry.asset_id)
        if asset is None or asset.asset_type not in SCENE_ASSET_TYPES:
            raise RenderManifestError(
                f"scene {entry.scene_id} names asset {entry.asset_id!r}, which does "
                f"not resolve to an image or video asset of this job"
            )
        if asset.scene_id is not None and asset.scene_id != entry.scene_id:
            raise RenderManifestError(
                f"scene {entry.scene_id} names asset {entry.asset_id!r}, which "
                f"belongs to scene {asset.scene_id!r}"
            )
        if entry.motion.type not in KNOWN_MOTION_TYPES:
            raise RenderManifestError(
                f"scene {entry.scene_id} asks for motion {entry.motion.type!r}, which "
                f"the renderer does not implement (expected one of "
                f"{sorted(KNOWN_MOTION_TYPES)})"
            )
        if entry.motion.type == MOTION_KEN_BURNS:
            for name, value in (
                ("scale_start", entry.motion.scale_start),
                ("scale_end", entry.motion.scale_end),
            ):
                if not isinstance(value, (int, float)) or value <= 0:
                    raise RenderManifestError(
                        f"scene {entry.scene_id}: ken_burns needs a positive "
                        f"{name}, got {value!r}"
                    )
    return manifest


def timeline_end_ms(manifest: RenderManifest) -> int:
    """Where the manifest's last scene ends. The render's declared length."""
    return max(entry.end_ms for entry in manifest.scenes)


def scene_source_paths(
    manifest: RenderManifest, store: JobStore, record
) -> Dict[str, Any]:
    """``asset_id -> guarded path`` for every scene the manifest renders."""
    assets = {asset.asset_id: asset for asset in record.assets}
    paths: Dict[str, Any] = {}
    for entry in manifest.scenes:
        asset = assets[entry.asset_id]
        paths[entry.asset_id] = store.asset_path(record.job.content_job_id, asset.storage_key)
    return paths


def subtitle_source_path(manifest: RenderManifest, store: JobStore, record) -> Optional[Any]:
    """The subtitle file to burn in, or ``None`` when the manifest names none."""
    if manifest.subtitle_asset_id is None:
        return None
    assets = {asset.asset_id: asset for asset in record.assets}
    return store.asset_path(
        record.job.content_job_id, assets[manifest.subtitle_asset_id].storage_key
    )
