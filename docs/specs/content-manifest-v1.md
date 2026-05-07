# Content Manifest v1

Content Manifest v1 is the storage-agnostic contract for deployable content
that agents can stage or execute during provisioning. The pure model lives in
`autopilot-proxmox/web/content_manifest.py`; database rows and HTTP endpoints
should validate through that module before persisting or returning a manifest.

## Manifest Shape

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "qemu-guest-agent",
      "kind": "package",
      "name": "QEMU Guest Agent",
      "version": "107.0",
      "source_uri": "https://content.local/qga-107.msi",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "size_bytes": 1048576,
      "architecture": "x64",
      "target_os": "windows",
      "reboot_behavior": "none",
      "conditions": {
        "phase": "full_os"
      },
      "metadata": {
        "install_command": "msiexec.exe /i {path} /qn"
      }
    }
  ]
}
```

## Validation Contract

- `schema_version` must be `1`.
- `kind` must be one of `app`, `package`, `script`, `driver`, `os_image`.
- `sha256` must be 64 hexadecimal characters and is normalized to lowercase.
- `size_bytes` must be a non-negative integer.
- `source_uri` must be a non-empty URI with a scheme.
- `reboot_behavior` must be one of `none`, `optional`, `required`, `deferred`.
- `conditions` and `metadata` must be JSON objects.
- `manifest_digest()` returns a SHA-256 hash of canonical JSON with sorted
  mapping keys and compact separators.

## Later Integration Point

The existing OSD v2 content endpoints and Postgres content tables are the
natural integration point. A later endpoint/database change should assemble a
Content Manifest v1 dict from content item/version rows, validate it with
`validate_manifest()`, and return `manifest_digest()` alongside the manifest for
agent cache and integrity checks.
