"""Tests for Search Extensions (DuckDuckGo scraping, Enrolled Gallery, Federated Search).

Ensures all search extensions strictly respect CONTRACTS.md §2 (extra="forbid",
assert_no_scoring_fields, pure retrieval without scoring state).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from contracts.schemas import (
    CanonicalStatus,
    SearchCandidate,
    SearchOutput,
    SourceType,
    assert_no_scoring_fields,
)
import services.search as search_mod


def test_search_enrolled_gallery_returns_search_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.gallery as gallery_mod

    def fake_gallery_candidates(*args: Any, **kwargs: Any):
        return [
            SearchCandidate(
                candidate_id="gal-1",
                candidate_url="http://127.0.0.1:8000/api/gallery/profiles/12345",
                source_type=SourceType.WEB,
                thumbnail_url="http://127.0.0.1:8000/api/gallery/images/12345.jpg",
            )
        ]

    monkeypatch.setattr(gallery_mod, "search_gallery_candidates", fake_gallery_candidates)

    out = search_mod.search_enrolled_gallery()
    assert isinstance(out, SearchOutput)
    assert out.status == CanonicalStatus.SEARCH_RESULTS_FOUND
    assert len(out.candidates) == 1
    assert out.candidates[0].candidate_id == "gal-1"
    assert_no_scoring_fields(out.model_dump())


def test_search_duckduckgo_parses_html_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_html = """
    <html>
      <body>
        <div class="result__body">
          <a class="result__url" href="https://campus.edu/directory/person1">Profile 1</a>
        </div>
        <div class="result__body">
          <a class="result__url" href="https://enterprise.org/team/person2">Profile 2</a>
        </div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = fake_html

    monkeypatch.setattr(search_mod.requests, "post", lambda *args, **kwargs: mock_resp)

    out = search_mod.search_duckduckgo(query="test search query")
    assert isinstance(out, SearchOutput)
    assert out.status == CanonicalStatus.SEARCH_RESULTS_FOUND
    assert len(out.candidates) == 2
    assert "https://campus.edu/directory/person1" in [c.candidate_url for c in out.candidates]
    # DDG is smoke-test only — found_via must stay None per v3.
    assert out.candidates[0].found_via is None
    assert_no_scoring_fields(out.model_dump())


def test_federated_search_aggregates_without_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.gallery as gallery_mod

    def fake_gallery_candidates(*args: Any, **kwargs: Any):
        return [
            SearchCandidate(
                candidate_id="gal-abc",
                candidate_url="http://127.0.0.1:8000/api/gallery/profiles/abc",
                source_type=SourceType.WEB,
                thumbnail_url="http://127.0.0.1:8000/api/gallery/images/abc.jpg",
            )
        ]

    monkeypatch.setattr(gallery_mod, "search_gallery_candidates", fake_gallery_candidates)
    monkeypatch.setattr(
        search_mod,
        "search",
        lambda *a, **kw: SearchOutput(candidates=[], status=CanonicalStatus.NO_SEARCH_RESULTS),
    )
    monkeypatch.setattr(
        search_mod,
        "search_duckduckgo",
        lambda *a, **kw: SearchOutput(candidates=[], status=CanonicalStatus.NO_SEARCH_RESULTS),
    )

    out = search_mod.federated_search(b"dummy_bytes")
    assert isinstance(out, SearchOutput)
    assert len(out.candidates) == 1
    assert out.candidates[0].candidate_id == "gal-abc"
    assert_no_scoring_fields(out.model_dump())
