"""Build NoCloud seed ISOs (cidata-labelled) for Ubuntu autoinstall + cloud-init."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def build_seed_iso(*, user_data: str, meta_data: str, out_path: Path,
                   network_config: str | None = None) -> Path:
    """Write user-data, meta-data (and optional network-config) to a temp dir,
    then genisoimage -V cidata -> `out_path`. Returns out_path on success."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage"
        stage.mkdir()
        (stage / "user-data").write_text(user_data, encoding="utf-8")
        (stage / "meta-data").write_text(meta_data, encoding="utf-8")
        if network_config is not None:
            (stage / "network-config").write_text(network_config, encoding="utf-8")

        try:
            subprocess.run(
                ["genisoimage", "-quiet", "-o", str(out_path),
                 "-J", "-r", "-V", "cidata", str(stage)],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "genisoimage not installed in container; install `genisoimage`"
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"genisoimage failed: {e.stderr[:300]}"
            ) from e

    return out_path
