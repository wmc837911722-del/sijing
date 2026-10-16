"""Bounded, read-only navigation over the current Markdown vault.

Discovery returns short receipts; the caller chooses which note, section or
relationship to inspect next. Nothing here runs a model or executes note text.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import log
from pathlib import PurePosixPath
import posixpath
import re
from urllib.parse import unquote

from .core import CoreError
from .content_layout import is_source_document


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


_PATH = {"type": "string", "description": "Exact vault-relative Markdown path returned by discovery."}
KNOWLEDGE_TOOLS = [
    _tool("knowledge_list", "List visible Markdown notes under a folder. Choose an entry; no note body is read into context.", {
        "folder": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _tool("knowledge_search", "Discover notes by keyword, exact path, title or business term. Index-only results locate files, not business evidence. Use next_offset for more matches.", {
        "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "offset": {"type": "integer", "minimum": 0}}, ("query",)),
    _tool("knowledge_outline", "Inspect headings and block IDs with character ranges before choosing a section to read. Use next_offset to inspect later entries in large outlines.", {
        "path": _PATH, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("path",)),
    _tool("knowledge_read", "Read a note or heading/#^block in bounded pages. Offset is relative to the selected section. Pass revision from discovery to detect changes; next_offset continues the same selection.", {
        "path": _PATH, "heading": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 24000},
        "revision": {"type": "string"}}, ("path",)),
    _tool("knowledge_links", "Follow resolved outgoing links or backlinks to related notes. Only accessible notes are returned; missing or ambiguous links are omitted.", {
        "path": _PATH, "direction": {"type": "string", "enum": ["outgoing", "incoming", "both"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "offset": {"type": "integer", "minimum": 0}}, ("path",)),
]


def _blank(value):
    return re.sub(r"[^\r\n]", " ", value)


def _structure_text(content):
    """Mask non-prose without moving character offsets in the authoritative text."""
    text = re.sub(r"\A---[^\S\r\n]*\r?\n.*?\r?\n---[^\S\r\n]*(?:\r?\n|$)",
                  lambda match: _blank(match[0]), content, count=1, flags=re.S)
    text = re.sub(r"<!--[\s\S]*?-->", lambda match: _blank(match[0]), text)
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            lines.append(_blank(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            lines.append(_blank(line))
        elif line.startswith("    ") or line.startswith("\t"):
            lines.append(_blank(line))
        else:
            lines.append(line)
    return "".join(lines)


def _prose_text(content):
    return re.sub(r"(`+)(?!`)(.*?)\1(?!`)", lambda match: _blank(match[0]),
                  _structure_text(content), flags=re.S)


def _outline(content):
    text = _structure_text(content)
    lines = text.splitlines(keepends=True)
    headings, blocks, offset, paragraph_start = [], [], 0, 0
    for number, line in enumerate(lines):
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line.rstrip("\r\n"))
        if match:
            headings.append({"heading": re.sub(r"\s+#+$", "", match[2]), "level": len(match[1]), "start": offset, "line": number + 1})
            paragraph_start = offset + len(line)
        elif number + 1 < len(lines) and line.strip() and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*(?:\r?\n)?", lines[number + 1]):
            headings.append({"heading": line.strip(), "level": 1 if lines[number + 1].lstrip().startswith("=") else 2,
                             "start": offset, "line": number + 1})
            paragraph_start = offset + len(line) + len(lines[number + 1])
        block = re.search(r"(?:^|\s)\^([\w-]+)[ \t]*(?:\r?\n)?$", line)
        if block:
            blocks.append({"id": block[1], "start": min(paragraph_start, offset), "end": offset + len(line)})
        offset += len(line)
        if not line.strip():
            paragraph_start = offset
    stack = []
    for heading in headings:
        while stack and stack[-1]["level"] >= heading["level"]:
            stack.pop()["end"] = heading["start"]
        heading["end"] = len(content)
        stack.append(heading)
    return headings, blocks


def _slug_heading(value):
    return re.sub(r"[^\w\s-]", "", value.casefold()).strip().replace(" ", "-")


class KnowledgeNavigator:
    def __init__(self, store, allowed=None):
        self.store = store
        self.allowed = allowed or (lambda _note: True)

    @staticmethod
    def schemas():
        return deepcopy(KNOWLEDGE_TOOLS)

    def _snapshot(self):
        self.store.refresh()
        # Use Store's parsed Markdown cache atomically. read_note computes the
        # complete backlink graph for every note, which discovery does not need.
        with self.store._lock:
            notes = {path: dict(note) for path, note in self.store._cache.items()}
            version = self.store.version
        visible = {path: note for path, note in notes.items() if self.allowed(note)}
        return notes, visible, version

    @staticmethod
    def _identity(note):
        return {key: note[key] for key in ("path", "title", "revision")}

    @staticmethod
    def _integer(value, name, default, maximum=None):
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if name == "limit" else 0):
            raise CoreError(400, f"{name} 必须是有效的非负整数" if name != "limit" else "limit 必须是正整数")
        return min(value, maximum) if maximum is not None else value

    def call(self, name, args):
        schema = next((tool["function"]["parameters"] for tool in KNOWLEDGE_TOOLS if tool["function"]["name"] == name), None)
        if schema is None:
            raise CoreError(400, "未知知识工具")
        if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
            raise CoreError(400, "知识工具参数不完整或包含未知字段")
        return getattr(self, name.removeprefix("knowledge_"))(**args)

    def list(self, folder="", offset=0, limit=30):
        if not isinstance(folder, str) or "\\" in folder or folder.startswith("/") or any(p in {".", ".."} or p.startswith(".") for p in folder.split("/") if p):
            raise CoreError(400, "folder 必须是知识库内的相对目录")
        folder = folder.rstrip("/")
        offset, limit = self._integer(offset, "offset", 0), self._integer(limit, "limit", 30, 50)
        _, visible, version = self._snapshot()
        notes = sorted((note for path, note in visible.items() if not folder or path.startswith(folder + "/")), key=lambda note: note["path"])
        selected = [{**self._identity(note), "type": note["type"], "tags": note["tags"]} for note in notes[offset:offset + limit]]
        more = offset + limit < len(notes)
        return {"version": version, "folder": folder, "notes": selected, "offset": offset, "total": len(notes),
                "truncated": more, "next_offset": offset + limit if more else None}

    def search(self, query, limit=8, offset=0):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise CoreError(400, "请输入 1–1000 字符的搜索词")
        limit, offset = self._integer(limit, "limit", 8, 20), self._integer(offset, "offset", 0)
        _, visible, version = self._snapshot()
        folded = query.strip().casefold()
        terms = re.findall(r"[a-z0-9_./-]+|[\u3400-\u9fff]+", folded)
        for phrase in list(terms):
            if re.fullmatch(r"[\u3400-\u9fff]{3,}", phrase):
                terms.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
        terms = list(dict.fromkeys([folded, *terms]))
        documents, frequencies = [], Counter()
        for note in visible.values():
            index_only = is_source_document(note)
            title = note["title"].casefold() if not index_only or note.get("type") == "code" else ""
            path = note["path"].casefold()
            body = "" if index_only else re.sub(
                r"\A---[^\S\r\n]*\r?\n.*?\r?\n---[^\S\r\n]*(?:\r?\n|$)",
                lambda match: _blank(match[0]), note["content"], count=1, flags=re.S).casefold()
            weights = {}
            for term in terms:
                weight = (8 if term in title else 0) + (8 if term in path else 0) + (1 if term in body else 0)
                if weight:
                    weights[term] = weight
            if weights:
                frequencies.update(weights.keys())
                documents.append((note, body, weights, index_only))
        matches = []
        for note, body, weights, index_only in documents:
            # Shared repository or framework terms must not drown out specific business terms.
            score = sum(weight * log(1 + len(visible) / frequencies[term]) for term, weight in weights.items())
            hit = body.find(folded)
            if hit < 0:
                best = min((term for term in weights if term in body),
                           key=lambda term: (frequencies[term], -len(term), body.find(term)), default=None)
                hit = body.find(best) if best is not None else 0
            start = max(0, hit - 120)
            excerpt = (f"代码文件索引：仅用于定位源文件，不提供业务结论。\n{note['title']}"[:480]
                       if index_only else note["content"][start:start + 480])
            matches.append((score, {**self._identity(note), "excerpt": excerpt,
                                    "is_current": True, "index_only": index_only}))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
        more = offset + limit < len(matches)
        return {"version": version, "notes": [item for _, item in matches[offset:offset + limit]],
                "offset": offset, "total": len(matches), "truncated": more,
                "next_offset": offset + limit if more else None}

    def _note(self, path, visible):
        if not isinstance(path, str):
            raise CoreError(400, "path 必须是相对 Markdown 路径")
        self.store._path(path)
        if path not in visible:
            raise CoreError(404, "笔记不存在或不在当前可读取范围内")
        return visible[path]

    def outline(self, path, offset=0, limit=100):
        offset, limit = self._integer(offset, "offset", 0), self._integer(limit, "limit", 100, 200)
        _, visible, version = self._snapshot()
        note = self._note(path, visible)
        headings, blocks = _outline(note["content"])
        total = max(len(headings), len(blocks))
        more = offset + limit < total
        return {"version": version, **self._identity(note), "headings": headings[offset:offset + limit], "blocks": blocks[offset:offset + limit],
                "total_headings": len(headings), "total_blocks": len(blocks),
                "offset": offset, "total": total, "next_offset": offset + limit if more else None,
                "truncated": more, "total_chars": len(note["content"])}

    def read(self, path, heading=None, offset=0, limit=6000, revision=None):
        if not isinstance(path, str) or heading is not None and not isinstance(heading, str) or revision is not None and not isinstance(revision, str):
            raise CoreError(400, "path、heading 和 revision 必须是字符串")
        if "#" in path:
            path, anchor = path.split("#", 1)
            heading = heading or unquote(anchor)
        offset, limit = self._integer(offset, "offset", 0), self._integer(limit, "limit", 6000, 24000)
        _, visible, version = self._snapshot()
        note = self._note(path, visible)
        if revision is not None and revision != note["revision"]:
            raise CoreError(409, "笔记已变化，请重新搜索或读取大纲后继续，不要混用不同版本的证据")
        content = note["content"]
        start, end = 0, len(content)
        if heading:
            anchor = unquote(heading).lstrip("#")
            headings, blocks = _outline(content)
            if anchor.startswith("^"):
                matches = [block for block in blocks if block["id"] == anchor[1:]]
            else:
                matches = [item for item in headings if item["heading"].casefold() == anchor.casefold()]
                if not matches:
                    matches = [item for item in headings if _slug_heading(item["heading"]) == anchor.casefold()]
            if not matches:
                raise CoreError(404, "未找到该标题或块，请先读取大纲")
            if len(matches) != 1:
                raise CoreError(409, "标题或块标识不唯一，请根据大纲使用全文 offset 定位")
            start, end = matches[0]["start"], matches[0]["end"]
        selected = content[start:end]
        more = offset + limit < len(selected)
        return {"version": version, **self._identity(note), "index_only": is_source_document(note), "content": selected[offset:offset + limit],
                "offset": offset, "total_chars": len(selected), "truncated": more,
                "next_offset": offset + limit if more else None, "selection_start": start, "selection_end": end,
                "heading": heading, "is_current": True}

    @staticmethod
    def _raw_links(content):
        text = _prose_text(content)
        for match in re.finditer(r"(?<!\\)\[\[([^\]\n]+)\]\]", text):
            yield match[1].split("|", 1)[0].strip(), "wiki"
        for match in re.finditer(r"(?<![\\\[])\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)", text):
            yield match[1] or match[2], "markdown"
        references = {}
        for match in re.finditer(r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|([^\s]+))", text):
            references.setdefault(" ".join(match[1].casefold().split()), match[2] or match[3])
        for match in re.finditer(r"(?<![\\\[])\[([^\]\n]+)\](?:\[([^\]\n]*)\])?(?![\](:])", text):
            key = " ".join((match[2] or match[1]).casefold().split())
            if key in references:
                yield references[key], "markdown"

    @staticmethod
    def _resolve(source, raw, kind, notes):
        destination, _, anchor = unquote(raw).partition("#")
        if not destination:
            return source, anchor
        if re.match(r"^[a-z][a-z0-9+.-]*:", destination, re.I) or "?" in destination or "\\" in destination:
            return None, anchor
        if (kind == "wiki" and not destination.casefold().endswith(".md")) or not PurePosixPath(destination).suffix:
            destination += ".md"
        if PurePosixPath(destination).suffix.casefold() != ".md":
            return None, anchor
        root = posixpath.normpath(destination.lstrip("/"))
        relative = posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent), destination))
        if kind == "markdown":
            candidate = root if destination.startswith("/") else relative
            return (candidate if candidate in notes else None), anchor
        candidates = {candidate for candidate in (root, relative) if candidate in notes}
        if not candidates and "/" not in destination:
            candidates = {path for path, note in notes.items() if PurePosixPath(path).name.casefold() == destination.casefold()
                          or note["title"].casefold() == destination[:-3].casefold()}
        return (next(iter(candidates)) if len(candidates) == 1 else None), anchor

    def _edges(self, notes, visible):
        edges, seen = [], set()
        for source, note in visible.items():
            for raw, kind in self._raw_links(note["content"]):
                target, anchor = self._resolve(source, raw, kind, notes)
                identity = (source, target, anchor, kind)
                if target in visible and identity not in seen:
                    seen.add(identity)
                    edges.append({"source": source, "target": target, "anchor": anchor, "kind": kind})
        return sorted(edges, key=lambda edge: (edge["source"], edge["target"], edge["anchor"], edge["kind"]))

    def links(self, path, direction="both", limit=30, offset=0):
        if not isinstance(direction, str) or direction not in {"both", "incoming", "outgoing"}:
            raise CoreError(400, "direction 必须是 outgoing、incoming 或 both")
        offset, limit = self._integer(offset, "offset", 0), self._integer(limit, "limit", 30, 50)
        notes, visible, version = self._snapshot()
        note = self._note(path, visible)
        edges = [edge for edge in self._edges(notes, visible)
                 if direction in {"both", "outgoing"} and edge["source"] == path
                 or direction in {"both", "incoming"} and edge["target"] == path]
        selected = edges[offset:offset + limit]
        more = offset + limit < len(edges)
        ids = {path} | {edge[key] for edge in selected for key in ("source", "target")}
        return {"version": version, "path": path, "revision": note["revision"], "direction": direction,
                "nodes": [self._identity(visible[key]) for key in sorted(ids)], "edges": selected,
                "offset": offset, "total": len(edges), "next_offset": offset + limit if more else None,
                "truncated": more}

    def graph(self, path=None, limit=500):
        """Compatibility view for MCP clients that already use knowledge_graph."""
        notes, visible, version = self._snapshot()
        edges = self._edges(notes, visible)
        ids = set(visible)
        if path is not None:
            self._note(path, visible)
            ids = {path} | {edge[key] for edge in edges if path in (edge["source"], edge["target"])
                            for key in ("source", "target")}
        selected = set(sorted(ids)[:limit])
        nodes = [{"id": key, **self._identity(visible[key]), "type": visible[key]["type"], "tags": visible[key]["tags"]}
                 for key in sorted(selected)]
        return {"version": version, "nodes": nodes,
                "edges": [edge for edge in edges if edge["source"] in selected and edge["target"] in selected],
                "truncated": len(ids) > limit}
