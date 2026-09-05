use chrono::{DateTime, Utc};
use pve_port::*;
use serde_json::{Value, json};

fn time() -> DateTime<Utc> {
    "2026-09-05T12:00:00Z".parse().unwrap()
}
fn node() -> NodeName {
    NodeName::parse("pve-test").unwrap()
}
fn template() -> Value {
    json!({"node":"pve-test","vmid":900,"digest":"digest-1","name":"blank-template",
        "cores":2,"memory":2048,"scsi0":"local-lvm:vm-900-disk-0,size=80G",
        "smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0",
        "bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=0,type=virtio",
        "boot":"order=scsi0","template":1})
}
fn destination() -> Value {
    json!({"node":"pve-test","vmid":101,"digest":"digest-2","name":"deploy-101",
        "cores":4,"memory":4096,"scsi0":"local-lvm:vm-101-disk-0,size=120G,serial=DISK-101",
        "smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3302,serial=SYS-101",
        "net0":"virtio=02:00:00:00:01:01,bridge=vmbr0",
        "bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=1,type=virtio",
        "boot":"order=ide2;scsi0","template":0,
        "ide2":"local:iso/deployment.iso,media=cdrom","ide3":"local:iso/virtio.iso,media=cdrom"})
}
fn parse(data: Value) -> Result<ProvisioningVmConfigV1, PveReadError> {
    ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(900).unwrap(),
        NativeEvidenceSource::PveApi,
        data,
        time(),
    )
}

#[test]
fn rich_template_and_destination_preserve_every_sanitized_field() {
    let snapshot = parse(template()).unwrap();
    assert_eq!(snapshot.node(), &node());
    assert_eq!(snapshot.vmid(), Vmid::new(900).unwrap());
    assert_eq!(snapshot.source(), NativeEvidenceSource::PveApi);
    assert_eq!(snapshot.digest(), "digest-1");
    assert_eq!(snapshot.name().as_str(), "blank-template");
    assert_eq!((snapshot.cores(), snapshot.memory_mib()), (2, 2048));
    assert_eq!(snapshot.primary_disk().storage().as_str(), "local-lvm");
    assert_eq!(snapshot.primary_disk().volume(), "vm-900-disk-0");
    assert_eq!(snapshot.primary_disk().capacity_bytes(), 85_899_345_920);
    assert_eq!(snapshot.primary_disk().serial(), None);
    assert_eq!(snapshot.system_serial(), None);
    assert_eq!(
        snapshot.uuid(),
        VmUuid::parse("3f2504e0-4f89-41d3-9a0c-0305e82c3301").unwrap()
    );
    assert_eq!(snapshot.mac().as_str(), "02:00:00:00:09:00");
    assert_eq!(snapshot.bridge().as_str(), "vmbr0");
    assert_eq!(snapshot.firmware(), ProvisioningFirmwareV1::Seabios);
    assert_eq!(snapshot.cpu(), ProvisioningCpuV1::Host);
    assert_eq!(snapshot.qga_channel(), ProvisioningQgaChannelV1::Virtio);
    assert!(!snapshot.qga_enabled());
    assert_eq!(snapshot.balloon_mib(), 0);
    assert_eq!(
        snapshot.boot_profile(),
        Some(ProvisioningBootProfile::InstalledDisk)
    );
    assert_eq!(snapshot.deployment_iso(), &ProvisioningMediaSlotV1::Absent);
    assert_eq!(snapshot.driver_iso(), &ProvisioningMediaSlotV1::Absent);
    assert!(snapshot.is_template());
    assert!(!snapshot.locked());
    assert!(snapshot.unsupported().is_empty());
    assert_eq!(snapshot.fake_clone_provenance(), None);
    assert_eq!(snapshot.observed_at(), time());

    let snapshot = ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(101).unwrap(),
        NativeEvidenceSource::PveApi,
        destination(),
        time(),
    )
    .unwrap();
    assert_eq!(snapshot.primary_disk().capacity_bytes(), 128_849_018_880);
    assert_eq!(snapshot.primary_disk().serial(), Some("DISK-101"));
    assert_eq!(snapshot.system_serial(), Some("SYS-101"));
    assert_eq!(
        snapshot.boot_profile(),
        Some(ProvisioningBootProfile::PeMedia)
    );
    assert_eq!(
        snapshot.deployment_iso().volid(),
        Some("local:iso/deployment.iso")
    );
    assert_eq!(snapshot.driver_iso().volid(), Some("local:iso/virtio.iso"));
    assert!(!snapshot.is_template());
    assert!(snapshot.qga_enabled());
    assert!(snapshot.unsupported().is_empty());
    assert!(snapshot.template_fingerprint().is_err());
}

