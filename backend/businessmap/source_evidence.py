"""Bounded, untrusted source evidence from one selected immutable Git snapshot."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from math import log
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .core import CoreError, Store


class SourceNavigator:
    COMMAND_TIMEOUT = 10
    SEARCH_TIMEOUT = 30
    MAX_METADATA_BYTES = 8 * 1024 * 1024
    MAX_BLOB_BYTES = 128_000
    MAX_CHARS = 12_000
    SEARCH_SNIPPET_CHARS = 400
    COMMON_TERMS = {suffix[1:] for suffix in Store.SOURCE_SUFFIXES} | {
        "com", "org", "src", "main", "public", "private", "protected", "class", "interface",
        "import", "package", "return", "function", "const", "def",
    }

    def __init__(self, store, repository_id):
        self.store = store
        self.repo = dict(store.get_repository(repository_id))
        self.repository_id = self.repo["id"]
        self.commit = self.repo.get("commit", "")
        if not re.fullmatch(r"[a-f0-9]{40,64}", self.commit):
            raise CoreError(409, "仓库尚未完成同步，请完成后读取源码")
        self.vault = str(store.vault)
        self.epoch = store._vault_epoch
        # Reuse the authoritative source safety filter, with bounded Git output
        # and regular-file mode checks supplied by this adapter's _git method.
        self.files = {item["path"]: item for item in Store._source_files(self, self.repo, self.commit)}
        self._guard()

    def _safe_source(self, path, size):
        return self.store._safe_source(path, size) and self._valid_path(path)

    @staticmethod
    def _valid_path(path):
        return (isinstance(path, str) and bool(path) and len(path) <= 1024
                and not re.search(r"[\\\x00-\x1f\x7f]", path)
                and not PurePosixPath(path).is_absolute()
                and PurePosixPath(path).as_posix() == path
                and all(part not in {".", ".."} for part in PurePosixPath(path).parts))

    def _guard(self):
        if str(self.store.vault) != self.vault or self.store._vault_epoch != self.epoch:
            raise CoreError(409, "知识库已切换，请重新检索源码")
        try:
            current = self.store.get_repository(self.repository_id)
        except CoreError:
            raise CoreError(409, "仓库已变化，请重新检索源码") from None
        if any(current.get(key) != self.repo.get(key) for key in ("commit", "path", "vault_path")):
            raise CoreError(409, "仓库版本已变化，请重新检索源码")

    def _run_git(self, args, *, max_bytes, allow_no_match=False, deadline=None):
        timeout = self.COMMAND_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise CoreError(408, "源码检索超时，请缩小关键词范围")
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("GIT_", "SSH_ASKPASS", "GCM_"))}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_ASKPASS="", GIT_SSH_COMMAND="false", GIT_NO_LAZY_FETCH="1")
        command = ["git", "--no-pager", "-c", "core.hooksPath=" + os.devnull,
                   "-c", "core.fsmonitor=false", "-c", "protocol.allow=never", *args]
        try:
            process = subprocess.Popen(command, cwd=Path(self.repo["path"]), env=env, shell=False,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError:
            raise CoreError(502, "无法启动只读 Git 源码操作") from None
        output = bytearray()
        overflow = threading.Event()

        def collect():
            while chunk := process.stdout.read(65_536):
                available = max_bytes - len(output)
                output.extend(chunk[:max(0, available)])
                if len(chunk) > available:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join()
            raise CoreError(408, "只读 Git 源码操作超时") from None
        finally:
            reader.join()
            process.stdout.close()
        if overflow.is_set():
            raise CoreError(413, "源码操作输出超过安全上限，请缩小范围")
        if process.returncode not in ({0, 1} if allow_no_match else {0}):
            raise CoreError(502, "只读 Git 源码操作失败，未返回源码")
        return bytes(output)

    def _git(self, args, cwd=None):
        raw = self._run_git(args, max_bytes=self.MAX_METADATA_BYTES)
        entries = []
        for entry in raw.split(b"\0"):
            if entry.split(b" ", 1)[0] not in {b"100644", b"100755"}:
                continue
            try:
                entries.append(entry.decode("utf-8"))
            except UnicodeError:
                continue
        return "\0".join(entries)

    def _blob(self, item, deadline=None):
        raw = self._run_git(["cat-file", "blob", item["blob"]], max_bytes=self.MAX_BLOB_BYTES, deadline=deadline)
        if b"\0" in raw:
            raise CoreError(415, "二进制文件不能作为源码证据")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeError:
            raise CoreError(415, "源码必须是 UTF-8 文本") from None
        # Match repository_context's redaction while retaining source line numbers.
        return re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=]\s*)['\"][^'\"\r\n]+['\"]",
                      r'\1"[REDACTED]"', content)

    @staticmethod
    def _integer(value, name, minimum, maximum=None):
        if (isinstance(value, bool) or not isinstance(value, int) or value < minimum
                or maximum is not None and value > maximum):
            raise CoreError(400, f"{name} 超出允许范围")
        return value

    @staticmethod
    def _lines(content):
        # Git line numbers count LF, not Unicode paragraph/line separators.
        lines = content.split("\n")
        if not lines[-1]:
            lines.pop()
        return [line.removesuffix("\r") for line in lines]

    def _receipt(self, path, content, line_start, line_end, max_chars):
        lines = self._lines(content)
        total = len(lines)
        if line_start > max(total, 1):
            raise CoreError(416, "源码起始行超出文件范围")
        end = min(line_end if line_end is not None else total, total)
        rendered, used, actual_end, partial = [], 0, line_start - 1, False
        for number in range(line_start, end + 1):
            line = f"{number}: {lines[number - 1]}"
            cost = len(line) + bool(rendered)
            if used + cost > max_chars:
                if not rendered:
                    rendered.append(line[:max_chars])
                    actual_end, partial = number, True
                break
            rendered.append(line)
            used += cost
            actual_end = number
        if not total:
            actual_end = 1  # Empty editors still open at line 1; content remains empty.
        next_line = actual_end if partial else actual_end + 1 if actual_end < total else None
        item = self.files[path]
        reference = (f"source://{quote(self.repository_id, safe='')}/{self.commit}/{quote(path, safe='/')}"
                     f"#L{line_start}-L{actual_end}")
        return {"repository_id": self.repository_id, "commit": self.commit, "blob": item["blob"],
                "revision": item["blob"], "path": path, "content": "\n".join(rendered),
                "line_start": line_start, "line_end": actual_end, "total_lines": total,
                "truncated": partial or actual_end < end, "next_line": next_line,
                "reference": reference, "untrusted": True, "partial_line": partial}

    def read(self, path, line_start=1, line_end=None, commit=None, max_chars=6000):
        self._guard()
        if commit is not None and commit != self.commit:
            raise CoreError(409, "只能读取所选仓库当前快照的源码")
        if not self._valid_path(path):
            raise CoreError(400, "源码路径必须是仓库内的相对路径")
        if path not in self.files:
            raise CoreError(404, "源码不在所选仓库的安全跟踪文件范围内")
        self._integer(line_start, "line_start", 1)
        if line_end is not None:
            self._integer(line_end, "line_end", line_start)
        self._integer(max_chars, "max_chars", 1, self.MAX_CHARS)
        result = self._receipt(path, self._blob(self.files[path]), line_start, line_end, max_chars)
        self._guard()
        return result

    def _grep_files(self, terms, paths, deadline):
        found = set()
        batch, size = [], 0
        batches = []
        for path in paths:
            if batch and (len(batch) >= 100 or size + len(path) > 12_000):
                batches.append(batch)
                batch, size = [], 0
            batch.append(path)
            size += len(path)
        if batch:
            batches.append(batch)
        for batch in batches:
            args = ["grep", "--no-textconv", "--no-color", "-I", "-i", "-F", "-l", "-z"]
            for term in terms:
                args.extend(["-e", term])
            args += [self.commit, "--", *(":(literal)" + path for path in batch)]
            raw = self._run_git(args, max_bytes=self.MAX_METADATA_BYTES, allow_no_match=True, deadline=deadline)
            for entry in raw.decode("utf-8", "replace").split("\0"):
                prefix = self.commit + ":"
                if entry.startswith(prefix) and entry[len(prefix):] in self.files:
                    found.add(entry[len(prefix):])
        return found

    def search(self, query, limit=8, offset=0):
        self._guard()
        if (not isinstance(query, str) or not query.strip() or len(query) > 500
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", query)):
            raise CoreError(400, "源码检索词必须是 1–500 个字符")
        self._integer(limit, "limit", 1, 8)
        self._integer(offset, "offset", 0)
        terms = list(dict.fromkeys(query.split()))
        if len(terms) > 8:
            raise CoreError(400, "源码检索每次最多使用 8 个关键词")
        deadline = time.monotonic() + self.SEARCH_TIMEOUT
        path_hits = {path for path in self.files if any(term.casefold() in path.casefold() for term in terms)}
        text_paths = self._grep_files([""], sorted(path_hits), deadline)
        text_paths.update(path for path in path_hits if self.files[path]["size"] == 0)
        content_hits = self._grep_files(terms, sorted(self.files), deadline)
        folded_terms = [term.casefold() for term in terms]
        specific = [term for term in folded_terms if term not in self.COMMON_TERMS]
        weights = {term: (0.1 if specific and term in self.COMMON_TERMS else
                          1 + log(1 + len(self.files) / (1 + sum(term in path.casefold() for path in self.files))))
                   for term in folded_terms}
        specific_hits = (self._grep_files(specific, sorted(self.files), deadline)
                         if specific and len(specific) < len(folded_terms) else content_hits)

        def relevance(path):
            folded = path.casefold()
            stem, filename = PurePosixPath(folded).stem, PurePosixPath(folded).name
            exact = sum(weights[term] for term in folded_terms if term in {folded, filename, stem}
                        or stem == term + "impl")
            path_score = sum(weights[term] for term in folded_terms if term in folded)
            # An explicit class name beats suffix noise. When FooService matches
            # both its interface and FooServiceImpl, expose the implementation first.
            return (-exact, -path_score, path not in specific_hits, not stem.endswith("impl"), path)

        paths = sorted(text_paths | content_hits, key=relevance)
        matches = []
        for path in paths[offset:offset + limit]:
            content = self._blob(self.files[path], deadline)
            lines = self._lines(content)
            scored_lines = [(sum(weights[term] for term in folded_terms if term in line.casefold()), -number)
                            for number, line in enumerate(lines, 1)]
            hit = -max(scored_lines)[1] if scored_lines else 1
            hit_terms = [term for term in folded_terms if term in lines[hit - 1].casefold()] if lines else []
            best_term = max(hit_terms, key=weights.get) if hit_terms else ""
            limit_chars = self.SEARCH_SNIPPET_CHARS
            match = self._receipt(path, content, max(1, hit - 3), min(len(lines), hit + 3), limit_chars)
            if best_term and best_term not in match["content"].casefold():
                # A match may sit deep inside a generated/minified line. Search
                # excerpts must still show the hit; only read() grants evidence.
                line = lines[hit - 1]
                position = line.casefold().find(best_term)
                start = max(0, position - 120)
                prefix = f"{hit}: "
                excerpt = line[start:start + limit_chars - len(prefix) - 2]
                match = self._receipt(path, content, hit, hit, limit_chars)
                match["content"] = prefix + ("…" if start else "") + excerpt + ("…" if start + len(excerpt) < len(line) else "")
                match["partial_line"] = True
                match["truncated"] = True
            # Discovery only locates a range. Provenance receipts are issued by
            # read(), so do not repeatedly spend context on hashes and references.
            match = {key: match[key] for key in ("path", "content", "line_start", "line_end", "total_lines",
                                                "truncated", "partial_line")}
            match["matched_by"] = (["path"] if path in text_paths else []) + (["content"] if path in content_hits else [])
            matches.append(match)
        self._guard()
        more = offset + limit < len(paths)
        return {"repository_id": self.repository_id, "commit": self.commit, "query": query,
                "matches": matches, "total": len(paths), "offset": offset,
                "next_offset": offset + limit if more else None, "truncated": more,
                "eligible_files_count": len(self.files), "untrusted": True,
                "coverage": {"scope": "all_safe_tracked_sources", "searched_files_count": len(self.files), "complete": True}}
