from __future__ import annotations

import datetime as dt
import logging

import pytest
from pydantic import ValidationError

from tg_monitor.models import FACET_MIN_EXAMPLES, Post, Source, Topic


def _source(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "src_a",
        "ref": "@channel_a",
        "added": dt.date(2026, 7, 20),
    }
    base.update(overrides)
    return base


def _topic(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "topic_one",
        "target": "@target_channel_one",
        "facets": [{"id": "facet_a", "examples": ["один пример"]}],
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


def test_source_boost_above_max_is_accepted_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="tg_monitor.models"):
        source = Source.model_validate(_source(boost=0.06))

    assert source.boost == 0.06
    assert any("boost" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("boost", [0.01, -0.03])
def test_source_boost_below_max_is_accepted_without_warning(
    boost: float, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tg_monitor.models"):
        source = Source.model_validate(_source(boost=boost))

    assert source.boost == boost
    assert caplog.records == []


def test_source_defaults() -> None:
    source = Source.model_validate(_source())
    assert source.status == "active"
    assert source.tags == []
    assert source.boost == 0.0
    assert source.note == ""


def test_topic_facet_below_recommended_examples_is_accepted_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="tg_monitor.models"):
        topic = Topic.model_validate(
            _topic(facets=[{"id": "facet_a", "examples": ["один", "два"]}])
        )

    assert len(topic.facets[0].examples) == 2
    assert any(
        "topic_one" in record.getMessage() and "facet_a" in record.getMessage()
        for record in caplog.records
    )


def test_topic_facet_at_recommended_examples_is_accepted_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    examples = [f"пример {i}" for i in range(FACET_MIN_EXAMPLES)]
    with caplog.at_level(logging.WARNING, logger="tg_monitor.models"):
        topic = Topic.model_validate(_topic(facets=[{"id": "facet_a", "examples": examples}]))

    assert len(topic.facets[0].examples) == FACET_MIN_EXAMPLES
    assert caplog.records == []


def test_topic_facet_empty_examples_rejected() -> None:
    with pytest.raises(ValidationError):
        Topic.model_validate(_topic(facets=[{"id": "facet_a", "examples": []}]))


def test_post_defaults() -> None:
    post = Post(message_id=1, source_id="src_a", date=dt.datetime(2026, 7, 20, 12, 0))
    assert post.text is None
    assert post.is_repost is False
    assert post.has_media is False
    assert post.forward_forbidden is False
    assert post.message_ids == []