#[test]
fn old_native_parser_keeps_rejecting_rich_layout() {
    let old =
        NativeVmConfig::from_wire(node(), Vmid::new(101).unwrap(), destination(), time()).unwrap();
    for reason in [
        UnsupportedConfig::UnknownField,
        UnsupportedConfig::SmbiosProperties,
        UnsupportedConfig::DiskProperties,
        UnsupportedConfig::AdditionalDisk,
        UnsupportedConfig::BootOrder,
    ] {
        assert!(old.unsupported().contains(&reason), "{reason:?}");
    }
}

#[test]
fn capacity_is_exact_positive_checked_binary_bytes() {
    for (raw, expected) in [
        ("1", 1),
        ("17K", 17_408),
        ("2M", 2_097_152),
        ("80G", 85_899_345_920),
        ("2T", 2_199_023_255_552),
        ("18446744073709551615", u64::MAX),
        ("171798691857", 171_798_691_857),
    ] {
        let mut data = template();
        data["scsi0"] = json!(format!("local-lvm:vm-900-disk-0,size={raw}"));
        assert_eq!(
            parse(data).unwrap().primary_disk().capacity_bytes(),
            expected
        );
    }
    for raw in [
        "0",
        "0G",
        "-1G",
        "+1G",
        "1.5G",
        "1e3",
        " 1G",
        "1G ",
        "1g",
        "1B",
        "1P",
        "G",
        "",
        "18446744073709551616",
        "18446744073709551615K",
    ] {
        let mut data = template();
        data["scsi0"] = json!(format!("local-lvm:vm-900-disk-0,size={raw}"));
        assert_eq!(parse(data), Err(PveReadError::InvalidResponse), "{raw}");
    }
    for raw in [
        "local-lvm:vm-900-disk-0",
        "local-lvm:folder/disk,size=80G",
        "local-lvm:,size=80G",
        "local-lvm:vm-900-disk-0,size=80G,serial=bad,secret",
        "local-lvm:vm-900-disk-0,size=80G,serial=bad_serial_",
    ] {
        let mut data = template();
        data["scsi0"] = json!(raw);
        assert_eq!(parse(data), Err(PveReadError::InvalidResponse));
    }
}

#[test]
fn explicit_core_fields_types_and_binding_cannot_be_defaulted() {
    for key in [
        "digest", "name", "cores", "memory", "scsi0", "smbios1", "net0", "bios", "cpu", "balloon",
        "agent", "boot", "template",
    ] {
        let mut data = template();
        data.as_object_mut().unwrap().remove(key);
        assert_eq!(
            parse(data),
            Err(PveReadError::InvalidResponse),
            "missing {key}"
        );
        for bad in [Value::Null, json!([]), json!({}), json!(true)] {
            let mut data = template();
            data[key] = bad;
            assert_eq!(
                parse(data),
                Err(PveReadError::InvalidResponse),
                "type {key}"
            );
        }
    }
    for (key, value) in [
        ("node", json!("other")),
        ("vmid", json!(901)),
        ("vmid", json!("900")),
        ("cores", json!(0)),
        ("cores", json!(129)),
        ("cores", json!(1.0)),
        ("memory", json!(511)),
        ("memory", json!(513)),
        ("memory", json!(1_048_704)),
        ("balloon", json!(-1)),
        ("template", json!(2)),
        ("lock", json!("")),
        ("lock", json!("bad secret")),
        ("digest", json!("bad secret")),
        ("bios", json!("")),
        ("cpu", json!("")),
        ("agent", json!(1)),
        ("agent", json!("enabled=1")),
        ("agent", json!("type=virtio")),
        ("agent", json!("enabled=2,type=virtio")),
    ] {
        let mut data = template();
        data[key] = value;
        assert_eq!(parse(data), Err(PveReadError::InvalidResponse), "{key}");
    }
}

