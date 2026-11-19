"""Codex bookkeeping must not discard an already returned, paid response."""
from contextlib import closing, contextmanager
import json
import logging
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from businessmap import codex_provider
from businessmap.core import Store
from businessmap.providers import ProviderService, ToolError


@pytest.fixture
def codex_service(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("fixture disabled")))
    monkeypatch.setattr(codex_provider, "status", lambda: {"available": True, "logged_in": True})
    store = Store(tmp_path / "data", refresh_index_on_init=False)
    provider = {"id": "fixture-codex", "name": "Fixture Codex", "kind": "codex-local",
                "base_url": "codex://local", "model": "fixture-model"}
    store.config_set("providers", [provider])
    store.config_set("active_provider_id", provider["id"])
    state = SimpleNamespace(store=store, service=ProviderService(store), provider=provider,
                            entered=0, dispatched=0, returned=0, events=[],
                            response={"text": '{"operations":[]}',
                                      "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2},
                                      "output_limited": False,
                                      "runtime": {"kind": "codex-local", "version": "fixture", "model": "fixture-model"}})
    real_db = store._db

    @contextmanager
    def bounded_db():
        # Keep Store's connection, WAL PRAGMA and commit/rollback behavior real.
        with real_db() as db:
            db.execute("PRAGMA busy_timeout=50")
            db.set_trace_callback(lambda statement: state.events.append("usage_begin_immediate")
                                  if statement == "BEGIN IMMEDIATE" else None)
            yield db

    def adapter(messages, model, *, max_output, before_send, timeout=None, output_schema=None):
        state.entered += 1
        assert messages == [{"role": "system", "content": "fixture system"},
                            {"role": "user", "content": "fixture user"}]
        assert model == "fixture-model" and max_output == 2200 and timeout is None
        before_send()
        state.dispatched += 1
        state.returned += 1
        state.events.append("adapter_valid_response_returned")
        return state.response

    monkeypatch.setattr(store, "_db", bounded_db)
    monkeypatch.setattr(codex_provider, "complete", adapter)
    yield state
    store.close()


def committed_usage(store):
    # A separate connection is the committed-state contractual test seam.
    with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT value FROM config WHERE key='provider_usage'").fetchall()
    assert len(rows) <= 1
    return json.loads(rows[0][0]) if rows else None


def usage_warnings(caplog):
    return [record for record in caplog.records if record.name == "businessmap.providers"]


def assert_safe_failure_warning(caplog):
    records = usage_warnings(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "provider_usage_persist_failed"
    standard_fields = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    fields = {key: value for key, value in record.__dict__.items() if key not in standard_fields}
    assert fields == {"event": "provider_usage_persist_failed", "provider_id": "fixture-codex",
                      "error_type": "OperationalError"}
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert not record.args
    return fields


def test_valid_codex_response_survives_usage_write_lock(codex_service, caplog, record_property):
    state = codex_service
    caplog.set_level(logging.WARNING, logger="businessmap.providers")
    assert committed_usage(state.store) is None
    barrier = sqlite3.connect(state.store.db_path, timeout=.05)
    try:
        barrier.execute("BEGIN IMMEDIATE")
        response = state.service.complete("fixture system", "fixture user")
        assert response == {"text": '{"operations":[]}', "provider": "Fixture Codex", "usage_persisted": False,
                            "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2},
                            "output_limited": False,
                            "runtime": {"kind": "codex-local", "version": "fixture", "model": "fixture-model"}}
        assert response["usage_persisted"] is False
        assert state.entered == state.dispatched == state.returned == 1
        assert state.events == ["adapter_valid_response_returned", "usage_begin_immediate"]
        assert committed_usage(state.store) is None
        warning = assert_safe_failure_warning(caplog)
    finally:
        barrier.rollback()
        barrier.close()
    assert committed_usage(state.store) is None
    record_property("integrity_observations", json.dumps({"adapter_completed": state.returned,
                    "usage_persisted": response["usage_persisted"], "committed_usage": None,
                    "events": state.events, "warning": warning}, sort_keys=True))


def test_unlocked_codex_usage_is_committed_once(codex_service, caplog, record_property):
    state = codex_service
    response = state.service.complete("fixture system", "fixture user")
    assert response == {"text": '{"operations":[]}', "provider": "Fixture Codex", "usage_persisted": True,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2},
                        "output_limited": False,
                        "runtime": {"kind": "codex-local", "version": "fixture", "model": "fixture-model"}}
    assert response["usage_persisted"] is True
    assert state.entered == state.dispatched == state.returned == 1
    assert state.events == ["adapter_valid_response_returned", "usage_begin_immediate"]
    assert committed_usage(state.store) == {"requests": 1, "input_tokens": 10,
                                           "output_tokens": 5, "cached_tokens": 2}
    assert not usage_warnings(caplog)
    record_property("integrity_observations", json.dumps({"adapter_completed": state.returned,
                    "usage_persisted": response["usage_persisted"],
                    "committed_usage": committed_usage(state.store), "warning_count": 0}, sort_keys=True))


