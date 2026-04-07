"""Tests for the custom smbios Jinja2 filter plugin."""

import base64
import re
import sys
import os

import pytest

# Add filter_plugins to path so we can import directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "filter_plugins"))
from smbios import FilterModule  # noqa: E402


@pytest.fixture
def filters():
    return FilterModule()


# ── proxmox_smbios1 ──────────────────────────────────────────────────────────


class TestProxmoxSmbios1:
    def test_returns_none_for_empty_dict(self, filters):
        assert filters.proxmox_smbios1({}) is None

    def test_uuid_only(self, filters):
        result = filters.proxmox_smbios1({"uuid": "AAAA-BBBB-CCCC"})
        assert result == "uuid=AAAA-BBBB-CCCC"

    def test_manufacturer_only(self, filters):
        result = filters.proxmox_smbios1({"manufacturer": "Lenovo"})
        expected_b64 = base64.b64encode(b"Lenovo").decode()
        assert result == f"base64=1,manufacturer={expected_b64}"

    def test_all_oem_fields_with_uuid(self, filters):
        fields = {
            "manufacturer": "Lenovo",
            "product": "ThinkPad T14 Gen 4",
            "family": "ThinkPad",
            "serial": "PF-A3B7C1D9",
            "sku": "21HES06600",
            "uuid": "12345678-ABCD-EF01-2345-67890ABCDEF0",
        }
        result = filters.proxmox_smbios1(fields)
        assert result.startswith("base64=1,")
        assert "uuid=12345678-ABCD-EF01-2345-67890ABCDEF0" in result
        # UUID must NOT be base64-encoded
        assert "uuid=MTIz" not in result
        # Verify each OEM field is base64-encoded
        for key in ["manufacturer", "product", "family", "serial", "sku"]:
            encoded = base64.b64encode(fields[key].encode("utf-8")).decode()
            assert f"{key}={encoded}" in result

    def test_raw_smbios1_string(self, filters):
        raw = "base64=1,manufacturer=TGVub3Zv,uuid=OLD-UUID"
        result = filters.proxmox_smbios1(raw)
        assert "uuid=OLD-UUID" not in result
        assert "manufacturer=TGVub3Zv" in result

    def test_returns_none_for_non_dict_non_str(self, filters):
        assert filters.proxmox_smbios1(None) is None
        assert filters.proxmox_smbios1(42) is None


# ── proxmox_disk_serial ───────────────────────────────────────────────────────


class TestProxmoxDiskSerial:
    def test_appends_serial(self, filters):
        config = "local-lvm:64,iothread=1,ssd=1,discard=on"
        result = filters.proxmox_disk_serial(config, "APHV000100ABCDEF01")
        assert result == "local-lvm:64,iothread=1,ssd=1,discard=on,serial=APHV000100ABCDEF01"

    def test_replaces_existing_serial(self, filters):
        config = "local-lvm:64,iothread=1,serial=OLD,ssd=1"
        result = filters.proxmox_disk_serial(config, "NEW")
        assert result == "local-lvm:64,iothread=1,ssd=1,serial=NEW"
        assert "serial=OLD" not in result

    def test_raises_on_empty_config(self, filters):
        with pytest.raises(ValueError):
            filters.proxmox_disk_serial("", "SER")


# ── generate_serial_number ────────────────────────────────────────────────────


class TestGenerateSerialNumber:
    @pytest.mark.parametrize(
        "manufacturer,expected_prefix",
        [
            ("Lenovo", "PF"),
            ("Dell Inc.", "SVC"),
            ("HP", "CZC"),
            ("Microsoft Corporation", "MSF"),
            ("Proxmox", "LAB"),
            ("", "LAB"),
        ],
    )
    def test_prefix_mapping(self, filters, manufacturer, expected_prefix):
        result = filters.generate_serial_number(manufacturer)
        assert result.startswith(f"{expected_prefix}-")
        # 8 hex chars after the dash
        hex_part = result.split("-", 1)[1]
        assert len(hex_part) == 8
        assert re.match(r"^[0-9A-F]+$", hex_part)

    def test_custom_serial_passthrough(self, filters):
        result = filters.generate_serial_number("Lenovo", custom_serial="MY-CUSTOM-123")
        assert result == "MY-CUSTOM-123"

    def test_custom_prefix_overrides_manufacturer(self, filters):
        result = filters.generate_serial_number("Lenovo", prefix="ACME")
        assert result.startswith("ACME-")
        hex_part = result.split("-", 1)[1]
        assert len(hex_part) == 8
        assert re.match(r"^[0-9A-F]+$", hex_part)

    def test_custom_prefix_with_empty_manufacturer(self, filters):
        result = filters.generate_serial_number("", prefix="TEST")
        assert result.startswith("TEST-")

    def test_custom_serial_takes_priority_over_prefix(self, filters):
        result = filters.generate_serial_number("Lenovo", custom_serial="EXACT-123", prefix="ACME")
        assert result == "EXACT-123"

    def test_randomness(self, filters):
        results = {filters.generate_serial_number("Lenovo") for _ in range(10)}
        assert len(results) > 1  # should not all be the same


# ── generate_vm_identity ──────────────────────────────────────────────────────


class TestGenerateVmIdentity:
    def test_structure(self, filters):
        identity = filters.generate_vm_identity(100)
        assert "uuid" in identity
        assert "disk_serial" in identity

    def test_uuid_format(self, filters):
        identity = filters.generate_vm_identity(100)
        # UUID4 uppercase: 8-4-4-4-12
        assert re.match(
            r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$",
            identity["uuid"],
        )

    def test_disk_serial_format(self, filters):
        identity = filters.generate_vm_identity(42)
        serial = identity["disk_serial"]
        assert serial.startswith("APHV")
        # APHV + 6-digit vmid + 10-char uuid prefix = 20 chars
        assert len(serial) == 20
        assert serial[4:10] == "000042"

    def test_vmid_padding(self, filters):
        identity = filters.generate_vm_identity(999999)
        assert identity["disk_serial"][4:10] == "999999"

    def test_uniqueness(self, filters):
        ids = [filters.generate_vm_identity(100) for _ in range(10)]
        uuids = {i["uuid"] for i in ids}
        assert len(uuids) == 10
