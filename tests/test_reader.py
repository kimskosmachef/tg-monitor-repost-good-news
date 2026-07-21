from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
from pathlib import Path

import pytest
import yaml

from tests.conftest import MINIMAL_CONFIG, MINIMAL_EXAMPLES, MINIMAL_TOPICS, write_examples_files
from tests.telethon_fakes import FakeClient, make_message
from tg_monitor.config_store import ConfigBundle, ConfigStore
from tg_monitor.models import Post
from tg_monitor.reader import TelegramReader, run_with_graceful_shutdown
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
    write_examples_files(tmp_path, MINIMAL_EXAMPLES)


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
        state=state_store.load(),
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
    assert post.message_ids == [10, 11]
    assert post.has_media is True
    assert post.origin == "live"
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
    assert sink.posts[0].message_ids == [1]
    assert sink.posts[0].origin == "live"


# --- отсечка по возрасту поста ----------------------------------------------


def test_old_post_is_not_sent_to_sink_but_marked_processed(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    # max_post_age_min=120 в MINIMAL_CONFIG, FIXED_NOW=15:00 → отсечка 13:00.
    old_message = make_message(
        7, date=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC), text="старьё"
    )

    asyncio.run(reader._emit_batch("src_a", [old_message], origin="catchup"))

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

    asyncio.run(reader._emit_batch("src_a", [fresh_message], origin="catchup"))

    assert len(sink.posts) == 1
    assert sink.posts[0].text == "свежак"


# --- время внутри системы — UTC (§3) -----------------------------------------


def test_post_date_is_stored_in_utc_regardless_of_logging_timezone(tmp_path: Path) -> None:
    # MINIMAL_CONFIG не задаёт logging.timezone явно — берётся дефолт
    # LoggingConfig, Europe/Riga (не UTC), чтобы разница была видна.
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, sink = _make_reader(tmp_path, client, sources)
    message = make_message(1, date=dt.datetime(2026, 7, 20, 14, 30, tzinfo=dt.UTC), text="x")

    asyncio.run(reader._emit_batch("src_a", [message], origin="catchup"))

    post_date = sink.posts[0].date
    assert post_date == dt.datetime(2026, 7, 20, 14, 30, tzinfo=dt.UTC)
    assert post_date.utcoffset() == dt.timedelta(0)


# --- обновление last_message_id ---------------------------------------------


def test_last_message_id_persisted_to_disk_after_emit(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, _sink = _make_reader(tmp_path, client, sources)
    message = make_message(3, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="x")

    asyncio.run(reader._emit_batch("src_a", [message], origin="catchup"))

    assert state_store.load().last_message_id == {"src_a": 3}


def test_last_message_id_does_not_go_backwards(tmp_path: Path) -> None:
    client = FakeClient()
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, _sink = _make_reader(tmp_path, client, sources)
    newer = make_message(9, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="9")
    older = make_message(5, date=dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC), text="5")

    async def scenario() -> None:
        await reader._emit_batch("src_a", [newer], origin="catchup")
        await reader._emit_batch("src_a", [older], origin="catchup")

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
    # last_message_id уже известен (не первый добор) — сид «с текущего
    # момента» (§8) в этом сценарии не участвует, проверяется отдельно.
    state_store.save(state_store.load().__class__(last_message_id={"src_a": 0}))
    reader._state = state_store.load()

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert [p.message_id for p in sink.posts] == [1, 2, 4]
    assert sink.posts[1].grouped_id == 42
    assert sink.posts[1].text == "два"
    assert sink.posts[1].message_ids == [2, 3]
    assert all(p.origin == "catchup" for p in sink.posts)
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


# --- §8: пустое состояние — старт «с текущего момента» -----------------------


def test_first_catchup_with_no_prior_state_seeds_current_head_without_backlog(
    tmp_path: Path,
) -> None:
    # §8: при отсутствии last_message_id (первый запуск, чистый state.json)
    # добор не поднимает весь архив канала — фиксируется только id последнего
    # существующего сообщения, старые посты в sink не уходят.
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [
        make_message(1, date=base, text="старый архив"),
        make_message(2, date=base, text="ещё старее"),
        make_message(3, date=base, text="последний перед стартом"),
    ]

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    assert state_store.load().last_message_id == {"src_a": 3}


