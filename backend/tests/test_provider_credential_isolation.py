"""Provider selection must not open another configured provider's credentials."""
from copy import deepcopy
import json

import httpx
import pytest

from businessmap import codex_provider
from businessmap.providers import ProviderService


class FakeStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.values = {"active_provider_id": "local", "providers": [
            {"id": "http-a", "name": "First HTTP", "kind": "openai-compatible",
             "base_url": "https://first.example.invalid/v1", "model": "first-model", "secret_ref": "first-secret"},
            {"id": "local", "name": "Local Codex", "kind": "codex-local", "base_url": "codex://local", "model": "default"},
            {"id": "http-b", "name": "Second HTTP", "kind": "openai-compatible",
             "base_url": "https://second.example.invalid/v1", "model": "second-model", "secret_ref": "second-secret"},
        ]}

    def config_get(self, key, default=None):
        return deepcopy(self.values.get(key, default))

    def config_set(self, key, value):
        self.values[key] = deepcopy(value)


class FakeSecrets:
    def __init__(self, _data_dir):
        self.reads = []
        self.values = {"first-secret": "fixture-first-key", "second-secret": ""}

    def get(self, key):
        self.reads.append(key)
        return self.values.get(key)

    def storage(self, key):
        return "session" if self.values.get(key) else "none"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr("businessmap.providers.Secrets", FakeSecrets)
    monkeypatch.setattr(codex_provider, "status", lambda: {
        "installed": True, "logged_in": True, "status": "ready", "version": "fixture", "detail": "fixture login"})
    def complete(messages, model, *, max_output, before_send, timeout):
        assert model == "default" and messages[-1]["role"] == "user"
        before_send()
        return {"text": "OK", "usage": {"input_tokens": 10, "output_tokens": 1, "cached_tokens": None},
                "output_limited": False, "runtime": {"kind": "codex-local", "version": "fixture", "model": model}}
    monkeypatch.setattr(codex_provider, "complete", complete)
    return ProviderService(FakeStore(tmp_path))


@pytest.mark.parametrize("provider_id,connection_test", [("local", False), (None, False), ("local", True)])
def test_codex_calls_do_not_read_unrelated_http_keys(service, provider_id, connection_test):
    result = service.complete("Reply with OK.", "Synthetic connection test.", provider_id=provider_id,
                              max_output=32, _connection_test=connection_test)
    assert result["text"] == "OK"
    assert service.secrets.reads == []


@pytest.mark.parametrize("operation", ["get", "active", "save", "convert_http", "activate"])
def test_codex_metadata_actions_do_not_read_http_keys(service, operation):
    if operation in {"save", "convert_http"}:
        provider = service.save({"id": "local" if operation == "save" else "http-a", "name": "Renamed Codex", "kind": "codex-local", "model": "default"})
    elif operation == "activate":
        service.activate("local")
        provider = service.get("local")
    else:
        provider = service.get("local" if operation == "get" else None)
    assert provider["has_key"] is False
    assert provider["secret_storage"] == "none"
    assert provider["codex_status"]["logged_in"] is True
    assert "secret_ref" not in provider
    assert service.secrets.reads == []


def test_missing_provider_does_not_open_any_credentials(service):
    assert service.get("missing") is None
    assert service.secrets.reads == []


@pytest.mark.parametrize("provider_id,has_key,storage", [("http-a", True, "session"), ("http-b", False, "none")])
def test_http_get_keeps_key_availability_and_reads_only_selected_provider(service, provider_id, has_key, storage):
    provider = service.get(provider_id)
    assert provider["has_key"] is has_key
    assert provider["secret_storage"] == storage
    assert "secret_ref" not in provider
    assert "fixture-first-key" not in json.dumps(provider)
    assert service.secrets.reads == ["first-secret" if provider_id == "http-a" else "second-secret"]


def test_http_completion_keeps_selected_credentials(service, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Fixture HTTP answer"}}], "usage": {}})
    client = httpx.Client
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    result = service.complete("Policy", "Synthetic request", provider_id="http-a")
    assert result["text"] == "Fixture HTTP answer"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://first.example.invalid/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer fixture-first-key"
    assert set(service.secrets.reads) == {"first-secret"}


def test_settings_list_preserves_all_http_key_availability(service):
    providers = {provider["id"]: provider for provider in service.list()}
    assert providers["http-a"]["has_key"] is True
    assert providers["http-b"]["has_key"] is False
    assert providers["local"]["has_key"] is False
    assert set(service.secrets.reads) == {"first-secret", "second-secret"}
    assert "fixture-first-key" not in json.dumps(providers)
