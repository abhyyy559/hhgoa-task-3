"""VisionService — face detection + embedding (CONTRACTS.md §1).

Public API
----------
- ``detect_and_encode(image_bgr) -> VisionOutput``: run InsightFace
  ``buffalo_l`` detection + ArcFace recognition on a BGR numpy image and
  return the §1 payload validated by ``contracts.schemas.VisionOutput``.
- ``select_primary_face(faces) -> face``: pure, model-free selection of the
  primary face (largest bbox area, tie-break most centered) so the selection
  policy is unit-testable without downloading/loading the model pack.
- ``VisionModelNotReadyError``: typed error raised when the buffalo_l model
  pack is missing so the backend can surface an explicit, actionable failure
  instead of a silent empty result.

Deliberate design decisions
---------------------------
- **Lazy, thread-safe singleton.** The InsightFace ``FaceAnalysis`` object is
  built once per process behind a module-level ``threading.Lock`` with
  double-checked locking. Importing ``services.vision`` performs *no* model
  download and no heavy imports: ``insightface``/``onnxruntime`` are imported
  inside ``_get_model()`` on first use, so import stays cheap and no network
  access happens at import time.
- **Model pack readiness.** ``buffalo_l`` is pre-downloaded to
  ``~/.insightface/models/buffalo_l`` (override the root via the
  ``INSIGHTFACE_HOME`` env var). If the pack directory is missing or empty we
  raise ``VisionModelNotReadyError`` — never a silent empty result.
- **Multiple faces.** When detection finds more than one face we still return
  status ``MULTIPLE_FACES_DETECTED`` *and* the embedding of the primary face
  (auto-selected by ``select_primary_face``). This is deliberate: the query
  embedding is held by the backend (CONTRACTS.md §1) for later independent
  verification, so the pipeline can continue with a deterministic choice while
  the status flag tells upstream consumers that the input was ambiguous.
  Downstream verification remains fully independent of this choice.
- **Low detection confidence.** If the primary face's detector score is below
  ``MIN_DET_SCORE`` (default 0.5, tunable via the ``VISION_MIN_DET_SCORE`` env
  var) we return ``LOW_IMAGE_QUALITY`` with ``embedding=None``. A weak
  detection would produce a noisy query embedding that could poison the
  similarity comparison downstream, so we refuse to emit one.
- **CPU only.** Providers are pinned to ``["CPUExecutionProvider"]`` with
  ``ctx_id=-1`` and ``det_size=(640, 640)`` for reproducibility and to keep
  the demo GPU-free.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any, List, Optional, Protocol

import numpy as np

from contracts.schemas import VisionOutput, VisionStatus

# ---------------------------------------------------------------------------
# Configuration (env-driven; no secrets — only local paths / thresholds)
# ---------------------------------------------------------------------------
MODEL_NAME: str = "buffalo_l"
MODEL_ROOT: str = os.getenv("INSIGHTFACE_HOME", os.path.expanduser("~/.insightface"))
PROVIDERS: List[str] = ["CPUExecutionProvider"]

#: Detector confidence below which we refuse to emit an embedding
#: (status LOW_IMAGE_QUALITY). Tunable via VISION_MIN_DET_SCORE; default 0.5
#: matches insightface's own det_thresh so behaviour is predictable.
MIN_DET_SCORE: float = float(os.getenv("VISION_MIN_DET_SCORE", "0.5"))

#: Module-level singleton slot + lock (lazy, double-checked locking).
_model: Any = None
_model_lock = threading.Lock()


class VisionModelNotReadyError(RuntimeError):
    """Raised when the buffalo_l model pack is missing or not yet downloaded."""


class FaceLike(Protocol):
    """Structural type for a detected face (real insightface Face or a fake).

    Only the attributes used by ``select_primary_face`` / ``detect_and_encode``
    are declared, which keeps the selection logic unit-testable with simple
    namedtuple fakes.
    """

    bbox: np.ndarray  # [x1, y1, x2, y2]
    det_score: Any  # float or 1-element array
    normed_embedding: Optional[np.ndarray]  # 512 floats or None


def _model_pack_path() -> Path:
    """Absolute path of the buffalo_l model pack directory."""
    return Path(MODEL_ROOT) / "models" / MODEL_NAME


def _get_model() -> Any:
    """Thread-safe lazy singleton around the InsightFace FaceAnalysis app.

    Heavy imports (insightface, onnxruntime) happen here — on first call —
    never at module import time, so importing ``services.vision`` stays light
    and never triggers a model download.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # double-checked locking
                pack_dir = _model_pack_path()
                if not pack_dir.is_dir() or not any(pack_dir.iterdir()):
                    raise VisionModelNotReadyError(
                        f"InsightFace model pack '{MODEL_NAME}' not found at "
                        f"{pack_dir}. Pre-download it (e.g. the project's "
                        f"model download script) or set INSIGHTFACE_HOME to "
                        f"the directory containing models/{MODEL_NAME}."
                    )
                # Deferred heavy imports: nothing is downloaded or loaded at
                # import time of services.vision.
                from insightface.app import FaceAnalysis  # noqa: PLC0415

                app = FaceAnalysis(
                    name=MODEL_NAME,
                    root=str(MODEL_ROOT),
                    providers=PROVIDERS,
                )
                app.prepare(ctx_id=-1, det_size=(640, 640))
                _model = app
    return _model


