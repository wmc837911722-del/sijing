from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from businessmap.core import CoreError, Store
from businessmap.source_evidence import SourceNavigator


def git(source, *args):
    return subprocess.run(["git", "-c", "user.name=Source Test", "-c", "user.email=source@example.invalid", *args],
                          cwd=source, check=True, capture_output=True, timeout=10).stdout.decode().strip()


@pytest.fixture
def source_store(tmp_path):
    source = tmp_path / "business-repository"
    source.mkdir()
    git(source, "init")
    files = {
        "src/StarCardService.java": "// imports\n" * 80 + "class StarCardService {\n  void deductStar() { // 保星卡扣星规则\n  }\n}\n",
        "src/Orders.py": "def order_total():\n    return 42\n",
        "src/StarPolicy.py": "# 保星卡需要人工审批\nvalue = 1\n",
        ".hidden.py": "保星卡\n",
        "src/credentials.py": "保星卡\n",
        "node_modules/dependency.py": "保星卡\n",
        "src/binary.py": "保星卡\x00private bytes\n",
        "src/settings.py": 'token = "inline-private-value"\npassword: \'private-password\'\npublic = "okay"\n',
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (source / "src" / "shortcut.py").symlink_to("Orders.py")
    git(source, "add", ".")
    git(source, "commit", "-m", "Tracked source fixture")
    commit = git(source, "rev-parse", "HEAD")
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    repo = {"id": "repo-fixture", "name": "Business repository", "url": str(source), "path": str(source),
            "remote": False, "branch": "", "commit": commit, "status": "ready", "vault_path": str(store.vault),
            "connection": {"auth_type": "none"}}
    store._write_repo(repo)
    try:
        yield source, store, repo
    finally:
        store.close()


def test_search_reaches_real_content_beyond_first_500_and_returns_matching_lines(source_store):
    source, store, repo = source_store
    for index in range(505):
        (source / f"a{index:04d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "More than the index cap")
    repo["commit"] = git(source, "rev-parse", "HEAD")
    store._write_repo(repo)
    navigator = SourceNavigator(store, repo["id"])
    result = navigator.search("扣星")
    assert result["total"] == 1
    assert result["eligible_files_count"] >= 509
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["searched_files_count"] == result["eligible_files_count"]
    match = result["matches"][0]
    assert match["path"] == "src/StarCardService.java"
    assert "82:   void deductStar() { // 保星卡扣星规则" in match["content"]
    assert match["line_start"] > 70
    assert result["untrusted"] is True
    assert match["matched_by"] == ["content"]
    assert list(store.vault.rglob("*.md")) == []


def test_search_literal_or_path_content_and_stable_pagination(source_store):
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    first = nav.search("保星卡 order_total", limit=1)
    assert first["total"] == 3
    assert first["next_offset"] == 1 and first["truncated"] is True
    second = nav.search("保星卡 order_total", limit=1, offset=1)
    assert first["matches"][0]["path"] < second["matches"][0]["path"]
    assert nav.search("StarCardService")["matches"][0]["matched_by"] == ["path", "content"]
    assert nav.search("business-repository")["total"] == 0
    assert nav.search(".*")["total"] == 0
    assert nav.search("no-such-term")["matches"] == []


def test_read_uses_immutable_blob_with_provenance_and_line_ranges(source_store):
    source, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    (source / "src/Orders.py").write_text("uncommitted malicious content\n", encoding="utf-8")
    result = nav.read("src/Orders.py", 2, 2, commit=repo["commit"])
    assert result["content"] == "2:     return 42"
    assert result["repository_id"] == repo["id"] and result["commit"] == repo["commit"]
    assert result["blob"] == git(source, "rev-parse", f"{repo['commit']}:src/Orders.py")
    assert result["revision"] == result["blob"]
    assert result["reference"] == f"source://repo-fixture/{repo['commit']}/src/Orders.py#L2-L2"
    assert result["total_lines"] == 2 and result["line_start"] == result["line_end"] == 2
    assert result["next_line"] is None and result["truncated"] is False and result["untrusted"] is True


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/outside.py", ".hidden.py", "src/credentials.py",
                                  "node_modules/dependency.py", "src/shortcut.py", "src/untracked.py"])