#[test]
fn comma_syntax_duplicates_fail_before_unsupported_projection() {
    for (key, value) in [
        ("scsi0", "local-lvm:vm-900-disk-0,size=80G,size=80G"),
        (
            "scsi0",
            "local-lvm:vm-900-disk-0,size=80G,secret=x,secret=y",
        ),
        (
            "smbios1",
            "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,serial=A,serial=B",
        ),
        ("net0", "virtio=02:00:00:00:09:00,bridge=vmbr0,bridge=vmbr0"),
        ("agent", "enabled=1,type=virtio,type=virtio"),
        ("boot", "order=scsi0,order=scsi0"),
        ("ide2", "local:iso/a.iso,media=cdrom,media=cdrom"),
        ("ide3", "none,media=cdrom,secret=x,secret=y"),
        ("net1", "virtio=02:00:00:00:01:02,bridge=vmbr0,bridge=vmbr0"),
        ("scsi1", "local-lvm:other,size=1G,size=1G"),
        ("efidisk0", "local-lvm:efi,size=1G,size=1G"),
        ("net1", "virtio=02:00:00:00:01:02,broken"),
        ("scsi1", "local-lvm:other,broken"),
        ("boot", "broken"),
        ("agent", "enabled=1,type=virtio,"),
        ("smbios1", "uuid=,secret=x"),
    ] {
        let mut data = template();
        data[key] = json!(value);
        assert_eq!(
            parse(data),
            Err(PveReadError::InvalidResponse),
            "{key}: {value}"
        );
    }
}

#[test]
fn unsupported_values_are_fixed_payload_free_and_block_template_admission() {
    for (key, value, reason) in [
        ("bios", "ovmf-secret", ProvisioningUnsupportedV1::Firmware),
        ("cpu", "custom-secret", ProvisioningUnsupportedV1::Cpu),
        (
            "agent",
            "enabled=1,type=secret",
            ProvisioningUnsupportedV1::QgaChannel,
        ),
        (
            "agent",
            "enabled=1,type=virtio,secret=value",
            ProvisioningUnsupportedV1::AgentProperties,
        ),
        ("boot", "order=secret", ProvisioningUnsupportedV1::BootOrder),
        (
            "scsi0",
            "local-lvm:vm-900-disk-0,size=80G,secret=value",
            ProvisioningUnsupportedV1::DiskProperties,
        ),
        (
            "smbios1",
            "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,secret=value",
            ProvisioningUnsupportedV1::SmbiosProperties,
        ),
        (
            "net0",
            "virtio=02:00:00:00:09:00,bridge=vmbr0,secret=value",
            ProvisioningUnsupportedV1::NicProperties,
        ),
        (
            "net1",
            "virtio=02:00:00:00:01:02,bridge=vmbr0,secret=value",
            ProvisioningUnsupportedV1::AdditionalNic,
        ),
        (
            "scsi1",
            "local-lvm:secret,size=1G",
            ProvisioningUnsupportedV1::AdditionalDisk,
        ),
        (
            "efidisk0",
            "local-lvm:secret,size=1G",
            ProvisioningUnsupportedV1::EfiDisk,
        ),
        (
            "tpmstate0",
            "local-lvm:secret,size=1G",
            ProvisioningUnsupportedV1::TpmState,
        ),
        (
            "unused0",
            "local-lvm:secret",
            ProvisioningUnsupportedV1::UnusedDisk,
        ),
        ("oem", "secret", ProvisioningUnsupportedV1::Oem),
        ("args", "secret", ProvisioningUnsupportedV1::Args),
        (
            "unknown-secret",
            "secret",
            ProvisioningUnsupportedV1::UnknownField,
        ),
        (
            "ide2",
            "none,media=cdrom",
            ProvisioningUnsupportedV1::DeploymentMedia,
        ),
        (
            "ide3",
            "local:iso/a.iso,media=cdrom,secret=value",
            ProvisioningUnsupportedV1::DriverMedia,
        ),
    ] {
        let mut data = template();
        data[key] = json!(value);
        let snapshot = parse(data).unwrap();
        assert!(snapshot.unsupported().contains(&reason), "{key}");
        assert!(snapshot.template_fingerprint().is_err(), "{key}");
        let encoded = serde_json::to_string(&snapshot).unwrap();
        assert!(!encoded.contains("secret"), "{key}: {encoded}");
        assert_eq!(
            serde_json::from_str::<ProvisioningVmConfigV1>(&encoded).unwrap(),
            snapshot
        );
    }
    let mut data = template();
    data["balloon"] = json!(128);
    let snapshot = parse(data).unwrap();
    assert_eq!(snapshot.balloon_mib(), 128);
    assert!(
        snapshot
            .unsupported()
            .contains(&ProvisioningUnsupportedV1::Balloon)
    );
}