def test_second_catchup_after_seed_processes_only_new_messages(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [
        make_message(1, date=base, text="старый архив"),
        make_message(2, date=base, text="последний перед стартом"),
    ]

    asyncio.run(reader._catchup_source("src_a", "entity_a"))
    assert sink.posts == []

    client.history["src_a"].append(make_message(3, date=base, text="новый после старта"))
    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert [p.message_id for p in sink.posts] == [3]
    assert state_store.load().last_message_id == {"src_a": 3}


def test_first_catchup_on_empty_channel_seeds_zero(tmp_path: Path) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    assert state_store.load().last_message_id == {"src_a": 0}


def test_flood_wait_while_seeding_current_position_is_retried(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    client.history["src_a"] = [make_message(5, date=base, text="текущий")]
    client.iter_error["src_a"] = errors.FloodWaitError(request=None, capture=5)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    assert state_store.load().last_message_id == {"src_a": 5}


def test_source_becoming_unavailable_while_seeding_is_marked_and_skipped(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    client.iter_error["src_a"] = errors.ChannelPrivateError(request=None)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    assert "src_a" not in state_store.load().last_message_id
    updated = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert updated[0]["status"] == "unavailable"


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


def test_reload_after_marking_unavailable_does_not_reattempt_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # §4 CLAUDE.md-правки: запись status: unavailable меняет mtime sources.yaml
    # и вызывает перезагрузку ConfigStore — убедиться, что это не превращается
    # в цикл повторных попыток резолва/повторной пометки того же источника.
    client = FakeClient()
    client.unresolvable.add("@gone")
    sources = [_source("src_gone", "@gone")]
    reader, config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)

    async def scenario() -> ConfigBundle:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)
        # Второй цикл, как это делает _catchup_loop: перечитывает конфиг
        # (mtime sources.yaml уже сменился после записи unavailable) и
        # пробует подписаться заново на все источники.
        bundle_after_reload = config_store.get()
        await reader._subscribe_active_sources(bundle_after_reload.sources)
        return bundle_after_reload

    with caplog.at_level(logging.WARNING, logger="tg_monitor.reader"):
        bundle_after_reload = asyncio.run(scenario())

    assert bundle_after_reload.sources[0].status == "unavailable"
    assert "src_gone" not in reader._entities
    mark_count = sum(
        1 for record in caplog.records if "помечен status: unavailable" in record.getMessage()
    )
    assert mark_count == 1


def test_source_becoming_unavailable_mid_catchup_is_marked_and_skipped(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    state_store.save(state_store.load().__class__(last_message_id={"src_a": 0}))
    reader._state = state_store.load()
    client.iter_error["src_a"] = errors.ChannelPrivateError(request=None)

    asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    updated = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert updated[0]["status"] == "unavailable"


def test_unexpected_value_error_during_catchup_does_not_mark_source_unavailable(
    tmp_path: Path,
) -> None:
    # §9 CLAUDE.md-правки: ValueError ловится только при резолве сущности
    # (get_entity). Случайная ValueError из iter_messages не должна навсегда
    # помечать живой канал unavailable — она просто всплывает наверх.
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    state_store.save(state_store.load().__class__(last_message_id={"src_a": 0}))
    reader._state = state_store.load()
    client.iter_error["src_a"] = ValueError("что-то не так, но канал тут ни при чём")

    with pytest.raises(ValueError, match="что-то не так"):
        asyncio.run(reader._catchup_source("src_a", "entity_a"))

    assert sink.posts == []
    updated = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))
    assert updated[0]["status"] == "active"


# --- FloodWait: ждать и повторять --------------------------------------------


