"""Tests for Enrolled Identity Directory (Campus & Enterprise Common Person Gallery).

Tests cover:
- Enrollment of common individuals (students, teammates, campus members)
- Gallery listing and individual member retrieval
- Raw photo retrieval
- Canonical profile HTML rendering with open-graph tags
- Candidate generation for zero-scoring search retrieval (CONTRACTS.md §2)
- Zero-trust scoring assertion compliance (assert_no_scoring_fields)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from contracts.schemas import SearchCandidate, SourceType, assert_no_scoring_fields
import services.gallery as gallery


@pytest.fixture(autouse=True)
def temp_gallery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate gallery storage to a temporary directory for tests."""
    test_gallery_dir = tmp_path / "gallery"
    test_index_file = test_gallery_dir / "gallery_index.json"
    monkeypatch.setattr(gallery, "GALLERY_DIR", test_gallery_dir)
    monkeypatch.setattr(gallery, "GALLERY_INDEX_FILE", test_index_file)
    gallery._ensure_gallery_dirs()


def _dummy_portrait() -> bytes:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[20:80, 20:80] = (120, 180, 240)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def test_enroll_member_creates_record_and_file() -> None:
    portrait = _dummy_portrait()
    member = gallery.enroll_member(
        full_name="Alex Rivera",
        role="Student Researcher",
        organization="HH Goa Cyber Academy",
        bio="Focusing on zero-trust identity proofs.",
        image_bytes=portrait,
        base_url="http://localhost:8000",
        metadata={"department": "CS", "year": "2026"},
    )

    assert member.member_id is not None
    assert member.full_name == "Alex Rivera"
    assert member.role == "Student Researcher"
    assert member.organization == "HH Goa Cyber Academy"
    assert member.profile_url == f"http://localhost:8000/api/gallery/profiles/{member.member_id}"
    assert member.metadata.get("department") == "CS"

    # Verify retrieval
    fetched = gallery.get_member(member.member_id)
    assert fetched is not None
    assert fetched.full_name == "Alex Rivera"

    # Verify image bytes
    img_bytes = gallery.get_member_image_bytes(member.member_id)
    assert img_bytes == portrait


def test_list_enrolled_members_returns_all() -> None:
    portrait = _dummy_portrait()
    gallery.enroll_member(
        full_name="Member One",
        role="Role 1",
        organization="Org 1",
        bio="Bio 1",
        image_bytes=portrait,
    )
    gallery.enroll_member(
        full_name="Member Two",
        role="Role 2",
        organization="Org 2",
        bio="Bio 2",
        image_bytes=portrait,
    )

    members = gallery.list_enrolled_members()
    assert len(members) == 2
    names = {m.full_name for m in members}
    assert "Member One" in names
    assert "Member Two" in names


def test_generate_profile_html() -> None:
    portrait = _dummy_portrait()
    member = gallery.enroll_member(
        full_name="Siddharth Rao",
        role="Cryptography Analyst",
        organization="HH Goa Labs",
        bio="Working on cryptographic hashing.",
        image_bytes=portrait,
        base_url="http://127.0.0.1:8000",
    )

    html = gallery.generate_profile_html(member, base_url="http://127.0.0.1:8000")
    assert "<!DOCTYPE html>" in html
    assert "Siddharth Rao" in html
    assert "Cryptography Analyst" in html
    assert 'meta property="og:image"' in html
    assert f"/api/gallery/images/{member.image_filename}" in html
    assert 'class="profile-photo"' in html


def test_search_gallery_candidates_zero_scoring_contract() -> None:
    portrait = _dummy_portrait()
    gallery.enroll_member(
        full_name="Jane Doe",
        role="Security Architect",
        organization="Campus Infosec",
        bio="Zero-trust security lead.",
        image_bytes=portrait,
    )

    candidates = gallery.search_gallery_candidates(base_url="http://127.0.0.1:8000")
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.source_type == SourceType.WEB
    assert "/api/gallery/profiles/" in cand.candidate_url
    assert "/api/gallery/images/" in cand.thumbnail_url

    # Strict zero-trust contract: ensure no similarity or scoring fields leaked into candidate
    assert_no_scoring_fields(cand.model_dump())