#[test]
fn template_semantic_hash_has_independent_golden_and_exact_field_binding() {
    // Independently sorted literal JSON, hashed with shasum -a 256; no production
    // fingerprint helper supplies this expected digest or its semantic fields.
    let golden = json!({"contract_version":1,"node":"pve-test","vmid":900,"template":true,
        "name":"blank-template","cores":2,"memory_mib":2048,
        "primary_disk":{"slot":"scsi0","storage":"local-lvm","volume":"vm-900-disk-0","capacity_bytes":85899345920_u64,"serial":null},
        "uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:09:00","bridge":"vmbr0","system_serial":null,
        "firmware":"seabios","cpu":"host","balloon_mib":0,"qga_enabled":false,"qga_channel":"virtio",
        "boot_profile":"installed_disk","deployment_iso":null,"driver_iso":null,
        "template_device_policy":"reject_extra_devices","evidence_level":"observed_configuration"});
    const HASH: &str = "a92f7aa3d5d90f777ae14d467760b3187717e647272a7ec06438dd2066a4ac20";
    assert_eq!(event_journal::payload_digest(&golden).unwrap(), HASH);
    assert_eq!(
        parse(template()).unwrap().template_fingerprint().unwrap(),
        HASH
    );
    for (key, value) in [
        ("name", "other-template"),
        ("scsi0", "local-lvm:other,size=80G"),
        ("scsi0", "other:vm-900-disk-0,size=80G"),
        ("scsi0", "local-lvm:vm-900-disk-0,size=85899345921"),
        ("scsi0", "local-lvm:vm-900-disk-0,size=80G,serial=DISK-900"),
        ("smbios1", "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3302"),
        (
            "smbios1",
            "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,serial=SYS-900",
        ),
        ("net0", "virtio=02:00:00:00:09:01,bridge=vmbr0"),
        ("net0", "virtio=02:00:00:00:09:00,bridge=vmbr1"),
        ("agent", "enabled=1,type=virtio"),
    ] {
        let mut data = template();
        data[key] = json!(value);
        assert_ne!(
            parse(data).unwrap().template_fingerprint().unwrap(),
            HASH,
            "{key}/{value}"
        );
    }
    for (key, value) in [("cores", 4), ("memory", 4096)] {
        let mut data = template();
        data[key] = json!(value);
        assert_ne!(parse(data).unwrap().template_fingerprint().unwrap(), HASH);
    }
    let mut data = template();
    data["node"] = json!("other");
    let changed = ProvisioningVmConfigV1::from_wire(
        NodeName::parse("other").unwrap(),
        Vmid::new(900).unwrap(),
        NativeEvidenceSource::PveApi,
        data,
        time(),
    )
    .unwrap();
    assert_ne!(changed.template_fingerprint().unwrap(), HASH);
    let mut data = template();
    data["vmid"] = json!(901);
    let changed = ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(901).unwrap(),
        NativeEvidenceSource::PveApi,
        data,
        time(),
    )
    .unwrap();
    assert_ne!(changed.template_fingerprint().unwrap(), HASH);
    let mut data = template();
    data["digest"] = json!("changed");
    let changed = ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(900).unwrap(),
        NativeEvidenceSource::FakePve,
        data,
        time() + chrono::Duration::hours(1),
    )
    .unwrap();
    assert_eq!(changed.template_fingerprint().unwrap(), HASH);
    for (key, value) in [
        ("template", json!(0)),
        ("lock", json!("clone")),
        ("ide2", json!("local:iso/a.iso,media=cdrom")),
        ("ide3", json!("none,media=cdrom")),
        ("boot", json!("order=ide2;scsi0")),
        ("bios", json!("ovmf")),
        ("cpu", json!("other")),
        ("balloon", json!(128)),
        ("agent", json!("enabled=0,type=isa")),
    ] {
        let mut data = template();
        data[key] = value;
        assert!(
            parse(data).unwrap().template_fingerprint().is_err(),
            "{key}"
        );
    }
}

fn golden_snapshot() -> Value {
    json!({"contract_version":1,"node":"pve-test","vmid":900,"source":"pve_api",
        "digest":"digest-1","name":"blank-template","cores":2,"memory_mib":2048,
        "primary_disk":{"storage":"local-lvm","volume":"vm-900-disk-0","capacity_bytes":85899345920_u64,"serial":null},
        "uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:09:00","bridge":"vmbr0","system_serial":null,
        "firmware":"seabios","cpu":"host","balloon_mib":0,"qga_enabled":false,"qga_channel":"virtio",
        "boot_profile":"installed_disk","deployment_iso":{"state":"absent"},"driver_iso":{"state":"absent"},
        "template":true,"locked":false,"unsupported":[],"fake_clone_provenance":null,"observed_at":"2026-09-05T12:00:00Z"})
}

