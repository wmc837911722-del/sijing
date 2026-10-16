"""Pure, lossless pagination for generated business maps and summaries."""
from __future__ import annotations

import re

from .core import CoreError


def _blocks_and_headings(body):
    blocks, pending, second, third = [], [], [], []
    fence = None
    for line in body.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if fence:
            pending.append(line)
            closing = re.fullmatch(r" {0,3}(`+|~+)[ \t]*", text)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= fence[1]:
                blocks.append(("".join(pending), True))
                pending, fence = [], None
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", text)
        if opening and (opening[1][0] == "~" or "`" not in opening[2]):
            if pending:
                blocks.append(("".join(pending), False))
            pending = [line]
            fence = (opening[1][0], len(opening[1]))
            continue
        heading = re.fullmatch(r" {0,3}(#{2,3})[ \t]+(.+?)\s*", text)
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2]).strip()
            (third if len(heading[1]) == 3 else second).append(title)
        pending.append(line)
        if not text.strip():
            blocks.append(("".join(pending), False))
            pending = []
    if pending:
        blocks.append(("".join(pending), fence is not None))
    return blocks, third or second


def paginate_document(key, title, body, source_paths, path, page_bytes):
    """Return a complete publication plan without reading or writing files.

    Page headers are additional presentation. Removing those exact headers and
    joining the page bodies reconstructs the original text byte for byte.
    Paragraphs are preferred boundaries; oversized prose splits at whole lines.
    Fenced examples remain intact and never become business headings.
    """
    if not isinstance(page_bytes, int) or isinstance(page_bytes, bool) or page_bytes <= 0:
        raise CoreError(400, "分析分页上限必须为正整数。")
    if not isinstance(path, str) or not path.endswith(".md"):
        raise CoreError(400, "分析分页目标必须为 Markdown 路径。")
    sources = list(source_paths)
    if len(body.encode("utf-8")) <= page_bytes:
        return {"documents": {key: (title, body, sources)}, "paths": {key: path}, "part_keys": []}
    blocks, headings = _blocks_and_headings(body)
    pages, current, size = [], [], 0

    def header(number):
        return f"# {title} · 第{number}部分\n\n"

    def capacity():
        available = page_bytes - len(header(len(pages) + 1).encode("utf-8"))
        if available <= 0:
            raise CoreError(413, "分析分页标题超过分页上限，未截断原文。")
        return available

    def flush():
        nonlocal current, size
        if current:
            pages.append("".join(current))
            current, size = [], 0

    def append(piece):
        nonlocal size
        weight = len(piece.encode("utf-8"))
        if current and size + weight > capacity():
            flush()
        if weight > capacity():
            raise CoreError(413, "单行分析内容超过分页上限，未截断事实；原文未改动。")
        current.append(piece)
        size += weight

    for block, fenced in blocks:
        weight = len(block.encode("utf-8"))
        if current and size + weight > capacity():
            flush()
        if weight <= capacity():
            append(block)
        elif fenced:
            raise CoreError(413, "完整代码示例超过分析分页上限，未拆改原文。")
        else:
            for line in block.splitlines(keepends=True):
                append(line)
    flush()
    part_keys = [f"{key}:part:{number}" for number in range(1, len(pages) + 1)]
    paths = {key: path, **{part: path[:-3] + f"/第{number}部分.md" for number, part in enumerate(part_keys, 1)}}
    homepage = f"# {title}\n\n完整内容分为 {len(pages)} 部分。以下保留全部业务主题与分页入口，原始分析内容未截断。\n"
    if headings:
        homepage += "\n## 全部业务主题\n\n" + "\n".join("- " + heading for heading in headings) + "\n"
    homepage += "\n## 完整内容分页\n\n" + "\n".join(f"- [[{paths[part][:-3]}|第{number}部分]]" for number, part in enumerate(part_keys, 1)) + "\n"
    if len(homepage.encode("utf-8")) > page_bytes:
        raise CoreError(413, "分析分页首页索引超过上限，未丢弃任何业务主题或分页入口；原文未改动。")
    documents = {key: (title, homepage, sources)}
    documents.update({part: (title + f" · 第{number}部分", header(number) + page, list(sources))
                      for number, (part, page) in enumerate(zip(part_keys, pages), 1)})
    return {"documents": documents, "paths": paths, "part_keys": part_keys}
