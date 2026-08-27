"""File-backed persistence for one job directory.

V0 has no database: the files under ``storage/jobs/<content_job_id>/`` are the
truth (PLAN-001 Q1). Layout::

    <root>/<content_job_id>/
        job.json
        scripts/script.json
        scenes/scene-NNN.json
        assets/assets.jsonl
        scenes/<scene_id>/images/        <- where a human drops generated stills
        scenes/<scene_id>/videos/        <- and generated clips (SPEC-001 3.2)
        provider_events.jsonl
        usage_ledger.jsonl
        decisions.jsonl
        generation_manifest.json
        render_manifest.json

The per-scene media directories are the SPEC-001 3.2 shape, which PLAN-001 Q9
names explicitly. The rest of this layout predates them and already diverges
from 3.2 (flat ``scene-NNN.json`` instead of ``scenes/<scene_id>/scene.json``,
no ``audit/`` prefix); that divergence is issue #1's and is not widened here.
A ``scenes/<scene_id>/`` directory cannot collide with a ``scene-NNN.json``
document because every glob over that directory matches ``scene-*.json``.

Single-file writes go through ``os.replace`` so a crash never leaves a
half-written JSON document. The ``.jsonl`` files are append-only: neither
``save`` nor ``replace`` rewrites them.

``generation_manifest.json`` and the ``scenes/<scene_id>/<kind>/`` directories
are scene-planner owned and deliberately *outside* the :class:`JobRecord`
document set, so ``replace`` can never delete them — those directories may hold
files a human placed there by hand. Use
:meth:`JobStore.write_generation_manifest` and :meth:`JobStore.scene_media_dir`.

Every path a read or write touches is resolved with ``os.path.realpath`` and
proven to sit under the store root before it is opened, so a symlinked job
directory *or* a symlinked ``scripts``/``scenes``/``assets`` subdirectory cannot
be used to reach outside the root.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Union

from app.models.content_job import (
    AssetRecord,
    ContentJob,
    GenerationManifest,
    ProviderEvent,
    RenderManifest,
    Scene,
    Script,
    UsageLedgerEntry,
)

#: A job id is an opaque token: no path separators, no dots-only names, no NUL.
#: ``:`` is excluded on top of that because it is the separator
#: ``budget.build_idempotency_key`` splits on — a job id carrying one is
#: unusable there, and accepting it here only moves the failure to after a
#: provider call has already been made. Scene ids become directory names under
#: ``assets/scenes/`` and are held to the same rule.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_ID_PATTERN = _ID_PATTERN

JOB_FILE = "job.json"
SCRIPT_FILE = os.path.join("scripts", "script.json")
SCENES_DIR = "scenes"
ASSETS_FILE = os.path.join("assets", "assets.jsonl")
#: SPEC-001 3.2 names these four per-scene subdirectories. Only the ones a
#: caller asks for are created; an unlisted name is refused rather than turned
#: into an arbitrary directory under the job.
SCENE_MEDIA_KINDS = frozenset({"images", "videos", "references", "qa"})
PROVIDER_EVENTS_FILE = "provider_events.jsonl"
USAGE_LEDGER_FILE = "usage_ledger.jsonl"
DECISIONS_FILE = "decisions.jsonl"
GENERATION_MANIFEST_FILE = "generation_manifest.json"
RENDER_MANIFEST_FILE = "render_manifest.json"

# append_event routes by record type; each contract owns exactly one file.
_APPEND_TARGETS = (
    (AssetRecord, ASSETS_FILE),
    (ProviderEvent, PROVIDER_EVENTS_FILE),
    (UsageLedgerEntry, USAGE_LEDGER_FILE),
)

AppendableEvent = Union[AssetRecord, ProviderEvent, UsageLedgerEntry]


class JobStoreError(ValueError):
    """The job id, job directory or job file is not usable."""


@dataclass
class JobRecord:
    """Everything persisted for one job. Only ``job`` is mandatory.

    The defaults are not "leave these alone": they mean *this job has no
    script, no scenes and no render manifest*. Handing a default-constructed
    record to :meth:`JobStore.replace` therefore deletes every document except
    ``job.json``. To persist a state transition, pass the bare
    :class:`ContentJob` to :meth:`JobStore.save` instead.
    """

    job: ContentJob
    script: Optional[Script] = None
    scenes: List[Scene] = field(default_factory=list)
    render_manifest: Optional[RenderManifest] = None
    assets: List[AssetRecord] = field(default_factory=list)
    provider_events: List[ProviderEvent] = field(default_factory=list)
    usage_ledger: List[UsageLedgerEntry] = field(default_factory=list)
    decisions: List[dict] = field(default_factory=list)


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)  # caller must guard the path first
    handle, temp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        # ponytail: best-effort cleanup; the temp file is inside the job dir.
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobStoreError(f"{label} is not readable UTF-8 JSON: {path.name}") from exc


def _read_jsonl(path: Path) -> List[Any]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise JobStoreError(f"{path.name} is not readable UTF-8 text") from exc
    records = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise JobStoreError(f"{path.name} line {number} is not valid JSON") from exc
    return records


def _as_decision(payload: Any, number: int) -> dict:
    """A decision line is a JSON object; anything else would break the type."""
    if not isinstance(payload, dict):
        raise JobStoreError(
            f"{DECISIONS_FILE} line {number} is not a JSON object: {type(payload).__name__}"
        )
    return payload


class JobStore:
    """Read and write one job directory tree under ``root``."""

    def __init__(self, root: Union[str, os.PathLike]):
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- public interface -------------------------------------------------

    def create(self, job: Union[ContentJob, JobRecord]) -> Path:
        """Write a fresh job directory. Fails if the job already exists."""
        record = job if isinstance(job, JobRecord) else JobRecord(job=job)
        job_dir = self._job_dir(record.job.content_job_id, must_exist=False)
        if job_dir.exists():
            raise JobStoreError(f"job already exists: {record.job.content_job_id}")

        job_dir.mkdir(parents=True)
        self._write_documents(job_dir, record)
        for relative in (
            ASSETS_FILE,
            PROVIDER_EVENTS_FILE,
            USAGE_LEDGER_FILE,
            DECISIONS_FILE,
        ):
            path = self._within_root(job_dir / relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for asset in record.assets:
            self._append_line(job_dir / ASSETS_FILE, asset.model_dump(mode="json"))
        for event in record.provider_events:
            self._append_line(
                job_dir / PROVIDER_EVENTS_FILE, event.model_dump(mode="json")
            )
        for entry in record.usage_ledger:
            self._append_line(job_dir / USAGE_LEDGER_FILE, entry.model_dump(mode="json"))
        for decision in record.decisions:
            self._append_line(job_dir / DECISIONS_FILE, decision)
        return job_dir

    def load(self, job_id: str) -> JobRecord:
        """Read the whole job directory back into validated models."""
        job_dir = self._job_dir(job_id)
        job_file = self._within_root(job_dir / JOB_FILE)
        if not job_file.is_file():
            raise JobStoreError(f"job file is missing for job: {job_id}")

        script_file = self._within_root(job_dir / SCRIPT_FILE)
        manifest_file = self._within_root(job_dir / RENDER_MANIFEST_FILE)
        scenes_dir = job_dir / SCENES_DIR
        scene_files = (
            sorted(scenes_dir.glob("scene-*.json")) if scenes_dir.is_dir() else []
        )
        for path in (
            scenes_dir / "scene-000.json",
            *scene_files,
            job_dir / ASSETS_FILE,
            job_dir / PROVIDER_EVENTS_FILE,
            job_dir / USAGE_LEDGER_FILE,
            job_dir / DECISIONS_FILE,
        ):
            self._within_root(path)

        return JobRecord(
            job=self._validate(ContentJob, _read_json(job_file, "job.json"), JOB_FILE),
            script=(
                self._validate(Script, _read_json(script_file, "script.json"), SCRIPT_FILE)
                if script_file.is_file()
                else None
            ),
            scenes=[
                self._validate(Scene, _read_json(path, "scene"), path.name)
                for path in scene_files
            ],
            render_manifest=(
                self._validate(
                    RenderManifest,
                    _read_json(manifest_file, "render_manifest.json"),
                    RENDER_MANIFEST_FILE,
                )
                if manifest_file.is_file()
                else None
            ),
            assets=[
                self._validate(AssetRecord, payload, ASSETS_FILE)
                for payload in _read_jsonl(job_dir / ASSETS_FILE)
            ],
            provider_events=[
                self._validate(ProviderEvent, payload, PROVIDER_EVENTS_FILE)
                for payload in _read_jsonl(job_dir / PROVIDER_EVENTS_FILE)
            ],
            usage_ledger=[
                self._validate(UsageLedgerEntry, payload, USAGE_LEDGER_FILE)
                for payload in _read_jsonl(job_dir / USAGE_LEDGER_FILE)
            ],
            decisions=[
                _as_decision(payload, number)
                for number, payload in enumerate(
                    _read_jsonl(job_dir / DECISIONS_FILE), start=1
                )
            ],
        )

    def save(self, job: ContentJob) -> Path:
        """Rewrite ``job.json`` only — the safe default after a state transition.

        Every other document is left exactly as it is, so the common "update the
        status" call cannot wipe the rest of the job. A :class:`JobRecord` is
        rejected rather than quietly treated as the whole document set.
        """
        if isinstance(job, JobRecord):
            raise JobStoreError(
                "save() rewrites job.json only and takes a ContentJob; "
                "use replace() to rewrite the whole document set"
            )
        job_dir = self._job_dir(job.content_job_id)
        self._write_guarded(job_dir / JOB_FILE, _dump(job))
        return job_dir

    def replace(self, record: JobRecord) -> Path:
        """Replace every single-file document with exactly what ``record`` holds.

        Destructive by design: a :class:`JobRecord` is the whole document set,
        not a patch. Scenes missing from ``record.scenes`` have their
        ``scene-NNN.json`` deleted and a ``None`` ``script``/``render_manifest``
        deletes that file, so a removed scene cannot come back on the next
        ``load`` — and ``replace(JobRecord(job=job))`` wipes all of them.
        Append-only ``.jsonl`` files are never touched.
        """
        if not isinstance(record, JobRecord):
            raise JobStoreError(
                "replace() rewrites the whole document set and takes a JobRecord; "
                "use save() to rewrite job.json only"
            )
        job_dir = self._job_dir(record.job.content_job_id)
        self._write_documents(job_dir, record)
        return job_dir

    def append_event(self, job_id: str, event: AppendableEvent) -> None:
        """Append one asset, provider event or usage ledger entry."""
        for model_type, relative in _APPEND_TARGETS:
            if isinstance(event, model_type):
                job_dir = self._job_dir(job_id)
                self._append_line(job_dir / relative, event.model_dump(mode="json"))
                return
        raise JobStoreError(
            "append_event accepts AssetRecord, ProviderEvent or UsageLedgerEntry, "
            f"got {type(event).__name__}"
        )

    def append_decision(self, job_id: str, record: Mapping[str, Any]) -> None:
        """Append one decision/transition record to ``decisions.jsonl``."""
        if not isinstance(record, Mapping):
            raise JobStoreError("decision record must be a mapping")
        job_dir = self._job_dir(job_id)
        self._append_line(job_dir / DECISIONS_FILE, dict(record))

    @staticmethod
    def _require_scene_media(scene_id: str, kind: str) -> None:
        if (
            not isinstance(scene_id, str)
            or ".." in scene_id
            or not _ID_PATTERN.fullmatch(scene_id)
        ):
            raise JobStoreError(
                f"scene_id must be an opaque token without path separators: {scene_id!r}"
            )
        if kind not in SCENE_MEDIA_KINDS:
            raise JobStoreError(
                f"unknown scene media directory: {kind!r} "
                f"(expected one of {sorted(SCENE_MEDIA_KINDS)})"
            )

    def scene_media_dir(self, job_id: str, scene_id: str, kind: str) -> Path:
        """Create — idempotently — and return one scene's human-import directory.

        Creation only: whatever a human already dropped in survives a replan,
        which is why this lives outside the destructive :meth:`replace` path.
        """
        self._require_scene_media(scene_id, kind)
        job_dir = self._job_dir(job_id)
        path = job_dir / SCENES_DIR / scene_id / kind
        self._within_root(path)
        path.mkdir(parents=True, exist_ok=True)
        # Re-checked after the mkdir: a symlinked ``scenes/<scene_id>`` would
        # have been followed by ``parents=True`` and is only visible now.
        return self._within_root(path)

    def scene_media_relative_dir(self, scene_id: str, kind: str) -> str:
        """``scene_media_dir``'s path as recorded in the generation manifest.

        Relative to the job directory and always POSIX-separated, so a manifest
        written on one machine still resolves on another. Validated exactly
        like :meth:`scene_media_dir`: a caller must not be able to mint an
        escaping path here just because nothing is created.
        """
        self._require_scene_media(scene_id, kind)
        return f"{Path(SCENES_DIR).as_posix()}/{scene_id}/{kind}"

    def write_generation_manifest(
        self, job_id: str, manifest: GenerationManifest
    ) -> Path:
        """Write the §6.1 generation manifest for ``job_id``."""
        if not isinstance(manifest, GenerationManifest):
            raise JobStoreError(
                "write_generation_manifest takes a GenerationManifest, "
                f"got {type(manifest).__name__}"
            )
        if manifest.content_job_id != job_id:
            raise JobStoreError(
                f"generation manifest belongs to {manifest.content_job_id!r}, "
                f"not to {job_id!r}"
            )
        job_dir = self._job_dir(job_id)
        path = job_dir / GENERATION_MANIFEST_FILE
        self._write_guarded(path, _dump(manifest))
        return path

    def read_generation_manifest(self, job_id: str) -> Optional[GenerationManifest]:
        """Read the §6.1 generation manifest, or ``None`` when none was written."""
        job_dir = self._job_dir(job_id)
        path = self._within_root(job_dir / GENERATION_MANIFEST_FILE)
        if not path.is_file():
            return None
        return self._validate(
            GenerationManifest,
            _read_json(path, GENERATION_MANIFEST_FILE),
            GENERATION_MANIFEST_FILE,
        )

    # -- internals --------------------------------------------------------

    def _within_root(self, path: Path) -> Path:
        """Prove ``path`` resolves inside the root, following every symlink.

        ``mkdir(parents=True, exist_ok=True)`` happily reuses an existing
        symlinked subdirectory, so checking the job directory alone is not
        enough: each file is re-checked immediately before it is opened. The
        prefix comparison carries the separator so ``<root>evil`` is not read
        as living under ``<root>``.
        """
        root = os.path.realpath(self._root)
        resolved = os.path.realpath(path)
        if not resolved.startswith(root + os.sep):
            raise JobStoreError(f"path escapes the store root: {path}")
        return path

    def _write_guarded(self, path: Path, text: str) -> None:
        _write_atomic(self._within_root(path), text)

    def _remove_guarded(self, path: Path) -> None:
        self._within_root(path)
        if path.exists():
            path.unlink()

    def _job_dir(self, job_id: Any, *, must_exist: bool = True) -> Path:
        """Resolve ``job_id`` to a directory proven to live inside the root."""
        if (
            not isinstance(job_id, str)
            or ".." in job_id
            or not _JOB_ID_PATTERN.fullmatch(job_id)
        ):
            raise JobStoreError(
                f"content_job_id must be an opaque token without path separators: {job_id!r}"
            )
        root = os.path.realpath(self._root)
        candidate = os.path.realpath(os.path.join(root, job_id))
        # The separator excludes both a sibling sharing the prefix ("<root>evil")
        # and the root itself: a symlink aliasing the root would otherwise let a
        # job's documents land loose among the job directories.
        if candidate == root or not candidate.startswith(root + os.sep):
            raise JobStoreError(f"job directory escapes the store root: {job_id}")
        job_dir = Path(candidate)
        if must_exist and not job_dir.is_dir():
            raise JobStoreError(f"job does not exist: {job_id}")
        return job_dir

    def _write_documents(self, job_dir: Path, record: JobRecord) -> None:
        """Replace the single-file documents with exactly what ``record`` holds.

        Validated first, so a rejected record leaves the directory untouched:
        every scene must carry this job's ``content_job_id`` and a unique
        ``scene_index``, and so must the render manifest. Then ``scene-NNN.json``
        files no longer in ``record.scenes`` are deleted, and a ``None``
        ``script``/``render_manifest`` deletes its file: what is not in the
        record does not survive in the directory.
        """
        job_id = record.job.content_job_id
        scene_names = {}
        for scene in record.scenes:
            if scene.content_job_id != job_id:
                raise JobStoreError(
                    f"scene {scene.scene_id} belongs to {scene.content_job_id!r}, "
                    f"not to {job_id!r}"
                )
            if scene.scene_index in scene_names:
                raise JobStoreError(f"duplicate scene_index: {scene.scene_index}")
            scene_names[scene.scene_index] = f"scene-{scene.scene_index:03d}.json"
        manifest = record.render_manifest
        if manifest is not None and manifest.content_job_id != job_id:
            raise JobStoreError(
                f"render manifest belongs to {manifest.content_job_id!r}, "
                f"not to {job_id!r}"
            )

        self._write_guarded(job_dir / JOB_FILE, _dump(record.job))
        if record.script is not None:
            self._write_guarded(job_dir / SCRIPT_FILE, _dump(record.script))
        else:
            self._remove_guarded(job_dir / SCRIPT_FILE)
        for scene in record.scenes:
            self._write_guarded(
                job_dir / SCENES_DIR / scene_names[scene.scene_index], _dump(scene)
            )
        self._prune_scenes(job_dir, set(scene_names.values()))
        if manifest is not None:
            self._write_guarded(job_dir / RENDER_MANIFEST_FILE, _dump(manifest))
        else:
            self._remove_guarded(job_dir / RENDER_MANIFEST_FILE)

    def _prune_scenes(self, job_dir: Path, keep: set) -> None:
        scenes_dir = job_dir / SCENES_DIR
        if not scenes_dir.is_dir():
            return
        self._within_root(scenes_dir / "scene-000.json")
        for path in scenes_dir.glob("scene-*.json"):
            if path.name not in keep:
                self._remove_guarded(path)

    def _append_line(self, path: Path, payload: Any) -> None:
        self._within_root(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _validate(model, payload: Any, label: str):
        try:
            return model.model_validate(payload)
        except Exception as exc:
            raise JobStoreError(f"{label} does not match {model.__name__}: {exc}") from exc
