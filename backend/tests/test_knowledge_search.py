from __future__ import annotations

import pytest

from businessmap.core import Store
from businessmap.knowledge import KnowledgeNavigator


@pytest.fixture
def indexed_store(tmp_path):
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    folder = store.vault / "代码索引" / "arena"
    folder.mkdir(parents=True)
    for number in range(500):
        source = f"arena-mcp/src/main/java/example/Capability{number:03d}.java"
        (folder / f"{number:03d}.md").write_text(
            f"---\ntitle: {source}\ntype: code\nrepository_id: arena\n"
            f"source_path: {source}\nsource_commit: {'a' * 40}\n---\n# {source}\n\n"
            "> 自动生成的代码文件索引，**不是 AI 业务分析结论**。\n\n"
            "- 仓库：天梯赛事 · 后端\n"
            f"- 源文件：`{source}`\n- 提交：`{'a' * 40}`\n- 文件大小：100 字节\n\n"
            "请在仓库面板主动发起 AI 分析，或人工补充业务含义。\n",
            encoding="utf-8",
        )
    yield store
    store.close()


def test_business_search_does_not_treat_shared_repository_name_as_evidence(indexed_store):
    result = KnowledgeNavigator(indexed_store).call(
        "knowledge_search", {"query": "天梯赛 扣星 保星卡"}
    )

    assert result["notes"] == []


def test_distinct_business_terms_outrank_common_identifiers_in_code_paths(indexed_store):
    indexed_store.save_note("业务/访问限制.md", "# 访问限制\nMCP 当前权限组包括数据库只读查询；空权限组和未知权限组均拒绝。\n")

    result = KnowledgeNavigator(indexed_store).search("MCP 权限组")

    assert result["notes"][0]["path"] == "业务/访问限制.md"
    assert "空权限组" in result["notes"][0]["excerpt"]


@pytest.mark.parametrize("query", ["Capability042", "Capability042.java", "arena-mcp/src/main/java/example/Capability042.java"])
def test_code_identifiers_never_reveal_source_files_in_business_search(indexed_store, query):
    result = KnowledgeNavigator(indexed_store).call("knowledge_search", {"query": query})

    assert result["notes"] == []
    assert result["total"] == 0


def test_search_reports_accessible_total_and_exposes_stable_pages(indexed_store):
    business = indexed_store.vault / "业务知识"
    business.mkdir()
    for number in range(500):
        (business / f"{number:03d}.md").write_text(f"# 业务说明 {number}\n共同业务条件：需要审批。\n")
    nav = KnowledgeNavigator(indexed_store, allowed=lambda note: int(note["path"].split("/")[-1][:-3]) % 2 == 0)

    first = nav.call("knowledge_search", {"query": "共同业务条件"})
    assert first["total"] == 250
    assert first["offset"] == 0 and first["next_offset"] == 8 and first["truncated"]
    second = nav.call("knowledge_search", {"query": "共同业务条件", "offset": first["next_offset"]})
    assert second["total"] == 250
    assert {n["path"] for n in first["notes"]}.isdisjoint(n["path"] for n in second["notes"])
    last = nav.call("knowledge_search", {"query": "共同业务条件", "offset": 248})
    assert len(last["notes"]) == 2 and last["next_offset"] is None and not last["truncated"]


def test_excerpt_centers_distinct_business_evidence_instead_of_shared_context(indexed_store):
    # Source indexes no longer enter the business search corpus. Establish the
    # genuinely common term using public business documents instead.
    for number in range(30):
        (indexed_store.vault / f"背景{number}.md").write_text("# 业务背景\nMCP 通用业务说明。\n")
    indexed_store.save_note("业务/访问限制.md", "# MCP 概览\nMCP 通用说明。" + "背景。" * 1000
                            + "\n权限组拒绝空值和未知代码。\n")

    result = KnowledgeNavigator(indexed_store).search("MCP 权限组")

    assert result["notes"][0]["path"] == "业务/访问限制.md"
    assert "权限组拒绝空值和未知代码" in result["notes"][0]["excerpt"]