def test_read_rejects_paths_outside_safe_tracked_regular_blobs(source_store, path):
    source, store, repo = source_store
    (source / "src/untracked.py").write_text("PRIVATE UNTRACKED\n", encoding="utf-8")
    with pytest.raises(CoreError):
        SourceNavigator(store, repo["id"]).read(path)


def test_binary_is_neither_readable_nor_searchable_by_content_or_path(source_store):
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    with pytest.raises(CoreError):
        nav.read("src/binary.py")
    assert nav.search("binary")["total"] == 0
    assert {match["path"] for match in nav.search("保星卡")["matches"]} == {
        "src/StarCardService.java", "src/StarPolicy.py"}


def test_inline_secrets_redact_without_moving_source_line_numbers(source_store):
    _, store, repo = source_store
    result = SourceNavigator(store, repo["id"]).read("src/settings.py")
    assert '1: token = "[REDACTED]"' in result["content"]
    assert '2: password: "[REDACTED]"' in result["content"]
    assert '3: public = "okay"' in result["content"]
    assert "private-value" not in result["content"] and "private-password" not in result["content"]


def test_read_enforces_output_budget_and_provides_next_line(source_store):
    _, store, repo = source_store
    result = SourceNavigator(store, repo["id"]).read("src/StarCardService.java", max_chars=80)
    assert len(result["content"]) <= 80
    assert result["truncated"] is True
    assert result["next_line"] == result["line_end"] + 1


@pytest.mark.parametrize("kwargs", [{"line_start": 0}, {"line_start": True}, {"line_start": 2, "line_end": 1},
                                    {"line_start": 999}, {"max_chars": 0}, {"commit": "HEAD"}])
def test_read_rejects_invalid_ranges_and_arbitrary_revisions(source_store, kwargs):
    _, store, repo = source_store
    with pytest.raises(CoreError):
        SourceNavigator(store, repo["id"]).read("src/Orders.py", **kwargs)


@pytest.mark.parametrize("change", ["commit", "vault"])
def test_snapshot_rejects_changed_repository_or_selected_vault(source_store, tmp_path, change):
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    if change == "commit":
        store.update_repository(repo["id"], {"commit": "b" * 40})
    else:
        store.open_vault(str(tmp_path / "different-vault"))
    with pytest.raises(CoreError) as error:
        nav.read("src/Orders.py")
    assert error.value.status_code == 409


