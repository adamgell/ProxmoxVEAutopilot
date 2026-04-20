"""Custom Jinja2 filters for Proxmox SMBIOS configuration."""

import base64
import os
import uuid


class FilterModule:
    """Ansible Jinja2 filter plugin for Proxmox SMBIOS operations."""

    def filters(self):
        return {
            "proxmox_smbios1": self.proxmox_smbios1,
            "proxmox_disk_serial": self.proxmox_disk_serial,
            "generate_serial_number": self.generate_serial_number,
            "generate_vm_identity": self.generate_vm_identity,
            "build_smbios_bin_b64": self.build_smbios_bin_b64,
        }

    @staticmethod
    def build_smbios_bin_b64(oem_profile, *, serial, uuid_str, chassis_type=None):
        """Build a per-VM SMBIOS file (Type 0 + Type 1 + Type 3) and
        return base64 of the bytes — Ansible writes the decoded bytes
        out to a file, scps to the Proxmox host, references via
        ``-smbios file=<path>`` in args.

        Why a single file: QEMU CLI doesn't expose Type 3 chassis_type
        (upstream issue #2769 still open in QEMU 10.1.2), and combining
        ``-smbios type=1,...`` + ``-smbios file=<type-3-only>`` empirically
        drops Proxmox's Type 1 — Windows reports BOCHS_ / BXPC____ for
        Manufacturer/Model and an empty BIOS serial. By bundling Type
        0/1/3 in one file, the file owns those structures cleanly and
        QEMU still auto-generates Type 2/4/etc.

        ``oem_profile`` is the dict from oem_profiles.yml (manufacturer,
        product, family, sku, plus optional chassis_type — overridden by
        the explicit ``chassis_type`` keyword if passed).
        """
        # Late import so the filter plugin loader doesn't bail when
        # web/* isn't on sys.path during test collection.
        import sys
        # Add the autopilot-proxmox dir so `from web.smbios_builder import...`
        # works when this filter runs from inside the container's Ansible.
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from web.smbios_builder import build_full_smbios

        if not isinstance(oem_profile, dict):
            oem_profile = {}
        ct = chassis_type or oem_profile.get("chassis_type")
        if not ct:
            # Caller asked for an SMBIOS file but no chassis_type
            # contributor exists. Default to 3 (Desktop) so the file
            # is still well-formed.
            ct = 3

        bytes_ = build_full_smbios(
            manufacturer=oem_profile.get("manufacturer") or "Generic",
            product_name=oem_profile.get("product") or "Generic Workstation",
            family=oem_profile.get("family") or "",
            sku=oem_profile.get("sku") or "",
            version=oem_profile.get("version") or "",
            serial_number=serial or "0",
            uuid_str=uuid_str,
            chassis_type=int(ct),
        )
        return base64.b64encode(bytes_).decode("ascii")

    @staticmethod
    def proxmox_smbios1(fields):
        """Build a Proxmox smbios1 config string from OEM fields.

        Accepts either:
          - A dict with keys: manufacturer, product, family, serial, sku, uuid
            OEM text fields are base64-encoded; uuid is NOT base64-encoded.
          - A raw smbios1 string (str) — existing uuid= segment is stripped
            and a new one appended from fields['uuid'] if present.

        Returns the comma-delimited smbios1 string, or None if no fields.
        """
        if isinstance(fields, str):
            # Raw smbios1 string mode — strip old uuid, append new
            parts = [p for p in fields.split(",") if p and not p.startswith("uuid=")]
            new_uuid = None  # no way to pass uuid separately in raw mode
            if parts or new_uuid:
                return ",".join(parts)
            return fields

        if not isinstance(fields, dict):
            return None

        manufacturer = fields.get("manufacturer", "")
        product = fields.get("product", "")
        family = fields.get("family", "")
        serial = fields.get("serial", "")
        sku = fields.get("sku", "")
        vm_uuid = fields.get("uuid", "")

        has_oem = any([manufacturer, product, family, serial, sku])
        if not has_oem and not vm_uuid:
            return None

        parts = []
        if has_oem:
            parts.append("base64=1")
            for key, value in [
                ("manufacturer", manufacturer),
                ("product", product),
                ("family", family),
                ("serial", serial),
                ("sku", sku),
            ]:
                if value:
                    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
                    parts.append(f"{key}={encoded}")

        if vm_uuid:
            parts.append(f"uuid={vm_uuid}")

        return ",".join(parts)

    @staticmethod
    def proxmox_disk_serial(disk_config, serial):
        """Add or replace the serial= property on a Proxmox disk config string.

        Splits the comma-delimited disk config, removes any existing serial=
        segment, and appends serial=<value>.
        """
        parts = [p for p in disk_config.split(",") if p and not p.startswith("serial=")]
        if not parts:
            raise ValueError(
                f"Disk config '{disk_config}' could not be parsed for serial injection."
            )
        parts.append(f"serial={serial}")
        return ",".join(parts)

    @staticmethod
    def generate_serial_number(manufacturer, custom_serial=None, prefix=None):
        """Generate a prefixed serial number.

        If custom_serial is provided, returns it verbatim.
        If prefix is provided, uses it instead of the manufacturer mapping.

        Default prefix mapping (when prefix is not set):
          Lenovo      -> PF
          Dell*       -> SVC
          HP          -> CZC
          Microsoft*  -> MSF
          default     -> LAB
        """
        # If custom_serial looks like a prefix (ends in '-') treat it as a
        # prefix instead of returning it verbatim. Otherwise every VM would
        # get the same invalid name — and Proxmox rejects names ending in
        # a hyphen as invalid DNS names anyway.
        if custom_serial and not custom_serial.endswith("-"):
            return custom_serial
        if custom_serial and not prefix:
            prefix = custom_serial

        random_bytes = os.urandom(4)
        hex_str = random_bytes.hex().upper()

        if prefix:
            # Strip trailing hyphens so a user-supplied prefix like "Gell-"
            # doesn't produce "Gell--HEX" — Proxmox's clone API rejects
            # names with consecutive hyphens via its DNS-name validator.
            clean_prefix = prefix.rstrip("-")
            if not clean_prefix:
                clean_prefix = "VM"
            return f"{clean_prefix}-{hex_str}"

        manufacturer = manufacturer or ""
        if manufacturer.startswith("Lenovo"):
            prefix = "PF"
        elif manufacturer.startswith("Dell"):
            prefix = "SVC"
        elif manufacturer.startswith("HP"):
            prefix = "CZC"
        elif manufacturer.startswith("Microsoft"):
            prefix = "MSF"
        else:
            prefix = "LAB"

        return f"{prefix}-{hex_str}"

    @staticmethod
    def generate_vm_identity(vmid):
        """Generate a UUID and disk serial for a new VM.

        Returns a dict with:
          uuid: uppercase UUID4 string
          disk_serial: APHV{vmid:06d}{uuid_hex_prefix:10}
        """
        vmid = int(vmid)
        vm_uuid = str(uuid.uuid4()).upper()
        uuid_hex = vm_uuid.replace("-", "")
        disk_serial = f"APHV{vmid:06d}{uuid_hex[:10]}"
        return {"uuid": vm_uuid, "disk_serial": disk_serial}
