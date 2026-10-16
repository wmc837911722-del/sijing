"""Validate model analysis evidence and normalize only Markdown structure."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from urllib.parse import quote

from .core import now
from .providers import ToolError


class AnalysisOutputError(ToolError):
    def __init__(self, message, category="citation"):
        super().__init__(message, 502)
        self.category = category


def _source_lines(content):
    # Git counts LF delimiters; Unicode paragraph separators are source text.
    lines = content.split("\n")
    if not lines[-1]:
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def prepare_file(item, limit, wire_budget):
    content = item["content"][:limit]
    while content:
        lines = _source_lines(content)
        numbered = "\n".join(f"{index}: {line}" for index, line in enumerate(lines, 1))
        exposed = {"path": item["path"], "content": numbered, "line_count": len(lines),
                   "truncated": len(content) < len(item["content"]) or bool(item.get("truncated", False))}
        size = len(json.dumps(exposed, ensure_ascii=False))
        if size <= wire_budget:
            return {**exposed, "content": content, "numbered_content": numbered}, size
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


def verify_sources(sources, files):
    available = {item["path"]: item["content"] for item in files}
    checked = []
    for source in sources:
        path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(path, str) or path not in available:
            raise AnalysisOutputError("模型引用了未提供的代码文件，未写入。")
        content = available[path]
        start, end = source.get("line_start", 1), source.get("line_end", 1)
        lines = _source_lines(content)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise AnalysisOutputError("模型引用行号无效，未写入。")
        quote = source.get("quote")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
            raise AnalysisOutputError("模型引用缺少有效的原文摘录，未写入。")
        matches = []
        for match in re.finditer(re.escape(quote), content):
            first = content.count("\n", 0, match.start()) + 1
            last = first + quote.count("\n") - int(quote.endswith("\n"))
            matches.append((first, last))
        if len(matches) > 1:
            matches = [(first, last) for first, last in matches if start <= first <= last <= end]
        if len(matches) != 1:
            raise AnalysisOutputError("模型引用摘录无法在已提供代码中唯一核实，未写入。")
        first, last = matches[0]
        excerpt = "\n".join(lines[first - 1:last])
        checked.append({"path": path, "line_start": first, "line_end": last, "excerpt": excerpt})
    return checked


def render_excerpt(source, commit, repository_id):
    excerpt = source["excerpt"]
    fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", excerpt)), default=3))
    reference = (f"source://{quote(repository_id, safe='')}/{commit}/{quote(source['path'], safe='/')}"
                 f"#L{source['line_start']}-L{source['line_end']}")
    label = source["path"].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return (f"- [{label}]({reference}) · L{source['line_start']}–L{source['line_end']} · commit `{commit}`\n\n"
            f"{fence}text\n{excerpt}\n{fence}")


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
        store.config_set(key, state)
        raise
    else:
        state.update(status="completed", updated_at=now())
        store.config_set(key, state)