def load_face_app() -> Any:
    """Public accessor for VerificationService: same singleton, fresh call.

    VerificationService runs its own detection/encoding pass through this
    seam — independence there is a separate *execution path* (separate fetch,
    separate detect, separate encode, separate score), not a separate model.
    Stated precisely per TASK3_ARCHITECTURE.md §10 rather than overclaimed.
    """
    return _model


def _as_float(value: Any) -> float:
    """Coerce insightface's scalar-ish values (np.float32 / shape-(1,) array)."""
    return float(np.asarray(value).reshape(-1)[0])


def _bbox_list(bbox: Any) -> List[int]:
    """Round a [x1, y1, x2, y2] bbox to the contract's list[int]."""
    return [int(round(float(v))) for v in np.asarray(bbox).reshape(-1)[:4]]


def select_primary_face(faces: List[FaceLike]) -> FaceLike:
    """Pure selection policy: largest bbox area, tie-break most centered.

    "Most centered" is defined without reference to the source image (which
    this function deliberately does not receive): among equal-area faces, the
    one whose bbox center is closest to the *mean center of all candidate
    bboxes* wins. This is deterministic and image-independent.

    Raises ``ValueError`` on an empty list — callers handle the "no face"
    state explicitly via ``VisionStatus.NO_FACE_DETECTED``.
    """
    if not faces:
        raise ValueError("select_primary_face() requires at least one face")

    def _center(face: FaceLike) -> tuple[float, float]:
        x1, y1, x2, y2 = (float(v) for v in np.asarray(face.bbox).reshape(-1)[:4])
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def _area(face: FaceLike) -> float:
        x1, y1, x2, y2 = (float(v) for v in np.asarray(face.bbox).reshape(-1)[:4])
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    centers = [_center(f) for f in faces]
    mean_cx = sum(c[0] for c in centers) / len(centers)
    mean_cy = sum(c[1] for c in centers) / len(centers)

    def _key(face: FaceLike) -> tuple[float, float]:
        cx, cy = _center(face)
        return (_area(face), -((cx - mean_cx) ** 2 + (cy - mean_cy) ** 2))

    return max(faces, key=_key)




def detect_and_encode(image_bgr: np.ndarray) -> VisionOutput:
    """Detect faces and build the CONTRACTS.md §1 VisionService payload.

    States (never a silent empty result):
    - no faces found            -> NO_FACE_DETECTED, embedding None
    - >1 faces                  -> MULTIPLE_FACES_DETECTED, embedding of the
                                   auto-selected primary face (see module
                                   docstring: deliberate choice so the
                                   pipeline continues with a deterministic
                                   face while flagging the ambiguity)
    - det_score < MIN_DET_SCORE -> LOW_IMAGE_QUALITY, embedding None
    - exactly one solid face    -> OK, 512-float normed embedding

    Raises:
        VisionModelNotReadyError: if the buffalo_l model pack is missing.
        ValueError: if ``image_bgr`` is not an HxWx3 ndarray.
    """
    arr = np.asarray(image_bgr)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(
            f"image_bgr must be an HxWx3 BGR ndarray, got shape {arr.shape}"
        )

    app = _get_model()
    faces: List[FaceLike] = list(app.get(arr))

    if not faces:
        return VisionOutput(
            face_id=str(uuid.uuid4()),
            embedding=None,
            bbox=None,
            quality_score=0.0,
            status=VisionStatus.NO_FACE_DETECTED,
        )

    primary = select_primary_face(faces)
    det_score = _as_float(primary.det_score)
    bbox = _bbox_list(primary.bbox)
    face_id = str(uuid.uuid4())

    if det_score < MIN_DET_SCORE:
        return VisionOutput(
            face_id=face_id,
            embedding=None,
            bbox=bbox,
            quality_score=det_score,
            status=VisionStatus.LOW_IMAGE_QUALITY,
        )

    raw_embedding = getattr(primary, "normed_embedding", None)
    embedding = (
        [float(v) for v in np.asarray(raw_embedding).reshape(-1)]
        if raw_embedding is not None
        else None
    )

    status = (
        VisionStatus.MULTIPLE_FACES_DETECTED if len(faces) > 1 else VisionStatus.OK
    )
    return VisionOutput(
        face_id=face_id,
        embedding=embedding,
        bbox=bbox,
        quality_score=det_score,
        status=status,
    )
