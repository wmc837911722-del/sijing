"""Validate model analysis evidence and normalize only Markdown structure."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
from urllib.parse import quote

from .core import now
from .providers import ToolError


class AnalysisOutputError(ToolError):
    def __init__(self, message, category="citation", details=None):
        super().__init__(message, 502)
        self.category = category
        self.details = details


def _source_lines(content):
    # Git counts LF delimiters; Unicode paragraph separators are source text.
    lines = content.split("\n")
    if not lines[-1]:
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _evidence(path, content):
    groups, uncovered = [], []
    pending, first, last = "", 1, 1
    lines = content.split("\n")
    for index, body in enumerate(lines, 1):
        line = body + ("\n" if index < len(lines) else "")
        if not line:
            continue
        if pending and len(pending) + len(line) > 1500:
            groups.append((first, last, pending))
            pending = ""
        if len(line) > 1500:
            uncovered.append(index)
            continue
        if not pending:
            first = index
        pending += line
        last = index
    if pending:
        groups.append((first, last, pending))
    blocks = []
    for first, last, snippet in groups:
        identifier = hashlib.sha256(f"{path}\0{first}\0{last}\0{snippet}".encode()).hexdigest()[:24]
        blocks.append({"evidence_id": "e_" + identifier, "path": path,
                       "line_start": first, "line_end": last, "quote": snippet})
    return blocks, uncovered


def prepare_file(item, limit, wire_budget):
    content = item["content"][:limit]
    while content:
        # An upstream/context cap may have cut the final source line. Only
        # complete lines can receive a full-line evidence range.
        if (len(content) < len(item["content"]) or item.get("truncated", False)) and not content.endswith("\n"):
            boundary = content.rfind("\n")
            content = content[:boundary + 1] if boundary >= 0 else ""
            if not content:
                break
        lines = _source_lines(content)
        numbered = "\n".join(f"{index}: {line}" for index, line in enumerate(lines, 1))
        blocks, uncovered = _evidence(item["path"], content)
        metadata = [{key: block[key] for key in ("evidence_id", "line_start", "line_end")} for block in blocks]
        exposed = {"path": item["path"], "content": numbered, "line_count": len(lines),
                   "truncated": len(content) < len(item["content"]) or bool(item.get("truncated", False)),
                   "evidence": metadata, "evidence_uncovered_lines": uncovered}
        size = len(json.dumps(exposed, ensure_ascii=False))
        if size <= wire_budget:
            return {**exposed, "content": content, "numbered_content": numbered,
                    "evidence": blocks, "evidence_metadata": metadata}, size
        content = content[:max(0, len(content) * wire_budget // size - 1)]
    return None, 0


_FENCE_START = re.compile(r"(?:\A|(?<=\n)|(?<=\\n))(?P<mark>`{3,}|~{3,})(?P<lang>[^\s`~\\]*)[ \t]*(?:\r?\n|(?<!\\)\\n)")
_PROTECTED = re.compile(r'`+[^`\n]*`+|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|[A-Za-z]:[\\/][^\s`"\']+')


def _prose(text):
    def normalize(part):
        def replace(match):
            following = part[match.end():]
            previous = re.split(r"\n|(?<!\\)\\n", part[:match.start()])[-1]
            if (len(match.group()) > 2 or re.match(r"#{1,6} ", previous)
                    or re.match(r"(?:#{1,6} |[-*+] |\d+\. |> |\|?[^\n|]+\||`{3}|~{3})", following)):
                return "\n" * (len(match.group()) // 2)
            return match.group()
        return re.sub(r"(?<!\\)(?:\\n)+", replace, part)
    pieces, offset = [], 0
    for match in _PROTECTED.finditer(text):
        pieces.extend((normalize(text[offset:match.start()]), match.group()))
        offset = match.end()
    pieces.append(normalize(text[offset:]))
    return "".join(pieces)


def normalize_markdown(text):
    """Do not decode string escapes in code, inline literals, JSON or paths."""
    pieces, offset = [], 0
    while opening := _FENCE_START.search(text, offset):
        pieces.append(_prose(text[offset:opening.start()]))
        closing = re.compile(r"(?:\r?\n|(?<!\\)\\n)" + re.escape(opening["mark"]) + r"[ \t]*(?=$|\r?\n|(?<!\\)\\n)").search(text, opening.end())
        if closing is None:
            pieces.append(text[opening.start():])
            return "".join(pieces)
        pieces.append(opening["mark"] + opening["lang"] + "\n" + text[opening.end():closing.start()] + "\n" + opening["mark"])
        offset = closing.end()
    pieces.append(_prose(text[offset:]))
    return "".join(pieces)


def verify_sources(sources, files, note_key=None):
    available = {item["path"]: item["content"] for item in files}
    file_indices = {item["path"]: index for index, item in enumerate(files, 1)}
    candidates = {candidate["evidence_id"]: candidate for item in files for candidate in item.get("evidence", [])}
    checked = []
    for source_index, source in enumerate(sources, 1):
        if isinstance(source, dict) and "evidence_id" in source:
            identifier = source["evidence_id"]
            candidate = candidates.get(identifier) if isinstance(identifier, str) else None
            if candidate is None:
                raise AnalysisOutputError("模型引用了本次请求未提供的证据编号，未写入。", details={
                    "reason_code": "unknown_evidence_id", "note_key": note_key, "source_index": source_index})
            if any(key in source and (type(source[key]) is not type(candidate[key]) or source[key] != candidate[key])
                   for key in ("path", "line_start", "line_end", "quote")):
                raise AnalysisOutputError("模型证据编号与引用坐标不一致，未写入。", details={
                    "reason_code": "inconsistent_evidence_id", "note_key": note_key, "source_index": source_index})
            lines = _source_lines(available[candidate["path"]])
            first, last = candidate["line_start"], candidate["line_end"]
            checked.append({"path": candidate["path"], "line_start": first, "line_end": last,
                            "excerpt": "\n".join(lines[first - 1:last])})
            continue
        path = source.get("path") if isinstance(source, dict) else None
        quote = source.get("quote") if isinstance(source, dict) else None
        details = {"source_index": source_index}
        if note_key in {"overview", "entities", "flows", "rules", "data"}:
            details["note_key"] = note_key
        if isinstance(path, str) and path in file_indices:
            details["file_index"] = file_indices[path]
        if isinstance(quote, str):
            details.update(quote_chars=len(quote), quote_lines=len(quote.splitlines()), literal_newline="\\n" in quote)

        def reject(reason_code, message):
            raise AnalysisOutputError(message, details={**details, "reason_code": reason_code})

        if not isinstance(path, str) or path not in available:
            reject("unprovided_path", "模型引用了未提供的代码文件，未写入。")
        content = available[path]
        start, end = source.get("line_start", 1), source.get("line_end", 1)
        lines = _source_lines(content)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            reject("invalid_line_range", "模型引用行号无效，未写入。")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
            reject("quote_too_long" if isinstance(quote, str) and len(quote) > 2000 else "missing_quote",
                   "模型引用缺少有效的原文摘录，未写入。")
        matches = []
        for match in re.finditer(re.escape(quote), content):
            first = content.count("\n", 0, match.start()) + 1
            last = first + quote.count("\n") - int(quote.endswith("\n"))
            matches.append((first, last))
        found = bool(matches)
        if len(matches) > 1:
            matches = [(first, last) for first, last in matches if start <= first <= last <= end]
        if len(matches) != 1:
            reject("ambiguous_quote" if found else "quote_not_found", "模型引用摘录无法在已提供代码中唯一核实，未写入。")
        first, last = matches[0]
        excerpt = "\n".join(lines[first - 1:last])
        checked.append({"path": path, "line_start": first, "line_end": last, "excerpt": excerpt})
    return checked


def render_excerpt(source, commit, repository_id, include_excerpt=True):
    excerpt = source["excerpt"]
    fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", excerpt)), default=3))
    reference = (f"source://{quote(repository_id, safe='')}/{commit}/{quote(source['path'], safe='/')}"
                 f"#L{source['line_start']}-L{source['line_end']}")
    label = source["path"].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    link = f"- [{label}]({reference}) · L{source['line_start']}–L{source['line_end']} · commit `{commit}`"
    return link + (f"\n\n{fence}text\n{excerpt}\n{fence}" if include_excerpt else "")


@contextmanager
def analysis_status(store, repo_id):
    repo = store.get_repository(repo_id)
    key = f"ai_analysis_status:{repo_id}"
    state = {"status": "running", "updated_at": now(), "summary": "正在分析有界代码片段。",
             "commit": repo.get("commit", ""), "vault_path": str(store.vault)}
    store.config_set(key, state)
    try:
        yield state
    except Exception as error:
        category = getattr(error, "category", None) or ("changed" if getattr(error, "status_code", None) == 409
                    else "model" if isinstance(error, ToolError) else "internal")
        summaries = {"citation": "代码引用未通过原文核验，请重新分析；现有笔记保留。",
                     "format": "模型未返回有效业务笔记，请重新分析；现有笔记保留。",
                     "changed": "分析期间知识库、代码版本或模型绑定变化，请确认后重试。",
                     "model": "模型调用未获得有效分析结果，请检查连接、额度或模型后重试。",
                     "internal": "分析未完成，请稍后重试或检查本地服务。"}
        state.update(status="failed", category=category, summary=summaries.get(category, summaries["internal"]), updated_at=now())
        if isinstance(error, AnalysisOutputError) and error.details:
            reasons = {"unprovided_path": "引用文件不在已提供范围", "invalid_line_range": "引用行号格式或范围无效",
                       "quote_too_long": "引用摘录超过2000字符", "missing_quote": "缺少有效原文摘录",
                       "unknown_evidence_id": "引用证据编号不在本次请求范围", "inconsistent_evidence_id": "证据编号与引用坐标不一致",
                       "ambiguous_quote": "原文摘录有多个匹配且行号无法消歧", "quote_not_found": "摘录与已提供原文不匹配"}
            state["error_details"] = error.details
            state["summary"] = "代码引用未通过核验：" + reasons[error.details["reason_code"]] + "；现有笔记保留。"
        store.config_set(key, state)
        raise
    else:
        state.update(status="completed", updated_at=now())
        store.config_set(key, state)