def test_locked_codex_usage_keeps_existing_totals_without_backfill(codex_service, caplog):
    state = codex_service
    state.store.config_set("provider_usage", {"requests": 7, "input_tokens": 100,
                                             "output_tokens": 50, "cached_tokens": 20})
    barrier = sqlite3.connect(state.store.db_path, timeout=.05)
    try:
        barrier.execute("BEGIN IMMEDIATE")
        response = state.service.complete("fixture system", "fixture user")
    finally:
        barrier.rollback()
        barrier.close()
    assert response["usage"] is state.response["usage"]
    assert response["runtime"] is state.response["runtime"]
    assert response["usage_persisted"] is False
    assert committed_usage(state.store) == {"requests": 7, "input_tokens": 100,
                                           "output_tokens": 50, "cached_tokens": 20}
    assert state.entered == state.dispatched == state.returned == 1
    assert_safe_failure_warning(caplog)


def test_codex_preserves_unknown_usage_and_output_limit(codex_service, caplog):
    state = codex_service
    state.response["usage"] = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    state.response["output_limited"] = True
    response = state.service.complete("fixture system", "fixture user")
    assert response == {**state.response, "provider": "Fixture Codex", "usage_persisted": True}
    assert committed_usage(state.store) == {"requests": 1, "input_tokens": None,
                                           "output_tokens": None, "cached_tokens": None}
    assert not usage_warnings(caplog)


def test_codex_destination_preflight_refusal_never_calls_adapter(codex_service, caplog):
    state = codex_service
    with pytest.raises(ToolError, match="模型目标已变化"):
        state.service.complete("fixture system", "fixture user", expected_destination={"id": "other"})
    assert state.entered == state.dispatched == state.returned == 0
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


@pytest.mark.parametrize("error", [ToolError("fixture permission refusal", 403),
                                   sqlite3.OperationalError("fixture guard database failure")])
def test_codex_before_send_errors_are_not_accounting_failures(codex_service, caplog, error):
    state = codex_service

    def refuse():
        raise error

    with pytest.raises(type(error)) as caught:
        state.service.complete("fixture system", "fixture user", before_send=refuse)
    assert caught.value is error
    assert state.entered == 1 and state.dispatched == state.returned == 0
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


@pytest.mark.parametrize("error", [ToolError("fixture transport failure", 502),
                                   sqlite3.OperationalError("fixture adapter database failure"),
                                   KeyboardInterrupt("fixture adapter cancellation")])
def test_codex_adapter_errors_propagate_without_retry(codex_service, monkeypatch, caplog, error):
    state = codex_service
    attempts = []

    def fail(*args, before_send, **kwargs):
        before_send()
        attempts.append("dispatch")
        raise error

    monkeypatch.setattr(codex_provider, "complete", fail)
    with pytest.raises(type(error)) as caught:
        state.service.complete("fixture system", "fixture user")
    assert caught.value is error and attempts == ["dispatch"]
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


