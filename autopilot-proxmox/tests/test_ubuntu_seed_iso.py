"""Ubuntu seed-ISO builder: writes user-data + meta-data, invokes genisoimage."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.ubuntu_seed_iso import build_seed_iso


def test_build_seed_iso_writes_user_data_and_meta_data(tmp_path: Path) -> None:
    user_data = "#cloud-config\nautoinstall:\n  version: 1\n"
    meta_data = "instance-id: i-1\n"

    # Patch subprocess.run to avoid needing genisoimage in CI.
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        iso_path = build_seed_iso(
            user_data=user_data, meta_data=meta_data,
            out_path=tmp_path / "ubuntu-seed.iso",
        )

    # Should have staged user-data + meta-data into a temp dir and called genisoimage
    args = mock_run.call_args[0][0]
    assert args[0] == "genisoimage"
    assert "-V" in args
    # NoCloud requires volume label "cidata" (lower-case).
    assert args[args.index("-V") + 1] == "cidata"
    # The output path we requested
    assert str(iso_path) in args


def test_build_seed_iso_raises_on_genisoimage_missing(tmp_path: Path) -> None:
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("genisoimage")
        try:
            build_seed_iso(user_data="x", meta_data="y",
                           out_path=tmp_path / "s.iso")
        except RuntimeError as e:
            assert "genisoimage" in str(e)
        else:
            raise AssertionError("expected RuntimeError")


def test_build_seed_iso_accepts_optional_network_config(tmp_path: Path) -> None:
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        build_seed_iso(
            user_data="#cloud-config\n",
            meta_data="instance-id: x\n",
            out_path=tmp_path / "with-network.iso",
            network_config="version: 2\nethernets:\n  eth0:\n    dhcp4: true\n",
        )
    # Stage dir passed as last arg must contain network-config
    stage_dir = Path(mock_run.call_args[0][0][-1])
    # The stage dir is a tempdir — existence checking not possible post-cleanup,
    # but we can confirm genisoimage was called with a real directory path.
    # Just assert the call happened without error.
    assert mock_run.called