def test_flood_wait_during_catchup_is_retried(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, state_store, sink = _make_reader(tmp_path, client, sources)
    state_store.save(state_store.load().__class__(last_message_id={"src_a": 0}))
    reader._state = state_store.load()
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


# --- штатная остановка: сигналы не должны ронять процесс трассировкой ------


def test_run_cancels_pending_background_tasks_on_shutdown(tmp_path: Path) -> None:
    client = FakeClient()
    client.block_until_disconnected = True
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    _write_config_set(tmp_path, sources)
    config_store = ConfigStore(tmp_path / "config.yaml")
    state_store = StateStore(tmp_path / "state.json")
    sink = RecordingSink()
    reader = TelegramReader(
        client=client,
        config_store=config_store,
        state_store=state_store,
        state=state_store.load(),
        sink=sink,
    )

    async def scenario() -> asyncio.Task[None]:
        run_task = asyncio.create_task(reader.run())
        await asyncio.sleep(0.05)  # дать run() дойти до run_until_disconnected

        base = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
        message = make_message(1, date=base, text="альбом", grouped_id=555)
        await client.fire_new_message("src_a", message)
        await asyncio.sleep(0.05)  # флаш-таск создан и ждёт media_group_flush_delay_sec
        assert reader._live_flush_tasks

        await reader.request_shutdown()
        await asyncio.wait_for(run_task, timeout=2)
        return run_task

    run_task = asyncio.run(scenario())

    assert run_task.done()
    assert run_task.exception() is None
    assert all(task.done() for task in reader._live_flush_tasks.values())


def test_run_with_graceful_shutdown_on_sigterm_logs_and_returns_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    client.block_until_disconnected = True
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    _write_config_set(tmp_path, sources)
    config_store = ConfigStore(tmp_path / "config.yaml")
    state_store = StateStore(tmp_path / "state.json")
    reader = TelegramReader(
        client=client,
        config_store=config_store,
        state_store=state_store,
        state=state_store.load(),
        sink=RecordingSink(),
    )

    async def scenario() -> None:
        run_task = asyncio.create_task(run_with_graceful_shutdown(reader))
        await asyncio.sleep(0.05)  # дать обработчикам сигналов установиться
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(run_task, timeout=2)

    with caplog.at_level(logging.INFO, logger="tg_monitor.reader"):
        asyncio.run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert any("получен сигнал SIGTERM" in m for m in messages)
    assert any("остановлено штатно" in m for m in messages)


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


# --- фоновые задачи не должны умирать молча (§9 CLAUDE.md-правки) ------------


def test_catchup_loop_logs_unexpected_exception_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, _config_store, _state_store, _sink = _make_reader(tmp_path, client, sources)

    calls = 0

    async def flaky_catchup_source(source_id: str, entity: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("сбой без причины")
        raise asyncio.CancelledError

    reader._catchup_source = flaky_catchup_source  # type: ignore[method-assign]

    with (
        caplog.at_level(logging.ERROR, logger="tg_monitor.reader"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(reader._catchup_loop())

    # первый вызов упал с RuntimeError и был пойман циклом (иначе второго
    # вызова, поднимающего CancelledError, просто не случилось бы) — цикл
    # не умер молча после первой ошибки.
    assert calls == 2
    assert any(
        "необработанная ошибка в цикле добора истории" in record.getMessage()
        for record in caplog.records
    )


def test_flush_live_group_logs_unexpected_exception_and_does_not_advance_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, config_store, state_store, sink = _make_reader(tmp_path, client, sources)

    async def failing_process_batch(source_id: str, messages: list[object]) -> None:
        raise RuntimeError("сбой сборки альбома")

    reader._process_batch = failing_process_batch  # type: ignore[method-assign]

    date = dt.datetime(2026, 7, 20, 14, 55, tzinfo=dt.UTC)
    message = make_message(10, date=date, text="подпись", grouped_id=555, has_media=True)

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)
        await reader._handle_incoming("src_a", message)
        task = reader._live_flush_tasks[("src_a", 555)]
        await task

    with caplog.at_level(logging.ERROR, logger="tg_monitor.reader"):
        asyncio.run(scenario())

    # пост не отправлен и last_message_id не продвинут — следующий добор
    # истории подхватит то же сообщение повторно, молчаливой потери нет.
    assert sink.posts == []
    assert "src_a" not in state_store.load().last_message_id
    assert any(
        "необработанная ошибка при обработке медиагруппы" in record.getMessage()
        for record in caplog.records
    )


def test_single_live_message_logs_unexpected_exception_and_does_not_advance_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    _link_entity(client, "@a", "entity_a", "src_a")
    sources = [_source("src_a", "@a")]
    reader, config_store, state_store, sink = _make_reader(tmp_path, client, sources)

    async def failing_process_batch(source_id: str, messages: list[object]) -> None:
        raise RuntimeError("сбой обработки сообщения")

    reader._process_batch = failing_process_batch  # type: ignore[method-assign]

    date = dt.datetime(2026, 7, 20, 14, 55, tzinfo=dt.UTC)
    message = make_message(11, date=date, text="одиночный пост")

    async def scenario() -> None:
        bundle = config_store.get()
        await reader._subscribe_active_sources(bundle.sources)
        await reader._handle_incoming("src_a", message)

    with caplog.at_level(logging.ERROR, logger="tg_monitor.reader"):
        asyncio.run(scenario())

    # пост не отправлен и last_message_id не продвинут — следующий добор
    # истории подхватит то же сообщение повторно, молчаливой потери нет.
    assert sink.posts == []
    assert "src_a" not in state_store.load().last_message_id
    assert any(
        "необработанная ошибка при обработке сообщения" in record.getMessage()
        for record in caplog.records
    )
