"""Enrolled Identity Directory (Campus & Enterprise Common Person Gallery).

This service allows common individuals (students, teammates, staff, campus
members) to be enrolled into an identity registry. When reverse-image queries
are executed, the gallery provides candidate profile URLs that can be
independently fetched and verified by VerificationService via standard HTTP/HTML
parsers, upholding zero-trust contract law with zero shared scoring state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from contracts.schemas import SearchCandidate, SourceType

GALLERY_DIR = Path(__file__).resolve().parent.parent / "data" / "gallery"
GALLERY_INDEX_FILE = GALLERY_DIR / "gallery_index.json"
GALLERY_LOCK = threading.Lock()


@dataclass
class EnrolledMember:
    member_id: str
    full_name: str
    role: str
    organization: str
    bio: str
    image_filename: str
    enrolled_at: str
    profile_url: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _ensure_gallery_dirs() -> None:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    images_dir = GALLERY_DIR / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    if not GALLERY_INDEX_FILE.exists():
        with open(GALLERY_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"members": []}, f, indent=2)


def _load_gallery_raw() -> list[dict[str, Any]]:
    _ensure_gallery_dirs()
    try:
        with open(GALLERY_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("members", [])
    except Exception:
        return []


def _save_gallery_raw(members: list[dict[str, Any]]) -> None:
    _ensure_gallery_dirs()
    with open(GALLERY_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump({"members": members}, f, indent=2)


def list_enrolled_members() -> list[EnrolledMember]:
    """List all registered campus/enterprise common members."""
    with GALLERY_LOCK:
        raw_list = _load_gallery_raw()
        members = []
        for item in raw_list:
            members.append(
                EnrolledMember(
                    member_id=item["member_id"],
                    full_name=item["full_name"],
                    role=item["role"],
                    organization=item["organization"],
                    bio=item["bio"],
                    image_filename=item["image_filename"],
                    enrolled_at=item["enrolled_at"],
                    profile_url=item["profile_url"],
                    metadata=item.get("metadata", {}),
                )
            )
        return members


def get_member(member_id: str) -> Optional[EnrolledMember]:
    """Retrieve an enrolled member by ID."""
    with GALLERY_LOCK:
        raw_list = _load_gallery_raw()
        for item in raw_list:
            if item["member_id"] == member_id:
                return EnrolledMember(
                    member_id=item["member_id"],
                    full_name=item["full_name"],
                    role=item["role"],
                    organization=item["organization"],
                    bio=item["bio"],
                    image_filename=item["image_filename"],
                    enrolled_at=item["enrolled_at"],
                    profile_url=item["profile_url"],
                    metadata=item.get("metadata", {}),
                )
        return None


def enroll_member(
    full_name: str,
    role: str,
    organization: str,
    bio: str,
    image_bytes: bytes,
    *,
    base_url: str = "http://127.0.0.1:8000",
    metadata: Optional[dict[str, Any]] = None,
) -> EnrolledMember:
    """Enroll a new identity into the common person directory."""
    _ensure_gallery_dirs()
    member_id = str(uuid.uuid4())[:8]
    ext = "jpg"
    image_filename = f"{member_id}.{ext}"
    image_path = GALLERY_DIR / "images" / image_filename

    with open(image_path, "wb") as f:
        f.write(image_bytes)

    profile_url = f"{base_url.rstrip('/')}/api/gallery/profiles/{member_id}"
    now_iso = datetime.now(timezone.utc).isoformat()

    new_member = EnrolledMember(
        member_id=member_id,
        full_name=full_name,
        role=role,
        organization=organization,
        bio=bio,
        image_filename=image_filename,
        enrolled_at=now_iso,
        profile_url=profile_url,
        metadata=metadata or {},
    )

    with GALLERY_LOCK:
        raw_list = _load_gallery_raw()
        raw_list.append(asdict(new_member))
        _save_gallery_raw(raw_list)

    return new_member


def get_member_image_bytes(member_id: str) -> Optional[bytes]:
    """Return raw photo bytes of an enrolled member."""
    member = get_member(member_id)
    if not member:
        return None
    image_path = GALLERY_DIR / "images" / member.image_filename
    if not image_path.exists():
        return None
    return image_path.read_bytes()


def generate_profile_html(member: EnrolledMember, base_url: str = "http://127.0.0.1:8000") -> str:
    """Generate canonical profile HTML with <img src> and og:image tags."""
    img_src = f"{base_url.rstrip('/')}/api/gallery/images/{member.image_filename}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{member.full_name} — {member.organization} Directory</title>
<meta property="og:title" content="{member.full_name}">
<meta property="og:description" content="{member.role} at {member.organization}">
<meta property="og:image" content="{img_src}">
<meta property="og:type" content="profile">
</head>
<body>
<article class="profile-card">
  <h1>{member.full_name}</h1>
  <p class="role">{member.role} &mdash; {member.organization}</p>
  <img src="{img_src}" alt="{member.full_name}" class="profile-photo" />
  <p class="bio">{member.bio}</p>
  <div class="metadata">
    <span>Member ID: {member.member_id}</span>
    <span>Enrolled: {member.enrolled_at}</span>
  </div>
</article>
</body>
</html>"""


