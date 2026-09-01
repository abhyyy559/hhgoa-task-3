"""Tests for VisionService (CONTRACTS.md §1).

Covers, in order of model independence:
1. Schema contract: a valid VisionOutput with a 512-float embedding
   constructs and validates (and extra fields are rejected per contract).
2. ``select_primary_face`` pure selection policy — fake faces, no model.
3. ``detect_and_encode`` state machine via a fake FaceAnalysis app
   (monkeypatched singleton) — no model needed.
4. Lazy-load behaviour + real model integration test (``pytest.mark.slow``,
   skipped when the buffalo_l model pack is absent): random noise must yield
   NO_FACE_DETECTED, never a silent "success" with a face.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from typing import Any, List

import numpy as np
import pytest

from contracts.schemas import VisionOutput, VisionStatus
from services import vision as vision_service
from services.vision import (
    MIN_DET_SCORE,
    VisionModelNotReadyError,
    detect_and_encode,
    select_primary_face,
)

FakeFace = namedtuple("FakeFace", ["bbox", "det_score", "normed_embedding"])


def _fake_face(x1: float, y1: float, x2: float, y2: float, score: float = 0.9):
    return FakeFace(
        bbox=np.array([x1, y1, x2, y2], dtype=np.float32),
        det_score=np.float32(score),
        normed_embedding=np.ones(512, dtype=np.float32) / 8.0,
    )


# ---------------------------------------------------------------------------
# 1. Schema contract tests
# ---------------------------------------------------------------------------
class TestVisionOutputSchema:
    def test_valid_output_with_512_float_embedding(self) -> None:
        out = VisionOutput(
            face_id="7b2c9a34-6c3f-4a1e-9f0d-2e5b1c8a7d11",
            embedding=[0.001 * i for i in range(512)],
            bbox=[10, 20, 210, 220],
            quality_score=0.87,
            status=VisionStatus.OK,
        )
        assert out.status is VisionStatus.OK
        assert out.embedding is not None and len(out.embedding) == 512
        assert all(isinstance(v, float) for v in out.embedding)
        assert out.bbox == [10, 20, 210, 220]

    def test_null_embedding_states_are_valid(self) -> None:
        for status in (VisionStatus.NO_FACE_DETECTED, VisionStatus.LOW_IMAGE_QUALITY):
            out = VisionOutput(
                face_id="x", embedding=None, bbox=None, quality_score=0.0,
                status=status,
            )
            assert out.embedding is None

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            VisionOutput(
                face_id="x",
                embedding=None,
                bbox=None,
                quality_score=0.0,
                status=VisionStatus.OK,
                similarity_score=0.9,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# 2. select_primary_face — pure logic, fake faces, no model
# ---------------------------------------------------------------------------
class TestSelectPrimaryFace:
    def test_single_face_returned(self) -> None:
        face = _fake_face(0, 0, 100, 100)
        assert select_primary_face([face]) is face

    def test_largest_bbox_area_wins(self) -> None:
        small = _fake_face(0, 0, 50, 50)
        big = _fake_face(0, 0, 300, 300)
        assert select_primary_face([small, big]) is big

    def test_tie_break_most_centered(self) -> None:
        # Equal areas; the face whose center is closest to the mean center
        # of all candidates must win. With only two equal-area faces the mean
        # center is exactly the midpoint (distances always tie), so three
        # faces are needed to exercise the tie-break asymmetrically.
        top_left = _fake_face(0, 0, 100, 100)        # center (50, 50)
        central = _fake_face(150, 50, 250, 150)      # center (200, 100)
        bottom_right = _fake_face(300, 300, 400, 400)  # center (350, 350)
        assert (
            select_primary_face([top_left, central, bottom_right]) is central
        )

    def test_fully_symmetric_pair_is_deterministic(self) -> None:
        # With a symmetric pair of equal-area faces the tie-break cannot
        # distinguish them; the result must still be deterministic (the same
        # face on every call) rather than arbitrary.
        a = _fake_face(0, 0, 100, 100)
        b = _fake_face(200, 200, 300, 300)
        assert select_primary_face([a, b]) is select_primary_face([a, b])

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError):
            select_primary_face([])

    def test_all_faces_as_numpy_bboxes(self) -> None:
        faces = [_fake_face(0, 0, 10, 10), _fake_face(0, 0, 400, 400)]
        assert select_primary_face(faces) is faces[1]


# ---------------------------------------------------------------------------
# 3. detect_and_encode state machine via a fake app — no model needed
# ---------------------------------------------------------------------------
class FakeApp:
    """Mimics the insightface FaceAnalysis .get() surface."""

    def __init__(self, faces: List[Any]) -> None:
        self._faces = faces

    def get(self, image: np.ndarray) -> List[Any]:
        return self._faces


@pytest.fixture()
def patch_singleton(monkeypatch: pytest.MonkeyPatch):
    def _patch(faces: List[Any]) -> None:
        monkeypatch.setattr(vision_service, "_model", FakeApp(faces))

    return _patch

class TestDetectAndEncodeStates:
    def test_no_face_detected(self, patch_singleton) -> None:
        patch_singleton([])
        out = detect_and_encode(np.zeros((100, 100, 3), dtype=np.uint8))
        assert out.status is VisionStatus.NO_FACE_DETECTED
        assert out.embedding is None and out.bbox is None

    def test_ok_single_face(self, patch_singleton) -> None:
        patch_singleton([_fake_face(10, 10, 110, 110, score=0.95)])
        out = detect_and_encode(np.zeros((200, 200, 3), dtype=np.uint8))
        assert out.status is VisionStatus.OK
        assert out.embedding is not None and len(out.embedding) == 512
        assert out.bbox == [10, 10, 110, 110]
        assert out.quality_score == pytest.approx(0.95, abs=1e-6)

    def test_multiple_faces_selects_largest(self, patch_singleton) -> None:
        patch_singleton(
            [
                _fake_face(0, 0, 40, 40, score=0.9),
                _fake_face(0, 0, 300, 300, score=0.9),
            ]
        )
        out = detect_and_encode(np.zeros((400, 400, 3), dtype=np.uint8))
        assert out.status is VisionStatus.MULTIPLE_FACES_DETECTED
        # Deliberate: embedding of the auto-selected primary face is still
        # returned alongside the ambiguous status.
        assert out.embedding is not None and len(out.embedding) == 512
        assert out.bbox == [0, 0, 300, 300]

    def test_low_det_score_yields_low_image_quality(self, patch_singleton) -> None:
        patch_singleton([_fake_face(10, 10, 110, 110, score=MIN_DET_SCORE - 0.1)])
        out = detect_and_encode(np.zeros((200, 200, 3), dtype=np.uint8))
        assert out.status is VisionStatus.LOW_IMAGE_QUALITY
        assert out.embedding is None
        assert out.bbox == [10, 10, 110, 110]

    def test_bad_input_shape_raises(self, patch_singleton) -> None:
        patch_singleton([])
        with pytest.raises(ValueError):
            detect_and_encode(np.zeros((100, 100), dtype=np.uint8))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Lazy-load behaviour + real model integration (model-dependent)
# ---------------------------------------------------------------------------
MODEL_PACK_DIR = Path(vision_service.MODEL_ROOT) / "models" / "buffalo_l"
slow = pytest.mark.slow


class TestLazyLoading:
    def test_import_does_not_load_model(self) -> None:
        # Importing the module must not build the singleton: insightface is
        # only imported inside _get_model(), never at module import time.
        assert "insightface" not in vision_service.__dict__

    def test_missing_model_pack_raises_typed_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(vision_service, "MODEL_ROOT", str(tmp_path))
        monkeypatch.setattr(vision_service, "_model", None)
        with pytest.raises(VisionModelNotReadyError, match="buffalo_l"):
            vision_service._get_model()


@slow
@pytest.mark.skipif(
    not MODEL_PACK_DIR.is_dir(),
    reason="buffalo_l model pack not downloaded to ~/.insightface/models",
)
class TestModelIntegration:
    def test_random_noise_image_has_no_face(self) -> None:
        model = vision_service._get_model()
        assert model is not None  # singleton built once, reused after
        rng = np.random.default_rng(42)
        noise = rng.integers(0, 256, size=(640, 640, 3), dtype=np.uint8)
        out = detect_and_encode(np.asarray(noise, dtype=np.uint8))
        assert out.status is VisionStatus.NO_FACE_DETECTED
        assert out.embedding is None
        assert 0.0 <= out.quality_score <= 1.0

