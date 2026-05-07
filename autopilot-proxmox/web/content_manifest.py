"""Storage-agnostic Content Manifest v1 model and validator."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 1
CONTENT_KINDS = frozenset({"app", "package", "script", "driver", "os_image"})
REBOOT_BEHAVIORS = frozenset({"none", "optional", "required", "deferred"})

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


class ContentManifestValidationError(ValueError):
    """Raised when a Content Manifest v1 payload is not valid."""


@dataclass(frozen=True)
class ContentManifestItem:
    id: str
    kind: str
    name: str
    version: str
    source_uri: str
    sha256: str
    size_bytes: int
    architecture: str
    target_os: str
    reboot_behavior: str = "none"
    conditions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "ContentManifestItem":
        context = f"items[{index}]"
        return cls(
            id=_required_string(raw, "id", context),
            kind=_content_kind(raw, context),
            name=_required_string(raw, "name", context),
            version=_required_string(raw, "version", context),
            source_uri=_source_uri(raw, context),
            sha256=_sha256(raw, context),
            size_bytes=_size_bytes(raw, context),
            architecture=_required_string(raw, "architecture", context),
            target_os=_required_string(raw, "target_os", context),
            reboot_behavior=_reboot_behavior(raw, context),
            conditions=_json_object(raw, "conditions", context),
            metadata=_json_object(raw, "metadata", context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "source_uri": self.source_uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "architecture": self.architecture,
            "target_os": self.target_os,
            "reboot_behavior": self.reboot_behavior,
            "conditions": _stable_json_value(self.conditions),
            "metadata": _stable_json_value(self.metadata),
        }


@dataclass(frozen=True)
class ContentManifest:
    schema_version: int = SCHEMA_VERSION
    items: list[ContentManifestItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ContentManifest":
        if not isinstance(raw, dict):
            raise ContentManifestValidationError("manifest must be an object")
        schema_version = raw.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ContentManifestValidationError(
                f"schema_version must be {SCHEMA_VERSION}"
            )
        items = raw.get("items")
        if not isinstance(items, list):
            raise ContentManifestValidationError("items must be a list")
        return cls(
            schema_version=SCHEMA_VERSION,
            items=[
                ContentManifestItem.from_dict(_item_object(item, index), index)
                for index, item in enumerate(items)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "items": [item.to_dict() for item in self.items],
        }


def validate_manifest(raw: ContentManifest | dict[str, Any]) -> ContentManifest:
    """Validate and normalize a Content Manifest v1 payload."""
    if isinstance(raw, ContentManifest):
        return ContentManifest.from_dict(raw.to_dict())
    return ContentManifest.from_dict(raw)


def manifest_digest(raw: ContentManifest | dict[str, Any]) -> str:
    """Return the deterministic SHA-256 digest for a validated manifest."""
    manifest = validate_manifest(raw)
    canonical = json.dumps(
        manifest.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item_object(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContentManifestValidationError(f"items[{index}] must be an object")
    return raw


def _required_string(raw: dict[str, Any], field_name: str, context: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ContentManifestValidationError(
            f"{context}.{field_name} must be a non-empty string"
        )
    return value


def _content_kind(raw: dict[str, Any], context: str) -> str:
    value = _required_string(raw, "kind", context)
    if value not in CONTENT_KINDS:
        allowed = ", ".join(sorted(CONTENT_KINDS))
        raise ContentManifestValidationError(
            f"{context}.kind must be one of: {allowed}"
        )
    return value


def _source_uri(raw: dict[str, Any], context: str) -> str:
    value = _required_string(raw, "source_uri", context)
    if not urlparse(value).scheme:
        raise ContentManifestValidationError(
            f"{context}.source_uri must include a URI scheme"
        )
    return value


def _sha256(raw: dict[str, Any], context: str) -> str:
    value = _required_string(raw, "sha256", context)
    if not _SHA256_RE.fullmatch(value):
        raise ContentManifestValidationError(
            f"{context}.sha256 must be 64 hexadecimal characters"
        )
    return value.lower()


def _size_bytes(raw: dict[str, Any], context: str) -> int:
    value = raw.get("size_bytes")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContentManifestValidationError(
            f"{context}.size_bytes must be a non-negative integer"
        )
    return value


def _reboot_behavior(raw: dict[str, Any], context: str) -> str:
    value = raw.get("reboot_behavior", "none")
    if not isinstance(value, str) or value not in REBOOT_BEHAVIORS:
        allowed = ", ".join(sorted(REBOOT_BEHAVIORS))
        raise ContentManifestValidationError(
            f"{context}.reboot_behavior must be one of: {allowed}"
        )
    return value


def _json_object(
    raw: dict[str, Any],
    field_name: str,
    context: str,
) -> dict[str, Any]:
    value = raw.get(field_name, {})
    if not isinstance(value, dict):
        raise ContentManifestValidationError(f"{context}.{field_name} must be an object")
    try:
        stable_value = _stable_json_value(value)
    except (TypeError, ValueError) as exc:
        raise ContentManifestValidationError(
            f"{context}.{field_name} must be JSON-serializable"
        ) from exc
    if not isinstance(stable_value, dict):
        raise ContentManifestValidationError(f"{context}.{field_name} must be an object")
    return stable_value


def _stable_json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True))
