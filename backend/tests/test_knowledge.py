from __future__ import annotations

import asyncio
import json

import pytest

from businessmap.core import CoreError, Store
from businessmap.knowledge import KnowledgeNavigator
from businessmap.mcp_server import create_server
from businessmap.tools_api import ToolsService


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    instance.open_vault(str(tmp_path / "empty-vault"))
    yield instance
    instance.close()


def test_search_discovers_late_evidence_then_outline_and_paginated_section_read(store):
    content = "# 退款说明\n\n## 前言\n" + "通用说明。" * 2000 + "\n\n## 特殊退款\n" + "退款由商户承担手续费。" * 700 + "\n\n## 其他\n结束。\n"
    store.save_note("业务/退款.md", content)
    nav = KnowledgeNavigator(store)
    found = nav.call("knowledge_search", {"query": "商户承担手续费"})
    note = found["notes"][0]
    assert "商户承担手续费" in note["excerpt"] and len(note["excerpt"]) <= 480
    assert "content" not in note
    outline = nav.call("knowledge_outline", {"path": note["path"]})
    section = next(item for item in outline["headings"] if item["heading"] == "特殊退款")
    assert section["start"] > 3500
    pages, offset = [], 0
    while offset is not None:
        page = nav.call("knowledge_read", {"path": note["path"], "heading": "特殊退款", "offset": offset,
                                           "limit": 1500, "revision": outline["revision"]})
        pages.append(page["content"])
        offset = page["next_offset"]
    assert len(pages) > 3
    assert "".join(pages) == content[section["start"]:section["end"]]
    assert "## 其他" not in "".join(pages)


def test_link_navigation_resolves_markdown_wikilinks_backlinks_and_ignores_code(store):
    store.save_note("业务/入口.md", "# 入口\n[[政策/退款#付款条件|退款规则]]\n[表](../数据/订单.md#字段)\n"
                    "`[[秘密]]`\n````md\n[[秘密]]\n```\n[[秘密]]\n````\n<!-- [[秘密]] -->\n"
                    "    [[秘密]]\n\\[[秘密]]\n[外部](https://example.com/秘密.md)\n")
    store.save_note("政策/退款.md", "# 退款\n## 付款条件\n说明。\n[返回](../业务/入口.md)\n")
    store.save_note("数据/订单.md", "# 订单\n## 字段\n金额。\n")
    store.save_note("秘密.md", "# 秘密\n")
    nav = KnowledgeNavigator(store)
    outgoing = nav.links("业务/入口.md", "outgoing")
    assert {(edge["target"], edge["anchor"], edge["kind"]) for edge in outgoing["edges"]} == {
        ("政策/退款.md", "付款条件", "wiki"), ("数据/订单.md", "字段", "markdown")}
    incoming = nav.links("业务/入口.md", "incoming")
    assert [(edge["source"], edge["target"]) for edge in incoming["edges"]] == [("政策/退款.md", "业务/入口.md")]
    assert all(item["revision"] for item in outgoing["nodes"])
    followed = nav.read("政策/退款.md#付款条件")
    assert followed["content"].startswith("## 付款条件")


def test_hidden_notes_never_leak_through_metadata_or_resolve_ambiguity(store):
    store.save_note("入口.md", "# 入口\n[[私有/账户]]\n[[说明]]\n")
    store.save_note("私有/账户.md", "# confidential account title\n[[入口]]\n")
    store.save_note("私有/说明.md", "# 说明\nsecret meaning\n")
    store.save_note("公共/说明.md", "# 说明\npublic meaning\n")
    nav = KnowledgeNavigator(store, allowed=lambda note: not note["path"].startswith("私有/"))
    links = nav.links("入口.md")
    assert links["edges"] == []  # A hidden duplicate cannot cause a guessed public destination.
    discovery = nav.search("说明")
    assert all(not item["path"].startswith("私有/") for item in discovery["notes"])
    payload = json.dumps([nav.list(), links, nav.graph()], ensure_ascii=False)
    assert "私有/" not in payload and "confidential" not in payload and "secret meaning" not in payload
    for tool in (nav.read, nav.outline, nav.links):
        with pytest.raises(CoreError) as hidden:
            tool("私有/账户.md")
        with pytest.raises(CoreError) as absent:
            tool("不存在.md")
        assert hidden.value.status_code == absent.value.status_code == 404
        assert str(hidden.value) == str(absent.value)
    # Visible note text remains the authoritative Markdown, including its own links.
    assert "[[私有/账户]]" in nav.read("入口.md")["content"]