#[test]
fn persisted_facts_require_objects_even_when_an_array_has_valid_ordered_fields() {
    let valid = golden_snapshot();
    let keys = [
        "contract_version",
        "node",
        "vmid",
        "source",
        "digest",
        "name",
        "cores",
        "memory_mib",
        "primary_disk",
        "uuid",
        "mac",
        "bridge",
        "system_serial",
        "firmware",
        "cpu",
        "balloon_mib",
        "qga_enabled",
        "qga_channel",
        "boot_profile",
        "deployment_iso",
        "driver_iso",
        "template",
        "locked",
        "unsupported",
        "fake_clone_provenance",
        "observed_at",
    ];
    let array = Value::Array(keys.iter().map(|k| valid[k].clone()).collect());
    assert!(serde_json::from_value::<ProvisioningVmConfigV1>(array).is_err());
    assert!(
        serde_json::from_value::<ProvisioningPrimaryDiskV1>(json!([
            "local-lvm",
            "vm-900-disk-0",
            85899345920_u64,
            null
        ]))
        .is_err()
    );
    assert!(
        serde_json::from_value::<ProvisioningMediaSlotV1>(json!(["iso", "local:iso/a.iso"]))
            .is_err()
    );
    assert!(
        serde_json::from_value::<ProvisioningMediaInventoryV1>(json!([
            1,
            "pve-test",
            "local",
            ["local:iso/a.iso"],
            "complete",
            "2026-09-05T12:00:00Z"
        ]))
        .is_err()
    );
    assert!(
        serde_json::from_value::<ProvisioningQgaObservationV1>(json!([
            1,
            "pve-test",
            101,
            true,
            "2026-09-05T12:00:00Z"
        ]))
        .is_err()
    );
}

#[test]
fn media_discriminators_require_strings_standalone_and_in_snapshots() {
    for state in ["absent", "iso", "unsupported"] {
        let mut valid = json!({"state": state});
        if state == "iso" {
            valid["volid"] = json!("local:iso/a.iso");
        }
        let slot: ProvisioningMediaSlotV1 = serde_json::from_value(valid.clone()).unwrap();
        assert_eq!(serde_json::to_value(slot).unwrap(), valid);

        let mut invalid = valid.clone();
        invalid["state"] = json!({state: null});
        let error = serde_json::from_value::<ProvisioningMediaSlotV1>(invalid.clone()).unwrap_err();
        assert_eq!(error.to_string(), PveReadError::InvalidResponse.to_string());

        for (field, reason) in [
            ("deployment_iso", "deployment_media"),
            ("driver_iso", "driver_media"),
        ] {
            let mut snapshot = golden_snapshot();
            snapshot[field] = valid.clone();
            if state == "unsupported" {
                snapshot["unsupported"] = json!([reason]);
            }
            let decoded: ProvisioningVmConfigV1 = serde_json::from_value(snapshot.clone()).unwrap();
            assert_eq!(serde_json::to_value(decoded).unwrap(), snapshot);
            snapshot[field] = invalid.clone();
            let error = serde_json::from_value::<ProvisioningVmConfigV1>(snapshot).unwrap_err();
            assert_eq!(error.to_string(), PveReadError::InvalidResponse.to_string());
        }
    }
    let secret = json!({"state": {"secret-value": null}});
    let error = serde_json::from_value::<ProvisioningMediaSlotV1>(secret).unwrap_err();
    assert_eq!(error.to_string(), PveReadError::InvalidResponse.to_string());
}

#[test]
fn nested_persisted_fields_reject_duplicates_and_unknowns_without_payloads() {
    for raw in [
        r#"{"storage":"local-lvm","storage":"other","volume":"vm-900-disk-0","capacity_bytes":1,"serial":null}"#,
        r#"{"storage":"local-lvm","volume":"vm-900-disk-0","capacity_bytes":1,"serial":null,"secret":"value"}"#,
    ] {
        assert!(
            !serde_json::from_str::<ProvisioningPrimaryDiskV1>(raw)
                .unwrap_err()
                .to_string()
                .contains("secret")
        );
    }
    for raw in [
        r#"{"state":"iso","state":"absent","volid":"local:iso/a.iso"}"#,
        r#"{"state":"iso","volid":"local:iso/a.iso","volid":"local:iso/b.iso"}"#,
        r#"{"state":"absent","volid":null}"#,
        r#"{"state":"unsupported","volid":null}"#,
    ] {
        assert!(serde_json::from_str::<ProvisioningMediaSlotV1>(raw).is_err());
    }
}

