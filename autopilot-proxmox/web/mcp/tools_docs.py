from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .registry import ToolRegistry, object_schema


TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".rst",
    ".ps1",
    ".psm1",
    ".sh",
    ".py",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
}
DEFAULT_DOC_ROOTS = [
    "/repo/README.md",
    "/repo/AGENTS.md",
    "/repo/CLAUDE.md",
    "/repo/.github/copilot-instructions.md",
    "/repo/docs",
    "/repo/tools/winpe-build/README.md",
    "/repo/tools/cloudosd-build",
    "/repo/tools/osdeploy-build",
    "/repo/autopilot-proxmox/README.md",
    "/repo/autopilot-proxmox/MIGRATION-microservice-split.md",
    "/repo/autopilot-proxmox/tools/cloudosd-build",
    "/repo/autopilot-proxmox/tools/osdeploy-build",
]


def _roots() -> list[Path]:
    raw = os.environ.get("AUTOPILOT_MCP_DOC_ROOTS", "")
    values = [item.strip() for item in raw.split(",") if item.strip()] or DEFAULT_DOC_ROOTS
    return [Path(value) for value in values]


def _doc_id(path: Path) -> str:
    resolved = path.resolve()
    for prefix, label in ((Path("/repo"), "repo"), (Path("/app"), "app")):
        try:
            return str(Path(label) / resolved.relative_to(prefix))
        except ValueError:
            continue
    return str(resolved)


def _path_for_doc_id(doc_id: str) -> Path:
    if doc_id.startswith("repo/"):
        return Path("/repo") / doc_id.removeprefix("repo/")
    if doc_id.startswith("app/"):
        return Path("/app") / doc_id.removeprefix("app/")
    return Path(doc_id)


def _title(path: Path, content: str | None = None) -> str:
    text = content
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.name
    return path.name


def _iter_docs() -> list[dict[str, Any]]:
    seen: set[Path] = set()
    docs: list[dict[str, Any]] = []
    for root in _roots():
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in paths:
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                stat = path.stat()
            except OSError:
                continue
            docs.append({
                "doc_id": _doc_id(path),
                "title": _title(path),
                "path": str(path),
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })
    return sorted(docs, key=lambda item: item["doc_id"])


def register(registry: ToolRegistry) -> None:
    @registry.register(
        "autopilot_docs.list",
        "List repo documentation indexed for MCP agents.",
        object_schema({"limit": {"type": "integer", "minimum": 1, "default": 100}}),
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def list_docs(args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 100)
        docs = _iter_docs()[:limit]
        return {"count": len(_iter_docs()), "docs": docs}

    @registry.register(
        "autopilot_docs.search",
        "Search indexed repo documentation and return matching snippets.",
        object_schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "default": 10},
            },
            required=["query"],
        ),
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def search_docs(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 10)
        terms = [term.casefold() for term in query.split() if term.strip()]
        results: list[dict[str, Any]] = []
        if not terms:
            return {"query": query, "results": []}
        for doc in _iter_docs():
            path = _path_for_doc_id(doc["doc_id"])
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = content.splitlines(keepends=True)
            snippets = []
            score = 0
            for idx, line in enumerate(lines, start=1):
                folded = line.casefold()
                hits = sum(1 for term in terms if term in folded)
                if hits:
                    score += hits * 10
                    snippets.append({"line": idx, "text": line[:500]})
                    if len(snippets) >= 3:
                        break
            folded_all = content.casefold()
            score += sum(folded_all.count(term) for term in terms)
            if score:
                results.append({
                    "doc_id": doc["doc_id"],
                    "title": _title(path, content),
                    "score": score,
                    "snippets": snippets,
                })
        results.sort(key=lambda item: (-item["score"], item["doc_id"]))
        return {"query": query, "results": results[:limit]}

    @registry.register(
        "autopilot_docs.read",
        "Read an indexed documentation file by doc_id.",
        object_schema(
            {
                "doc_id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1, "default": 12000},
            },
            required=["doc_id"],
        ),
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_doc(args: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(args.get("doc_id") or "")
        max_chars = int(args.get("max_chars") or 12000)
        allowed = {doc["doc_id"] for doc in _iter_docs()}
        if doc_id not in allowed:
            raise ValueError(f"document is not indexed: {doc_id}")
        path = _path_for_doc_id(doc_id)
        content = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > max_chars
        return {
            "doc_id": doc_id,
            "title": _title(path, content),
            "content": content[:max_chars],
            "truncated": truncated,
        }