def test_codex_metadata_database_failure_is_not_swallowed(codex_service, monkeypatch, caplog):
    state = codex_service
    real_get = state.store.config_get
    error = sqlite3.OperationalError("fixture metadata read failure")

    def failing_get(key, default=None):
        if key == "providers":
            raise error
        return real_get(key, default)

    monkeypatch.setattr(state.store, "config_get", failing_get)
    with pytest.raises(sqlite3.OperationalError) as caught:
        state.service.complete("fixture system", "fixture user")
    assert caught.value is error
    assert state.entered == state.dispatched == state.returned == 0
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


def test_codex_malformed_adapter_result_remains_an_error(codex_service, caplog):
    state = codex_service
    del state.response["usage"]
    with pytest.raises(KeyError, match="usage"):
        state.service.complete("fixture system", "fixture user")
    assert state.entered == state.dispatched == state.returned == 1
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


@pytest.mark.parametrize("error", [RuntimeError("fixture accounting bug"),
                                   TypeError("fixture accounting programming error"),
                                   sqlite3.IntegrityError("fixture accounting constraint failure"),
                                   KeyboardInterrupt("fixture accounting cancellation")])
def test_codex_usage_non_operational_errors_propagate(codex_service, monkeypatch, caplog, error):
    state = codex_service
    writes = []

    def fail(key, updater, default=None):
        writes.append(key)
        raise error

    monkeypatch.setattr(state.store, "config_update", fail)
    with pytest.raises(type(error)) as caught:
        state.service.complete("fixture system", "fixture user")
    assert caught.value is error and writes == ["provider_usage"]
    assert state.entered == state.dispatched == state.returned == 1
    assert committed_usage(state.store) is None
    assert not usage_warnings(caplog)


def test_codex_usage_failure_log_excludes_sensitive_exception_message(codex_service, monkeypatch, caplog):
    state = codex_service
    writes = []

    def fail(key, updater, default=None):
        writes.append(key)
        raise sqlite3.OperationalError("SELECT secret_api_key FROM source_PRIVATE; token=fixture-secret; response=PRIVATE")

    monkeypatch.setattr(state.store, "config_update", fail)
    response = state.service.complete("fixture system", "fixture user")
    assert response == {**state.response, "provider": "Fixture Codex", "usage_persisted": False}
    assert writes == ["provider_usage"] and state.returned == 1
    assert committed_usage(state.store) is None
    assert_safe_failure_warning(caplog)
    assert not any(token in caplog.text for token in ("SELECT", "secret_api_key", "source_PRIVATE", "fixture-secret", "PRIVATE"))


@pytest.mark.parametrize("locked", [False, True])
def test_http_provider_result_and_usage_failure_behavior_are_unchanged(codex_service, monkeypatch, caplog, locked):
    state = codex_service
    state.store.config_set("providers", [{"id": "fixture-http", "name": "Fixture HTTP", "kind": "openai-compatible",
                                         "base_url": "https://model.example.invalid/v1", "model": "http-model"}])
    state.store.config_set("active_provider_id", "fixture-http")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "fixture HTTP answer"}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                       "prompt_tokens_details": {"cached_tokens": 2}}})

    real_client = httpx.Client
    monkeypatch.setattr("businessmap.providers.httpx.Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    barrier = sqlite3.connect(state.store.db_path, timeout=.05)
    try:
        if locked:
            barrier.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                state.service.complete("fixture system", "fixture user")
        else:
            response = state.service.complete("fixture system", "fixture user")
            assert response == {"text": "fixture HTTP answer", "provider": "Fixture HTTP", "output_limited": False,
                                "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2, "cache_miss_tokens": None}}
    finally:
        barrier.rollback()
        barrier.close()
    assert len(requests) == 1 and state.entered == 0
    assert str(requests[0].url) == "https://model.example.invalid/v1/chat/completions"
    assert committed_usage(state.store) == (None if locked else {"requests": 1, "input_tokens": 10,
                                                               "output_tokens": 5, "cached_tokens": 2})
    assert not usage_warnings(caplog)
