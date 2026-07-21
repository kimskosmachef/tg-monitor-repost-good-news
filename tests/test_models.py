from __future__ import annotations

import datetime as dt
import logging

import pytest
from pydantic import ValidationError

from tg_monitor.models import Facet, Post, Source, Topic


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
        "facets": [{"id": "facet_a", "examples_file": "facet_a.txt"}],
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


def test_topic_facet_requires_examples_file() -> None:
    # examples (§4.2) больше не часть схемы topics.yaml — только путь к
    # файлу; число примеров и предупреждение о нём теперь на стороне
    # config_loader.load_topics(), см. tests/test_config_loader.py.
    with pytest.raises(ValidationError):
        Topic.model_validate(_topic(facets=[{"id": "facet_a"}]))


def test_topic_requires_at_least_one_facet() -> None:
    with pytest.raises(ValidationError):
        Topic.model_validate(_topic(facets=[]))


def test_topic_facet_rejects_inline_examples_even_with_examples_file_present() -> None:
    # Старый формат (инлайн-список) — не молчаливый игнор и не переопределяется
    # examples_file, а ошибка конфига (StrictModel: лишнее — ошибка). Файл
    # examples_file присутствует и валиден сам по себе — падает именно из-за
    # инлайн "examples", а не из-за отсутствующего examples_file.
    with pytest.raises(ValidationError):
        Topic.model_validate(
            _topic(
                facets=[
                    {
                        "id": "facet_a",
                        "examples_file": "facet_a.txt",
                        "examples": ["один пример"],
                    }
                ]
            )
        )


def test_topic_rejects_inline_negatives_even_with_negatives_file_present() -> None:
    with pytest.raises(ValidationError):
        Topic.model_validate({**_topic(), "negatives_file": "neg.txt", "negatives": ["бой"]})


def test_facet_examples_loaded_field_not_required_and_excluded_from_dump() -> None:
    # `examples` заполняется config_loader'ом после чтения файла (§4.2) —
    # присвоением атрибута, а не через конструктор/model_validate, поэтому
    # по умолчанию пуст и не сериализуется.
    facet = Facet.model_validate({"id": "facet_a", "examples_file": "facet_a.txt"})
    assert facet.examples == []
    assert "examples" not in facet.model_dump()


def test_post_defaults() -> None:
    post = Post(
        message_id=1, source_id="src_a", date=dt.datetime(2026, 7, 20, 12, 0), origin="live"
    )
    assert post.text is None
    assert post.is_repost is False
    assert post.has_media is False
    assert post.forward_forbidden is False
    assert post.message_ids == []