#[test]
fn standalone_classification_decoders_never_echo_untrusted_values() {
    fn check<T: serde::de::DeserializeOwned + std::fmt::Debug>() {
        let error = serde_json::from_str::<T>(r#""secret-value""#).unwrap_err();
        assert!(!error.to_string().contains("secret-value"));
    }
    check::<ProvisioningActionV1>();
    check::<ProvisioningBootProfile>();
    check::<ProvisioningFirmwareV1>();
    check::<ProvisioningCpuV1>();
    check::<ProvisioningQgaChannelV1>();
    check::<ProvisioningUnsupportedV1>();
    check::<ProvisioningCoverageV1>();
}

#[test]
fn fake_provenance_object_shape_is_required_without_changing_legacy_type() {
    let p = provenance();
    let array = json!([
        p["operation_id"],
        p["request_marker"],
        p["source_vmid"],
        p["target_vmid"],
        p["request_digest"]
    ]);
    // The old value's behavior is deliberately not broadened or tightened here.
    let mut raw = destination();
    raw["fake_clone_provenance"] = array.clone();
    assert_eq!(
        ProvisioningVmConfigV1::from_wire(
            node(),
            Vmid::new(101).unwrap(),
            NativeEvidenceSource::FakePve,
            raw,
            time()
        ),
        Err(PveReadError::InvalidResponse)
    );
    let mut raw = destination();
    raw["fake_clone_provenance"] = p;
    let snapshot = ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(101).unwrap(),
        NativeEvidenceSource::FakePve,
        raw,
        time(),
    )
    .unwrap();
    let mut data = serde_json::to_value(snapshot).unwrap();
    data["fake_clone_provenance"] = array;
    assert!(serde_json::from_value::<ProvisioningVmConfigV1>(data).is_err());
}

#[test]
fn persisted_config_exact_shape_rejects_missing_duplicate_and_unknown_fields() {
    let snapshot = parse(template()).unwrap();
    let golden = golden_snapshot();
    assert_eq!(serde_json::to_value(&snapshot).unwrap(), golden);
    assert_eq!(
        serde_json::from_value::<ProvisioningVmConfigV1>(golden.clone()).unwrap(),
        snapshot
    );
    for key in golden.as_object().unwrap().keys() {
        let mut data = golden.clone();
        data.as_object_mut().unwrap().remove(key);
        assert!(
            serde_json::from_value::<ProvisioningVmConfigV1>(data).is_err(),
            "missing {key}"
        );
        let text = serde_json::to_string(&golden).unwrap();
        let duplicate = format!(
            "{{{}:{},{}",
            serde_json::to_string(key).unwrap(),
            golden[key],
            &text[1..]
        );
        assert!(
            serde_json::from_str::<ProvisioningVmConfigV1>(&duplicate).is_err(),
            "duplicate {key}"
        );
    }
    for key in ["storage", "volume", "capacity_bytes", "serial"] {
        let mut data = golden.clone();
        data["primary_disk"].as_object_mut().unwrap().remove(key);
        assert!(
            serde_json::from_value::<ProvisioningVmConfigV1>(data).is_err(),
            "disk.{key}"
        );
    }
    let mut data = golden.clone();
    data["secret-unknown"] = json!("secret-value");
    assert!(
        !serde_json::from_value::<ProvisioningVmConfigV1>(data)
            .unwrap_err()
            .to_string()
            .contains("secret")
    );
}