def test_revision_guard_rejects_mixed_pages_and_external_rename_refreshes_backlinks(store):
    store.save_note("入口.md", "# 入口\n[[规则]]\n")
    saved = store.save_note("规则.md", "# 规则\n原来的条款。\n")
    nav = KnowledgeNavigator(store)
    first = nav.read("规则.md", limit=4)
    store.save_note("规则.md", "# 规则\n修改后的新条款。\n", saved["revision"])
    with pytest.raises(CoreError, match="笔记已变化") as changed:
        nav.read("规则.md", offset=first["next_offset"], revision=first["revision"])
    assert changed.value.status_code == 409
    (store.vault / "规则.md").rename(store.vault / "新规则.md")
    assert nav.links("入口.md")["edges"][0]["target"] == "新规则.md"
    with pytest.raises(CoreError):
        nav.read("规则.md")
    assert [note["path"] for note in nav.search("新条款")["notes"]] == ["新规则.md"]
    entrance = store.read_note("入口.md")
    store.save_note("入口.md", "# 入口\n[[新规则]]\n", entrance["revision"])
    assert nav.links("新规则.md", "incoming")["edges"][0]["source"] == "入口.md"


def test_listing_pages_and_scope_are_deterministic(store):
    for number in range(7):
        store.save_note(f"业务/{number}.md", f"# 条款 {number}\n正文。\n")
    store.save_note("其他.md", "# 其他\n")
    nav = KnowledgeNavigator(store, allowed=lambda note: note["path"] != "业务/3.md")
    first = nav.list("业务", limit=2)
    second = nav.list("业务", offset=first["next_offset"], limit=2)
    last = nav.list("业务", offset=second["next_offset"], limit=2)
    assert [note["path"] for page in [first, second, last] for note in page["notes"]] == [f"业务/{i}.md" for i in (0, 1, 2, 4, 5, 6)]
    assert first["total"] == 6 and last["next_offset"] is None and not last["truncated"]
    assert "正文" not in json.dumps([first, second, last], ensure_ascii=False)


def test_nested_headings_blocks_and_duplicate_anchor_detection(store):
    content = "---\ntitle: notes\n---\n# Overview\n\n## Payment rules\nPolicy paragraph\ncontinues. ^policy\n\n### Child\nextra\n\n## Else\nbye\n"
    store.save_note("notes.md", content)
    nav = KnowledgeNavigator(store)
    assert "### Child" in nav.read("notes.md", heading="payment-rules")["content"]
    assert "## Else" not in nav.read("notes.md", heading="payment-rules")["content"]
    block = nav.read("notes.md#^policy")
    assert block["content"] == "Policy paragraph\ncontinues. ^policy\n"
    assert not any(heading["heading"] == "title: notes" for heading in nav.outline("notes.md")["headings"])
    note = store.read_note("notes.md")
    store.save_note("notes.md", content + "\n## Else\nAnother\n", note["revision"])
    with pytest.raises(CoreError, match="不唯一"):
        nav.read("notes.md", heading="Else")


def test_bounded_outline_keeps_later_sections_directly_readable(store):
    store.save_note("large.md", "# Large\n" + "".join(f"## Section {index}\nPolicy {index}.\n" for index in range(300)))
    nav = KnowledgeNavigator(store)
    outline = nav.outline("large.md")
    assert len(outline["headings"]) == 100 and outline["total_headings"] == 301 and outline["truncated"]
    later = nav.outline("large.md", offset=outline["next_offset"], limit=200)
    last = nav.outline("large.md", offset=later["next_offset"])
    assert last["headings"][0]["heading"] == "Section 299" and last["next_offset"] is None
    assert nav.read("large.md", heading="Section 299", revision=outline["revision"])["content"] == "## Section 299\nPolicy 299.\n"


def test_reference_links_dot_names_and_encoded_relative_destinations(store):
    store.save_note("目录/entry.md", "# Entry\n[Policy][the rule]\n[Data][]\n[[规则.v2]]\n"
                    "\n[the rule]: ../%E8%A7%84%E5%88%99.v2.md#C%23\n[Data]: <../data notes.md>\n")
    store.save_note("规则.v2.md", "# Rules\n## C#\nActual rule.\n")
    store.save_note("data notes.md", "# Data\nActual data.\n")
    nav = KnowledgeNavigator(store)
    edges = nav.links("目录/entry.md", "outgoing")["edges"]
    assert {(edge["target"], edge["kind"]) for edge in edges} == {
        ("规则.v2.md", "wiki"), ("规则.v2.md", "markdown"), ("data notes.md", "markdown")}
    assert nav.read("规则.v2.md", heading="C#")["content"].startswith("## C#")


def test_large_link_neighborhood_remains_navigable_with_pages(store):
    store.save_note("hub.md", "# Hub\n" + "\n".join(f"[[rules/{index:02d}]]" for index in range(55)))
    (store.vault / "rules").mkdir()
    for index in range(55):
        (store.vault / "rules" / f"{index:02d}.md").write_text(f"# Rule {index}\n", encoding="utf-8")
    nav = KnowledgeNavigator(store)
    first = nav.links("hub.md", "outgoing", limit=50)
    last = nav.links("hub.md", "outgoing", limit=50, offset=first["next_offset"])
    assert first["total"] == 55 and len(first["edges"]) == 50
    assert len(last["edges"]) == 5 and last["next_offset"] is None and not last["truncated"]
    assert {edge["target"] for edge in first["edges"]}.isdisjoint(edge["target"] for edge in last["edges"])


