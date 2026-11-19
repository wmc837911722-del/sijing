"""Public business prose has no source provenance or implementation details.

Source receipts belong to private analysis state. This is a deterministic final
publication guard, not a request for the model to silently redact its mistakes.
"""
from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import unquote

import yaml

from .providers import ToolError


PUBLIC_FIELDS = {"title", "type", "content_role", "tags", "generated"}
ROLES = {"business_knowledge", "business_map", "analysis_summary"}
BUSINESS_TYPES = {"business", "rule", "entity", "process", "note", "overview", "data"}
SOURCE_SUFFIXES = frozenset("py ts tsx js jsx java kt go rs cs php rb vue sql prisma graphql proto mjs cjs mts cts swift dart c h cpp hpp scala groovy svelte html css scss sass less wxml wxss json xml yaml yml kts gradle properties toml".split())
PUBLIC_CONTENT_KINDS = {
    "document_format": "文档格式",
    "web_address": "链接地址",
    "source_location": "文件位置或源码文件名",
    "technical_notation": "技术语法或内部标记",
    "encoded_text": "编码片段",
    "implementation_identifier": "程序实现标识",
    "private_source_reference": "来源材料中的专用标识",
}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_BUSINESS_GLOSS = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*)[ \t]*（(?=[^（）\r\n]*[\u3400-\u9fff])"
    r"([\u3400-\u9fff0-9０-９ \t，、。；：！？“”‘’《》％—]+)）"
)
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$(?:\n|\Z)", re.MULTILINE | re.DOTALL)
_TECHNICAL = re.compile(
    r"`|(?m:^ {0,3}~{3,})|(?:source|file|ssh|git|data|javascript|module|topic|fact)\s*:\s*|"
    r"\b(?:repository_id|project_id|project_revision|source_commit|source_paths|source_repositories|"
    r"evidence_id|module_id|run_id|full_run_id|receipt_migration|snapshot_id|generated_key)\b|"
    r"\b[a-f0-9]{20,64}\b|(?P<source_location>(?:/Users/|/home/|[A-Za-z]:[\\/])|"
    r"(?:\b(?:src|node_modules|backend|frontend)/)|(?:\.(?:git|ssh|local-data|env)(?:/|\b)))|"
    r"(?:提交(?:号|版本)?|commit|revision|代码版本|版本号?|SHA(?:-?\d+)?)[\s:：('\"]*[a-f0-9]{7,64}|"
    r"(?:代码索引|00-Inbox/代码资料)/|\b(?:SELECT|INSERT|CREATE|class|def|import|package|return|const)\b|"
    r"</?[a-z]", re.I | re.ASCII)


class PublicContentError(ToolError):
    def __init__(self, kind):
        super().__init__("业务内容包含不适合公开的技术资料，未发布；内部分析断点已保留。", 422)
        self.kind = kind


def _reject(kind="document_format"):
    raise PublicContentError(kind)


def _canonical(text):
    value = unicodedata.normalize("NFKC", text)
    for _ in range(2):
        value = html.unescape(unquote(value))
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", value)


def implementation_identifiers(text):
    """Scan whole ASCII tokens once, including lowerCamel and method calls."""
    for match in _IDENTIFIER.finditer(text):
        token = match[0]
        mixed_case = any(char.isupper() for char in token[1:]) and any(char.islower() for char in token)
        end = match.end()
        while end < len(text) and text[end] in " \t\r\n":
            end += 1
        if "_" in token or mixed_case or end < len(text) and text[end] == "(":
            yield token


def _read_frontmatter(header):
    loader = yaml.SafeLoader(header)
    try:
        root = loader.get_single_node()
        if not isinstance(root, yaml.MappingNode):
            _reject()
        meta = loader.construct_document(root)
        if len(meta) != len(root.value):
            _reject()
        # Only these two structural scalars use underscores. Keep all other
        # original bytes, including comments, available to the public-text check.
        spans = []
        for key, value in root.value:
            if key.value == "content_role" and isinstance(value, yaml.ScalarNode) and value.value in ROLES:
                spans = [key, value]
        for node in sorted(spans, key=lambda node: node.start_mark.index, reverse=True):
            start, end = node.start_mark.index, node.end_mark.index
            header = header[:start] + header[start:end].replace(node.value, "role") + header[end:]
        return meta, header
    except yaml.YAMLError:
        _reject()
    finally:
        loader.dispose()


def public_business_metadata(text: str, forbidden_tokens=()) -> dict:
    """Validate public prose and return only its parsed frontmatter metadata."""
    if not isinstance(text, str):
        _reject()
    meta = {}
    # A business note may be validated as a whole; inspect only the allowed
    # presentation metadata, never treat source-bearing YAML as hidden/private.
    if re.match(r"\A---[ \t]*\r?\n", text):
        front = _FRONTMATTER.match(text)
        if front is None:
            _reject()
        meta, raw_header = _read_frontmatter(front[1])
        if (not isinstance(meta, dict) or set(meta) - PUBLIC_FIELDS
                or not isinstance(meta.get("type", "business"), str) or meta.get("type", "business") not in BUSINESS_TYPES
                or not isinstance(meta.get("content_role", "business_knowledge"), str)
                or meta.get("content_role", "business_knowledge") not in ROLES
                or not isinstance(meta.get("title", ""), str)
                or not isinstance(meta.get("tags", []), list)
                or any(not isinstance(tag, str) for tag in meta.get("tags", []))
                or "generated" in meta and meta["generated"] is not True):
            _reject()
        text = "\n".join([raw_header, meta.get("title", ""), *meta.get("tags", []), text[front.end():]])
    canonical = _canonical(text)
    if "://" in canonical:
        _reject("web_address")
    technical = _TECHNICAL.search(canonical)
    if technical:
        _reject(technical.lastgroup or "technical_notation")
    if re.search(r"%[0-9a-f]{2}|&(?:#[xX]?[0-9a-f]+|[a-z]+);", canonical, re.I):
        _reject("encoded_text")
    if "." in canonical and any(match[1].casefold() in SOURCE_SUFFIXES
            for match in re.finditer(r"\.([A-Za-z][A-Za-z0-9]{0,15})(?![A-Za-z0-9])", canonical, re.ASCII)):
        _reject("source_location")
    # Preserve the punctuation distinction between SKU（商品规格） and a
    # method call while keeping the normalized text for all other checks.
    identifiers = _canonical(_BUSINESS_GLOSS.sub(r"\1、\2、", text)) if "（" in text else canonical
    if next(implementation_identifiers(identifiers), None) is not None:
        _reject("implementation_identifier")
    folded = canonical.casefold()
    for token in forbidden_tokens:
        if not isinstance(token, str) or not token.strip():
            continue
        needle = _canonical(token).casefold()
        if len(needle) >= 4 and needle in folded:
            _reject("private_source_reference")
    return meta


def validate_public_business_text(text: str, forbidden_tokens=()) -> None:
    public_business_metadata(text, forbidden_tokens)


def render_public_note(title: str, body: str, *, content_role="business_knowledge", tags=()) -> str:
    if not isinstance(title, str) or not title.strip() or not isinstance(content_role, str) or content_role not in ROLES:
        _reject()
    if not isinstance(tags, (list, tuple)) or any(not isinstance(tag, str) for tag in tags):
        _reject()
    for text in (title, body, *tags):
        validate_public_business_text(text)
    metadata = {"title": title, "type": "business", "content_role": content_role,
                "tags": list(tags), "generated": True}
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + body.strip() + "\n"