#[test]
fn persisted_sanitized_invariants_cannot_be_forged() {
    let golden = golden_snapshot();
    for (path, value) in [
        ("/contract_version", json!(2)),
        ("/node", json!("bad/path")),
        ("/vmid", json!(0)),
        ("/source", json!("other")),
        ("/digest", json!("bad secret")),
        ("/name", json!("bad_name")),
        ("/cores", json!(129)),
        ("/memory_mib", json!(513)),
        ("/primary_disk/storage", json!("bad/path")),
        ("/primary_disk/volume", json!("bad/path")),
        ("/primary_disk/capacity_bytes", json!(0)),
        ("/primary_disk/serial", json!("bad,secret")),
        ("/uuid", json!("bad")),
        ("/mac", json!("bad")),
        ("/bridge", json!("bad/path")),
        ("/system_serial", json!("bad,secret")),
        ("/firmware", json!("unsupported")),
        ("/cpu", json!("unsupported")),
        ("/balloon_mib", json!(1)),
        ("/qga_channel", json!("unsupported")),
        ("/qga_enabled", json!(1)),
        ("/boot_profile", Value::Null),
        ("/deployment_iso", json!({"state":"unsupported"})),
        ("/driver_iso", json!({"state":"iso","volid":"bad,secret"})),
        ("/template", Value::Null),
        ("/locked", json!(1)),
        ("/observed_at", json!("bad")),
        ("/unsupported", json!(["firmware"])),
        ("/unsupported", json!(["unknown_field", "unknown_field"])),
    ] {
        let mut data = golden.clone();
        *data.pointer_mut(path).unwrap() = value;
        let err = serde_json::from_value::<ProvisioningVmConfigV1>(data).unwrap_err();
        assert!(!err.to_string().contains("secret"), "{path}");
    }
    for slot in ["deployment_iso", "driver_iso"] {
        for value in [
            json!({"state":"absent","volid":"secret"}),
            json!({"state":"iso"}),
            json!({"state":"iso","volid":"local:iso/a..b.iso"}),
            json!({"state":"unsupported","raw":"secret"}),
        ] {
            let mut data = golden.clone();
            data[slot] = value;
            assert!(serde_json::from_value::<ProvisioningVmConfigV1>(data).is_err());
        }
    }
}

fn provenance() -> Value {
    json!({"operation_id":"019c6e27-e55b-73d1-87d8-4e01f1f75043",
        "request_marker":"019c7714-3b77-74d1-9866-e1f484aae2ab","source_vmid":900,"target_vmid":101,
        "request_digest":"abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"})
}

#[test]
fn fake_marker_requires_fake_source_valid_provenance_and_target_binding() {
    let mut raw = destination();
    raw["fake_clone_provenance"] = provenance();
    assert_eq!(
        ProvisioningVmConfigV1::from_wire(
            node(),
            Vmid::new(101).unwrap(),
            NativeEvidenceSource::PveApi,
            raw.clone(),
            time()
        ),
        Err(PveReadError::InvalidResponse)
    );
    let snapshot = ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(101).unwrap(),
        NativeEvidenceSource::FakePve,
        raw.clone(),
        time(),
    )
    .unwrap();
    assert_eq!(
        snapshot.fake_clone_provenance().unwrap().target_vmid(),
        Vmid::new(101).unwrap()
    );
    assert_eq!(
        serde_json::from_value::<ProvisioningVmConfigV1>(serde_json::to_value(&snapshot).unwrap())
            .unwrap(),
        snapshot
    );
    let mut forged = serde_json::to_value(&snapshot).unwrap();
    forged["source"] = json!("pve_api");
    assert!(serde_json::from_value::<ProvisioningVmConfigV1>(forged).is_err());
    for (key, value) in [
        ("target_vmid", json!(102)),
        ("source_vmid", json!(101)),
        ("request_digest", json!("bad")),
        (
            "request_marker",
            json!("00000000-0000-0000-0000-000000000000"),
        ),
        ("extra", json!("secret")),
    ] {
        let mut data = raw.clone();
        data["fake_clone_provenance"][key] = value.clone();
        assert_eq!(
            ProvisioningVmConfigV1::from_wire(
                node(),
                Vmid::new(101).unwrap(),
                NativeEvidenceSource::FakePve,
                data,
                time()
            ),
            Err(PveReadError::InvalidResponse)
        );
        let mut data = serde_json::to_value(&snapshot).unwrap();
        data["fake_clone_provenance"][key] = value;
        assert!(serde_json::from_value::<ProvisioningVmConfigV1>(data).is_err());
    }
    let mut data = template();
    data["fake_clone_provenance"] = Value::Null;
    assert_eq!(parse(data), Err(PveReadError::InvalidResponse));
}

