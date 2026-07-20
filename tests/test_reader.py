from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import yaml

from tests.conftest import MINIMAL_CONFIG, MINIMAL_TOPICS
from tests.telethon_fakes import FakeClient, make_message
from tg_monitor.config_store import ConfigStore
from tg_monitor.models import Post
from tg_monitor.reader import TelegramReader
from tg_monitor.state import StateStore

FIXED_NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC)


def _source(id_: str, ref: str, status: str = "active") -> dict[str, object]:
    return {
        "id": id_,
        "ref": ref,
        "status": status,
        "tags": [],
        "boost": 0.0,
        "added": "2026-07-20",
        "note": "",
    }


def _write_config_set(tmp_path: Path, sources: list[dict[str, object]]) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(MINIMAL_CONFIG), encoding="utf-8")
    (tmp_path / "topics.yaml").write_text(yaml.safe_dump(MINIMAL_TOPICS), encoding="utf-8")
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")


class RecordingSink:
    def __init__(self) -> None:
        self.posts: list[Post] = []

    async def handle(self, post: Post) -> None:
        self.posts.append(post)


async def _instant_sleep(_seconds: float) -> None:
    return None


def _make_reader(
    tmp_path: Path,
    client: FakeClient,
    sources: list[dict[str, object]],
    now: dt.datetime = FIXED_NOW,
) -> tuple[TelegramReader, ConfigStore, StateStore, RecordingSink]:
    _write_config_set(tmp_path, sources)
    config_store = ConfigStore(tmp_path / "config.yaml")
    state_store = StateStore(tmp_path / "state.json")
    sink = RecordingSink()
    reader = TelegramReader(
        client=client,
        config_store=config_store,
        state_store=state_store,
        sink=sink,
        now=lambda: now,
        sleeper=_instant_sleep,
    )
    return reader, config_store, state_store, sink


def _link_entity(client: FakeClient, ref: str, entity: str, source_id: str) -> None:
    client.entities[ref] = entity
    client.entity_to_source[entity] = source_id


# --- подписка только на активные источники --------------------------------


def test_only_active_sources_are_subscribed(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    _link_entity(client, "@b", "entity_b", "src_b")
    sources = [_source("src_a", "@a", "active"), _source("src_b", "@b", "paused")]
    reader, config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)

    asyncio.run(scenario())

    assert "src_a" in reader._entities
    assert "src_b" not in reader._entities
    assert "src_a" in client.handlers


# --- сборка медиагруппы -----------------------------------------------------


def test_live_media_group_is_collated_into_one_post(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, config_store, _state_store, sink = _make_reader(tmp_path, client, sources)

    date = dt.datetime(2026, 7, 20, 14, 55, tzinfo=dt.UTC)
    photo_no_caption = make_message(10, date=date, grouped_id=555, has_media=True)
    photo_with_caption = make_message(
        11, date=date, text="подпись альбома", grouped_id=555, has_media=True
    )

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)
        await reader._handle_incoming("src_a", photo_no_caption)
        await reader._handle_incoming("src_a", photo_with_caption)
        task = reader._live_flush_tasks[("src_a", 555)]
        await task

    asyncio.run(scenario())

    assert len(sink.posts) == 1
    post = sink.posts[0]
    assert post.message_id == 11
    assert post.text == "подпись альбома"
    assert post.grouped_id == 555
    assert post.has_media is True
    assert reader._state.last_message_id["src_a"] == 11


def test_live_single_message_without_group_is_emitted_immediately(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, config_store, _state_store, sink = _make_reader(tmp_path, client, sources)
    message = make_message(1, date=dt.datetime(2026, 7, 20, 14, 55, tzinfo=dt.UTC), text="привет")

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)
        await reader._handle_incoming("src_a", message)

    asyncio.run(scenario())

    assert len(sink.posts) == 1
    assert sink.posts[0].text == "привет"
    assert sink.posts[0].grouped_id is None


# --- отсечка по возрасту поста ----------------------------------------------


