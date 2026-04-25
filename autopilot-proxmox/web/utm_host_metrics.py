"""macOS host metrics collectors for the UTM dashboard.

All public functions return an empty dict on any subprocess error — they
never raise. Each uses a 5-second timeout per subprocess call.

macOS version notes
-------------------
- ``vm_stat`` output format is identical on Intel and Apple Silicon.
- ``sysctl hw.memsize`` returns total physical RAM in bytes on both
  architectures (the sysctl name changed from ``hw.physmem`` in macOS 10.6+).
- ``sysctl -n vm.loadavg`` returns "{ 1m 5m 15m }" on macOS; Linux exposes
  this via /proc/loadavg instead.
- ``sysctl kern.boottime`` prints the struct-timeval as a human string, e.g.
  ``kern.boottime: { sec = 1717000000, usec = 123456 } ...``. We parse the
  ``sec =`` field only.
- ``df -k`` on macOS includes extra inode columns (``iused``, ``ifree``,
  ``%iused``) between Capacity and Mounted-on. The parser locates the
  Capacity ``%`` field dynamically rather than assuming a fixed column index.
- ``memory_pressure`` is a macOS binary (available since 10.9) but may be
  absent in minimal / server installs. When absent we derive pressure from
  the ratio of (wired + active) pages to total pages.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

_TIMEOUT = 5  # seconds per subprocess call


def _run(args: list[str]) -> str:
    """Run a command, return stdout string. Returns '' on any error."""
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def host_cpu() -> dict:
    # sysctl hw.ncpu → logical core count; vm.loadavg → "{ 1m 5m 15m }"
    ncpu_raw = _run(["sysctl", "-n", "hw.ncpu"])
    loadavg_raw = _run(["sysctl", "-n", "vm.loadavg"])
    try:
        cores = int(ncpu_raw.strip())
    except (ValueError, AttributeError):
        return {}
    try:
        # Format: "{ 0.72 0.65 0.59 }"
        nums = re.findall(r"\d+\.\d+|\d+", loadavg_raw)
        load_1m = float(nums[0])
        load_5m = float(nums[1])
        load_15m = float(nums[2])
    except (IndexError, ValueError):
        return {}
    return {
        "cores": cores,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
    }


def host_memory() -> dict:
    # vm_stat gives 4 KB page counts; sysctl hw.memsize gives total bytes.
    memsize_raw = _run(["sysctl", "-n", "hw.memsize"])
    vmstat_raw = _run(["vm_stat"])
    try:
        total_bytes = int(memsize_raw.strip())
    except (ValueError, AttributeError):
        return {}
    if not vmstat_raw:
        return {}

    page_size = 4096  # macOS always uses 4 KB VM pages
    # Try to extract the actual page size from the vm_stat header line
    # e.g. "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
    m = re.search(r"page size of (\d+) bytes", vmstat_raw)
    if m:
        page_size = int(m.group(1))

    def _pages(label: str) -> int:
        m2 = re.search(r"Pages\s+" + re.escape(label) + r"[^:]*:\s+(\d+)", vmstat_raw)
        return int(m2.group(1)) if m2 else 0

    free_pages = _pages("free")
    active_pages = _pages("active")
    inactive_pages = _pages("inactive")
    speculative_pages = _pages("speculative")
    wired_pages = _pages("wired down")

    # "available" = free + inactive + speculative (not committed to active use)
    available_pages = free_pages + inactive_pages + speculative_pages
    used_pages = total_bytes // page_size - available_pages
    used_bytes = max(0, used_pages * page_size)
    free_bytes = available_pages * page_size

    # Derive memory pressure
    pressure = _memory_pressure(
        total_bytes=total_bytes,
        wired_pages=wired_pages,
        active_pages=active_pages,
        page_size=page_size,
    )

    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "pressure": pressure,
    }


def _memory_pressure(
    *, total_bytes: int, wired_pages: int, active_pages: int, page_size: int
) -> str:
    """Return 'normal' | 'warning' | 'critical'.

    Tries the ``memory_pressure`` macOS binary first (exit code 0 = normal,
    1 = warning, 2 = critical). Falls back to a simple ratio: if
    (wired + active) / total > 0.85 → critical; > 0.65 → warning; else normal.
    """
    try:
        r = subprocess.run(
            ["memory_pressure"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        text = (r.stdout + r.stderr).lower()
        if "critical" in text:
            return "critical"
        if "warn" in text:
            return "warning"
        # If binary ran successfully and said nothing alarming, it's normal.
        if r.returncode == 0:
            return "normal"
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass

    # Fallback: committed-memory ratio
    if total_bytes <= 0:
        return "normal"
    committed = (wired_pages + active_pages) * page_size
    ratio = committed / total_bytes
    if ratio > 0.85:
        return "critical"
    if ratio > 0.65:
        return "warning"
    return "normal"


def disk_usage(path: str) -> dict:
    # `df -k <path>` → 1 K-block units; we convert to bytes.
    # macOS df -k has extra inode columns between Capacity and Mounted-on:
    #   Filesystem 1K-blocks Used Available Capacity iused ifree %iused Mounted-on
    # We locate the Capacity column by searching for a field ending in '%'.
    out = _run(["df", "-k", str(path)])
    if not out:
        return {}
    lines = out.strip().splitlines()
    # Last data line is the target filesystem
    for line in reversed(lines):
        parts = line.split()
        # Need at least: Filesystem, 1K-blocks, Used, Available, Capacity%, Mount
        if len(parts) < 6:
            continue
        try:
            total_bytes = int(parts[1]) * 1024
            used_bytes = int(parts[2]) * 1024
            available_bytes = int(parts[3]) * 1024
            # Find the capacity % field — first field ending with '%'
            cap_idx = None
            for i, p in enumerate(parts[4:], start=4):
                if p.endswith("%"):
                    cap_idx = i
                    break
            if cap_idx is None:
                continue
            pct_used = float(parts[cap_idx].rstrip("%"))
            mount = parts[-1]  # mount point is always the last field
            return {
                "mount": mount,
                "total_bytes": total_bytes,
                "used_bytes": used_bytes,
                "available_bytes": available_bytes,
                "pct_used": pct_used,
            }
        except (ValueError, IndexError):
            continue
    return {}


def host_uptime_seconds() -> int:
    # sysctl kern.boottime → "kern.boottime: { sec = 1717000000, usec = 123456 } ..."
    raw = _run(["sysctl", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", raw)
    if not m:
        return 0
    try:
        boot_epoch = int(m.group(1))
        return max(0, int(time.time()) - boot_epoch)
    except (ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 15.0  # host metrics don't change fast

_cache: dict = {}
_cache_ts: float = 0.0


def get_cached_host_summary(utm_documents_dir: str) -> dict:
    """Return {cpu, memory, disk_documents, disk_root, uptime_s, fetched_at}.

    Re-runs the collectors when the cache is older than _CACHE_TTL_SECONDS.
    The ``utm_documents_dir`` path is used for the documents-volume disk check;
    the root disk is always measured at '/'.
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache

    docs_path = str(Path(utm_documents_dir).expanduser()) if utm_documents_dir else "/"
    result = {
        "cpu": host_cpu(),
        "memory": host_memory(),
        "disk_documents": disk_usage(docs_path),
        "disk_root": disk_usage("/"),
        "uptime_s": host_uptime_seconds(),
        "fetched_at": time.time(),
    }
    _cache = result
    _cache_ts = now
    return result