def test_git_output_limit_kills_excessive_output_without_shell(source_store, monkeypatch):
    import businessmap.source_evidence as module
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    original = subprocess.Popen
    def excessive(command, **kwargs):
        assert isinstance(command, list) and kwargs.get("shell", False) is False
        return original([sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", excessive)
    with pytest.raises(CoreError) as error:
        nav.read("src/Orders.py")
    assert error.value.status_code == 413


def test_git_timeout_terminates_child(source_store, monkeypatch):
    import businessmap.source_evidence as module
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    original = subprocess.Popen
    def slow(command, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(3)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", slow)
    monkeypatch.setattr(nav, "COMMAND_TIMEOUT", 0.05)
    with pytest.raises(CoreError) as error:
        nav.read("src/Orders.py")
    assert error.value.status_code == 408


def test_search_long_matching_line_still_exposes_the_actual_match(source_store):
    source, store, repo = source_store
    (source / "src/long.py").write_text("# introductory context\n" * 4 + "value = '" + "x" * 5000 + "扣星策略'\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Long source line")
    store.update_repository(repo["id"], {"commit": git(source, "rev-parse", "HEAD")})
    result = SourceNavigator(store, repo["id"]).search("扣星策略")
    assert result["total"] == 1
    match = result["matches"][0]
    assert "扣星策略" in match["content"]
    assert len(match["content"]) <= 400
    assert match["line_start"] <= 5 <= match["line_end"]


def test_source_line_numbers_count_newlines_without_splitting_unicode_separators(source_store):
    source, store, repo = source_store
    (source / "src/separators.py").write_text("first = 'a\u2028b'\nsecond = 2\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Unicode within one source line")
    store.update_repository(repo["id"], {"commit": git(source, "rev-parse", "HEAD")})
    result = SourceNavigator(store, repo["id"]).read("src/separators.py", 2, 2)
    assert result["content"] == "2: second = 2"
    assert result["total_lines"] == 2


def test_read_rechecks_snapshot_after_git_returns(source_store, monkeypatch):
    _, store, repo = source_store
    nav = SourceNavigator(store, repo["id"])
    original = nav._run_git
    def changed_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        store.update_repository(repo["id"], {"commit": "b" * 40})
        return result
    monkeypatch.setattr(nav, "_run_git", changed_after_read)
    with pytest.raises(CoreError) as error:
        nav.read("src/Orders.py")
    assert error.value.status_code == 409


def test_search_ignores_color_configuration_and_preserves_literal_pathspecs(source_store):
    source, store, repo = source_store
    (source / "src/special[1].py").write_text("# unique literal source\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Literal pathspec fixture")
    git(source, "config", "color.grep", "always")
    store.update_repository(repo["id"], {"commit": git(source, "rev-parse", "HEAD")})
    result = SourceNavigator(store, repo["id"]).search("unique")
    assert result["total"] == 1
    assert result["matches"][0]["path"] == "src/special[1].py"


def test_search_rejects_nul_before_starting_git(source_store):
    _, store, repo = source_store
    with pytest.raises(CoreError) as error:
        SourceNavigator(store, repo["id"]).search("bad\x00term")
    assert error.value.status_code == 400


def test_empty_tracked_file_has_a_valid_reference_without_invented_content(source_store):
    source, store, repo = source_store
    (source / "src/empty.py").write_text("", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Empty tracked source")
    store.update_repository(repo["id"], {"commit": git(source, "rev-parse", "HEAD")})
    nav = SourceNavigator(store, repo["id"])
    result = nav.read("src/empty.py")
    assert result["content"] == "" and result["total_lines"] == 0
    assert result["reference"].endswith("#L1-L1")
    assert result["next_line"] is None
    match = nav.search("empty")["matches"][0]
    assert match["path"] == result["path"] and match["line_start"] == match["line_end"] == 1
    assert "reference" not in match


def discovery_repository(source_store):
    source, store, repo = source_store
    for index in range(16):
        path = source / f"a-common/Noise{index:02d}.java"
        path.parent.mkdir(exist_ok=True)
        path.write_text("public class Noise { // " + "通用框架说明。" * 100 + "\n}\n", encoding="utf-8")
    files = {
        "b-api/McpAccessService.java": "public interface McpAccessService {\n  void settle();\n}\n",
        "z-service/McpAccessServiceImpl.java": "public class McpAccessServiceImpl implements McpAccessService {\n  String settle() { return \"保星卡免扣星\"; }\n}\n",
        "a-wrapper/LegacyMcpAccessServiceWrapper.java": "public class LegacyMcpAccessServiceWrapper {}\n",
        "a-wrapper/OtherMcpAccessServiceImpl.java": "public class OtherMcpAccessServiceImpl {}\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Real discovery payload and implementation ranking")
    repo["commit"] = git(source, "rev-parse", "HEAD")
    store._write_repo(repo)
    return SourceNavigator(store, repo["id"])


def test_discovery_is_compact_and_does_not_issue_read_receipts(source_store):
    nav = discovery_repository(source_store)
    result = nav.search("java")
    assert len(result["matches"]) == 8
    wire_bytes = len(json.dumps(result, ensure_ascii=False).encode())
    print(f"8-result discovery page: {wire_bytes} UTF-8 bytes")
    assert wire_bytes <= 12_000
    for match in result["matches"]:
        assert len(match["content"]) <= 400
        assert {"path", "content", "line_start", "line_end", "matched_by"} <= match.keys()
        assert not {"reference", "blob", "revision", "repository_id", "commit"} & match.keys()
    read = nav.read("z-service/McpAccessServiceImpl.java", 1, 3)
    assert read["reference"].endswith("/z-service/McpAccessServiceImpl.java#L1-L3")
    assert read["blob"] == read["revision"] and "保星卡免扣星" in read["content"]


def test_discovery_prioritizes_specific_implementation_paths_over_java_and_suffix_noise(source_store):
    nav = discovery_repository(source_store)
    result = nav.search("McpAccessService java")
    paths = [item["path"] for item in result["matches"]]
    assert paths[:2] == ["z-service/McpAccessServiceImpl.java", "b-api/McpAccessService.java"]
    assert nav.search("McpAccessServiceImpl")["matches"][0]["path"] == "z-service/McpAccessServiceImpl.java"
    assert "class McpAccessServiceImpl" in result["matches"][0]["content"]


def test_private_discovery_remains_compact_and_business_exploration_never_reads_source(source_store, monkeypatch):
    import keyring
    from businessmap.business_content import render_public_note
    from businessmap.tools_api import ToolsService
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    navigator = discovery_repository(source_store)
    _, store, repo = source_store
    pages = [navigator.search("java"), navigator.search("public")]
    assert all(len(page["matches"]) == 8 for page in pages)
    assert all(len(json.dumps(page, ensure_ascii=False).encode()) <= 12000 for page in pages)
    discovered = navigator.search("McpAccessService")
    assert discovered["matches"][0]["path"] == "z-service/McpAccessServiceImpl.java"
    receipt = navigator.read(discovered["matches"][0]["path"], 1, 3)
    assert "保星卡免扣星" in receipt["content"] and receipt["reference"].startswith("source://")
    tools = ToolsService(store)
    provider = tools.providers.save({"name": "Fixture", "kind": "openai-compatible",
                                     "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    tools.bind_model(repo["id"], provider["id"])
    path = "01-Wiki/赛事/规则/星数保护.md"
    note = store.save_note(path, render_public_note("星数保护", "# 星数保护\n保护条件成立时，本次不扣星。"))
    store.config_set(f"ai_analysis:{repo['id']}", {"vault_path": str(store.vault), "notes": {"rules": {
        "path": path, "revision": note["revision"], "commit": repo["commit"],
        "source_paths": [receipt["path"]], "source_evidence": [receipt], "stale": False, "conflict": False}}})
    steps = [("knowledge_read", {"path": path}),
             ("finish_answer", {"answer": "保护条件成立时，本次不扣星。", "kind": "answer", "citations": [path]})]
    requests = []
    def turn(system, messages, schemas, **kwargs):
        index = len(requests)
        assert index < len(steps)
        assert not {"repository_read", "repository_search"} & {tool["function"]["name"] for tool in schemas}
        requests.append(json.dumps(messages, ensure_ascii=False))
        name, arguments = steps[index]
        return {"text": "", "tool_calls": [{"id": str(index), "name": name, "arguments": arguments}],
                "assistant_message": {"role": "assistant", "content": ""},
                "usage": {"input_tokens": 100, "output_tokens": 10}}
    monkeypatch.setattr(tools.providers, "tool_turn", turn)
    result = tools.chat("保护条件下会扣星吗？", repository_id=repo["id"])
    assert len(requests) == 2 and result["completion"] == "complete"
    assert result["sources"] == [{"path": path, "title": "星数保护"}]
    public = json.dumps([result, requests], ensure_ascii=False)
    assert not any(value in public for value in ("source://", "McpAccessServiceImpl", repo["commit"], "source_path"))
