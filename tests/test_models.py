from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from tg_monitor.models import Post, Source


def _source(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "src_a",
        "ref": "@channel_a",
        "added": dt.date(2026, 7, 20),
    }
    base.update(overrides)
    return base


def test_source_boost_zero_is_valid() -> None:
    source = Source.model_validate(_source(boost=0.0))
    assert source.boost == 0.0


@pytest.mark.parametrize("boost", [0.02, 0.035, 0.05])
def test_source_boost_within_range_is_valid(boost: float) -> None:
    source = Source.model_validate(_source(boost=boost))
    assert source.boost == boost


@pytest.mark.parametrize("boost", [0.01, 0.06, -0.03])
def test_source_boost_out_of_range_is_rejected(boost: float) -> None:
    with pytest.raises(ValidationError):
        Source.model_validate(_source(boost=boost))


def test_source_defaults() -> None:
    source = Source.model_validate(_source())
    assert source.status == "active"
    assert source.tags == []
    assert source.boost == 0.0
    assert source.note == ""


def test_post_is_described_but_unpopulated_in_skeleton() -> None:
    post = Post(id=1, source_id="src_a", date=dt.datetime(2026, 7, 20, 12, 0))
    assert post.text is None
    assert post.is_repost is False
    assert post.forward_forbidden is False