@pytest.mark.parametrize("generated_key", [True, False])
def test_moved_business_notes_still_require_current_analysis(store, monkeypatch, generated_key):
    current = {"id": "repo", "commit": "a" * 40}
    monkeypatch.setattr(store, "get_repository", lambda _repo_id: dict(current))
    key_line = "generated_key: rules\n" if generated_key else ""
    note = store.save_note("整理/退款.md", "---\ntype: business\nrepository_id: repo\nsource_commit: " + "a" * 40
                           + "\nsource_paths: [refunds.py]\n" + key_line + "---\n# 退款\n当前规则。\n")
    entry = {"path": "业务地图/repo/rules.md", "revision": note["revision"], "commit": "a" * 40,
             "source_paths": ["refunds.py"], "stale": False, "conflict": False}
    store.config_set("ai_analysis:repo", {"notes": {"rules": entry}})
    service = ToolsService(store)
    assert service._eligible(note)
    current["commit"] = "b" * 40
    assert not service._eligible(note)
    assert KnowledgeNavigator(store, service._eligible).search("当前规则")["notes"] == []
    current["commit"] = "a" * 40
    store.config_set("ai_analysis:repo", {"notes": {"rules": {**entry, "conflict": True}}})
    assert not service._eligible(note)
    # Moving and filtering must never rewrite the user's Markdown.
    assert store.read_note(note["path"])["revision"] == note["revision"]


def test_legacy_moved_note_source_match_must_be_unambiguous_and_manual_notes_stay_available(store, monkeypatch):
    monkeypatch.setattr(store, "get_repository", lambda _: {"id": "repo", "commit": "a" * 40})
    note = store.save_note("整理/旧笔记.md", "---\ntype: business\nrepository_id: repo\nsource_commit: " + "a" * 40
                           + "\nsource_paths: [refunds.py]\n---\n# 人工补充过的生成笔记\n规则。\n")
    entry = {"revision": "previous-revision", "commit": "a" * 40, "source_paths": ["refunds.py"]}
    service = ToolsService(store)
    store.config_set("ai_analysis:repo", {"notes": {"rules": entry}})
    assert service._eligible(note)
    store.config_set("ai_analysis:repo", {"notes": {"rules": entry, "flows": dict(entry)}})
    assert not service._eligible(note)
    manual = store.save_note("整理/人工笔记.md", "---\ntype: business\n---\n# 人工笔记\n规则。\n")
    assert service._eligible(manual)


def test_moved_code_index_uses_managed_blob_revision_not_only_latest_commit(store, monkeypatch):
    monkeypatch.setattr(store, "get_repository", lambda _: {"id": "repo", "commit": "b" * 40})
    note = store.save_note("整理/source.md", "---\ntype: code\nrepository_id: repo\nsource_commit: " + "a" * 40
                           + "\nsource_path: refunds.py\n---\n# refunds.py\n代码索引。\n")
    with store._db() as db:
        db.execute("INSERT INTO managed_notes VALUES (?,?,?,?,?)", ("repo", "refunds.py", "代码索引/repo/hash.md", "same-blob", note["revision"]))
    service = ToolsService(store)
    assert service._eligible(note)  # The tracked blob did not change between commits.
    with store._db() as db:
        db.execute("UPDATE managed_notes SET blob=?,revision=? WHERE repository_id=?", ("new-blob", "new-revision", "repo"))
    assert not service._eligible(note)
    with store._db() as db:
        db.execute("DELETE FROM managed_notes WHERE repository_id=?", ("repo",))
    assert not service._eligible(note)


@pytest.mark.parametrize("name,args", [
    ("knowledge_read", {"path": "../outside.md"}),
    ("knowledge_read", {"path": "C:/outside.md"}),
    ("knowledge_list", {"folder": "../"}),
    ("knowledge_list", {"offset": -1}),
    ("knowledge_list", {"limit": True}),
    ("knowledge_links", {"path": "note.md", "direction": "secret"}),
    ("knowledge_search", {"query": ""}),
    ("knowledge_search", {"query": "x", "repository_id": "unauthorized"}),
    ("knowledge_delete", {"path": "note.md"}),
])
def test_tool_arguments_do_not_expand_capabilities(store, name, args):
    with pytest.raises(CoreError):
        KnowledgeNavigator(store).call(name, args)


def test_mcp_and_internal_tools_share_section_and_relation_receipts(store):
    store.save_note("entry.md", "# Entry\n[[detail#Condition]]\n")
    store.save_note("detail.md", "# Details\n## Condition\nActual policy.\n")
    async def check():
        server = create_server(store)
        names = {tool.name for tool in await server.list_tools()}
        assert {"knowledge_list", "knowledge_outline", "knowledge_links"} <= names
        result = await server.call_tool("knowledge_links", {"path": "entry.md"})
        blocks = result[0] if isinstance(result, tuple) else result
        receipt = json.loads(blocks[0].text)
        assert receipt == KnowledgeNavigator(store).links("entry.md")
        result = await server.call_tool("knowledge_read", {"path": "detail.md", "heading": "Condition"})
        blocks = result[0] if isinstance(result, tuple) else result
        assert json.loads(blocks[0].text)["content"] == "## Condition\nActual policy.\n"
    asyncio.run(check())