#[test]
fn media_inventory_binds_storage_unique_bounded_volids_and_explicit_coverage() {
    let storage = StorageName::parse("local").unwrap();
    let valid = ProvisioningMediaInventoryV1::new(
        node(),
        storage.clone(),
        vec!["local:iso/a.iso".into()],
        ProvisioningCoverageV1::Complete,
        time(),
    )
    .unwrap();
    assert_eq!(valid.node(), &node());
    assert_eq!(valid.storage(), &storage);
    assert_eq!(valid.iso_volids(), ["local:iso/a.iso"]);
    assert_eq!(valid.coverage(), ProvisioningCoverageV1::Complete);
    assert_eq!(valid.observed_at(), time());
    assert_eq!(
        serde_json::to_value(&valid).unwrap(),
        json!({"contract_version":1,"node":"pve-test","storage":"local","iso_volids":["local:iso/a.iso"],"coverage":"complete","observed_at":"2026-09-05T12:00:00Z"})
    );
    assert_eq!(
        serde_json::from_value::<ProvisioningMediaInventoryV1>(
            serde_json::to_value(&valid).unwrap()
        )
        .unwrap(),
        valid
    );
    for volumes in [
        vec!["other:iso/a.iso".into()],
        vec!["local:iso/a.iso".into(), "local:iso/a.iso".into()],
        vec!["local:iso/a..b.iso".into()],
        (0..1025).map(|i| format!("local:iso/a{i}.iso")).collect(),
    ] {
        assert_eq!(
            ProvisioningMediaInventoryV1::new(
                node(),
                storage.clone(),
                volumes,
                ProvisioningCoverageV1::Complete,
                time()
            ),
            Err(PveReadError::InvalidResponse)
        );
    }
    let partial = ProvisioningMediaInventoryV1::new(
        node(),
        storage.clone(),
        vec![],
        ProvisioningCoverageV1::Partial,
        time(),
    )
    .unwrap();
    assert_eq!(partial.coverage(), ProvisioningCoverageV1::Partial);
    assert!(
        ProvisioningMediaInventoryV1::new(
            node(),
            storage,
            (0..1024).map(|i| format!("local:iso/a{i}.iso")).collect(),
            ProvisioningCoverageV1::Complete,
            time()
        )
        .is_ok()
    );
}

#[test]
fn qga_is_a_bound_observation_and_auxiliary_persisted_shapes_validate() {
    let fact = ProvisioningQgaObservationV1::new(node(), Vmid::new(101).unwrap(), true, time());
    assert_eq!(fact.node(), &node());
    assert_eq!(fact.vmid(), Vmid::new(101).unwrap());
    assert!(fact.reachable());
    assert_eq!(fact.observed_at(), time());
    let golden = json!({"contract_version":1,"node":"pve-test","vmid":101,"reachable":true,"observed_at":"2026-09-05T12:00:00Z"});
    assert_eq!(serde_json::to_value(&fact).unwrap(), golden);
    assert_eq!(
        serde_json::from_value::<ProvisioningQgaObservationV1>(golden.clone()).unwrap(),
        fact
    );
    let media = json!({"contract_version":1,"node":"pve-test","storage":"local","iso_volids":["local:iso/a.iso"],"coverage":"partial","observed_at":"2026-09-05T12:00:00Z"});
    for (which, valid) in [("qga", golden), ("media", media)] {
        let rejects = |value: Value| {
            if which == "qga" {
                serde_json::from_value::<ProvisioningQgaObservationV1>(value).is_err()
            } else {
                serde_json::from_value::<ProvisioningMediaInventoryV1>(value).is_err()
            }
        };
        for key in valid.as_object().unwrap().keys() {
            let mut data = valid.clone();
            data.as_object_mut().unwrap().remove(key);
            assert!(rejects(data));
            let text = serde_json::to_string(&valid).unwrap();
            let duplicate = format!(
                "{{{}:{},{}",
                serde_json::to_string(key).unwrap(),
                valid[key],
                &text[1..]
            );
            assert!(if which == "qga" {
                serde_json::from_str::<ProvisioningQgaObservationV1>(&duplicate).is_err()
            } else {
                serde_json::from_str::<ProvisioningMediaInventoryV1>(&duplicate).is_err()
            });
        }
        for (key, value) in [
            ("contract_version", json!(2)),
            ("node", json!("bad/path")),
            ("observed_at", json!("bad")),
            ("secret", json!("value")),
        ] {
            let mut data = valid.clone();
            data[key] = value;
            assert!(rejects(data));
        }
        if which == "qga" {
            for (key, value) in [("vmid", json!(0)), ("reachable", json!(1))] {
                let mut data = valid.clone();
                data[key] = value;
                assert!(rejects(data));
            }
        } else {
            for (key, value) in [
                ("storage", json!("other")),
                ("iso_volids", json!(["local:iso/a.iso", "local:iso/a.iso"])),
                ("coverage", json!("unknown")),
            ] {
                let mut data = valid.clone();
                data[key] = value;
                assert!(rejects(data));
            }
        }
    }
}