def test_old_post_is_not_sent_to_sink_but_marked_processed(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    # max_post_age_min=120 в MINIMAL_CONFIG, FIXED_NOW=15:00 → отсечка 13:00.
    old_message = make_message(
        7, date=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC), text="старьё"
    )

    asyncio.run(reader._emit_batch("src_a", [old_message]))

    assert sink.posts == []
    assert reader._state.last_message_id["src_a"] == 7
    assert state_store.load().last_message_id["src_a"] == 7


def test_fresh_post_within_age_limit_is_sent_to_sink(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, sink = _make_reader(tmp_path, client, sources)
    fresh_message = make_message(
        8, date=dt.datetime(2026, 7, 20, 14, 30, tzinfo=dt.UTC), text="свежак"
    )

    asyncio.run(reader._emit_batch("src_a", [fresh_message]))

    assert len(sink.posts) == 1
    assert sink.posts[0].text == "свежак"


# --- обновление last_message_id ---------------------------------------------


def test_last_message_id_persisted_to_disk_after_emit(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, _sink = _make_reader(tmp_path, client, sources)
    message = make_message(3, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="x")

    asyncio.run(reader._emit_batch("src_a", [message]))

    assert state_store.load().last_message_id == {"src_a": 3}


def test_last_message_id_does_not_go_backwards(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, _sink = _make_reader(tmp_path, client, sources)
    newer = make_message(9, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="9")
    older = make_message(5, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="5")

    async def scenario() -> None:
        await reader._emit_batch("src_a", [newer])
        await reader._emit_batch("src_a", [older])

    asyncio.run(scenario())

    assert state_store.load().last_message_id == {"src_a": 9}


# --- порядок добора истории --------------------------------------------------


def test_catchup_processes_history_in_chronological_order(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [
        make_message(1, date=base, text="один"),
        make_message(2, date=base, text="два", grouped_id=42, has_media=True),
        make_message(3, date=base, text=None, grouped_id=42, has_media=True),
        make_message(4, date=base, text="четыре"),
    ]

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert [p.message_id for p in sink.posts] == [1, 2, 4]
    assert sink.posts[1].grouped_id == 42
    assert sink.posts[1].text == "два"
    assert state_store.load().last_message_id == {"src_a": 4}


def test_catchup_resumes_from_last_message_id(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [
        make_message(1, date=base, text="один"),
        make_message(2, date=base, text="два"),
    ]
    state_store.save(state_store.load().__class__(last_message_id={"src_a": 1}))
    reader._state = state_store.load()

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert [p.message_id for p in sink.posts] == [2]


# --- поведение при недоступном источнике -------------------------------------


def test_unresolvable_source_is_marked_unavailable_and_skipped(tmp_path: Path) -> None:
    client = FakeClient()
    client.unresolvable.add("@gone")
    sources = [_source("src_gone", "@gone")]
    reader, config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)

    asyncio.run(scenario())

    assert "src_gone" not in reader._entities
    updated = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert updated[0]["status"] == "unavailable"


def test_source_becoming_unavailable_mid_catchup_is_marked_and_skipped(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, sink = _make_reader(tmp_path, client, sources)
    client.iter_error["src_a"] = errors.ChannelPrivateError(request=None)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    updated = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert updated[0]["status"] == "unavailable"


# --- FloodWait: ждать и повторять --------------------------------------------


def test_flood_wait_during_catchup_is_retried(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [make_message(1, date=base, text="после ожидания")]
    client.iter_error["src_a"] = errors.FloodWaitError(request=None, capture=5)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert [p.message_id for p in sink.posts] == [1]


def test_run_connects_subscribes_and_returns_after_disconnect(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)

    asyncio.run(reader.run())

    assert client.connected is True
    assert "src_a" in client.handlers


def test_flood_wait_during_entity_resolution_is_retried(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)
    client.get_entity_error["@a"] = errors.FloodWaitError(request=None, capture=3)

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)

    asyncio.run(scenario())

    assert reader._entities.get("src_a") == "entity_a"