def search_gallery_candidates(base_url: str = "http://127.0.0.1:8000") -> list[SearchCandidate]:
    """Generate search candidates for all enrolled gallery members.

    Zero-scoring adherence: returns pure SearchCandidate objects with candidate_url,
    candidate_id, and thumbnail_url. NO similarity or scoring is computed here.
    """
    members = list_enrolled_members()
    candidates: list[SearchCandidate] = []
    for m in members:
        prof_url = f"{base_url.rstrip('/')}/api/gallery/profiles/{m.member_id}"
        cid = hashlib.sha1(prof_url.encode("utf-8")).hexdigest()[:12]
        thumb_url = f"{base_url.rstrip('/')}/api/gallery/images/{m.image_filename}"
        candidates.append(
            SearchCandidate(
                candidate_id=cid,
                candidate_url=prof_url,
                source_type=SourceType.WEB,
                thumbnail_url=thumb_url,
            )
        )
    return candidates


def seed_default_demo_identities() -> None:
    """Seed initial sample team/campus identities if gallery is empty."""
    _ensure_gallery_dirs()
    with GALLERY_LOCK:
        existing = _load_gallery_raw()
        if existing:
            return  # Already seeded or has members

    # Seed with REAL faces so independent verification can actually detect and
    # score them. Cartoon/synthetic avatars are correctly rejected by the face
    # detector (NO_FACE_DETECTED), which would make every gallery candidate
    # unmatchable — a broken demo. skimage ships public-domain photos with real
    # faces (astronaut = Eileen Collins, camera = the classic "cameraman");
    # these give the pipeline genuine identities to match against end-to-end.
    initial_profiles = [
        {
            "full_name": "Aarav Sharma",
            "role": "Lead Security Researcher",
            "organization": "HH Goa Intelligence Lab",
            "bio": "Specializes in biometric verification protocols and zero-knowledge cryptographic proofs.",
            "face": "astronaut",
        },
        {
            "full_name": "Priya Nair",
            "role": "Distributed Systems Engineer",
            "organization": "HH Goa Cyber Defense",
            "bio": "Core developer of Polygon Amoy immutable state contracts and decentralized identity registries.",
            "face": "camera",
        },
    ]

    for p in initial_profiles:
        image_bytes = _real_face_jpeg(p["face"])
        if image_bytes is None:
            continue  # sample data unavailable — skip rather than seed a faceless entry
        enroll_member(
            full_name=p["full_name"],
            role=p["role"],
            organization=p["organization"],
            bio=p["bio"],
            image_bytes=image_bytes,
        )


def _real_face_jpeg(sample_name: str) -> Optional[bytes]:
    """Return JPEG bytes of a real, detectable face from skimage sample data.

    Returns ``None`` if skimage is unavailable or the sample cannot be loaded,
    so seeding degrades to "no entry" rather than a faceless placeholder.
    """
    try:
        from skimage import data as _sk_data  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — optional dependency, seeding is best-effort
        return None
    try:
        arr = np.asarray(getattr(_sk_data, sample_name)())
        if arr.ndim == 2:  # grayscale → BGR
            bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        elif arr.shape[2] == 4:  # RGBA → BGR
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        else:  # RGB → BGR
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr)
        return buf.tobytes() if ok else None
    except Exception:  # noqa: BLE001
        return None
