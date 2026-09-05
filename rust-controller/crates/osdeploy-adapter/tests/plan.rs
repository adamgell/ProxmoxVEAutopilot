use artifact_index::{DeclaredArtifact, DeclaredArtifactInput};
use osdeploy_adapter::{
    ContractError, DeploymentNames, DiskCapacity, OsDeployPlanInput, OsDeployPlanV1,
    PayloadDeclaration, PayloadDeclarationInput, PhasePolicy, PhasePolicyInput,
};
use pve_port::{
    BridgeName, MacAddress, NativeVmName, NativeVmPlan, NodeName, StorageName, VmUuid, Vmid,
};
use serde_json::{Value, json};
use uuid::Uuid;

const HASH_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const HASH_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const HASH_C: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const HASH_D: &str = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
const UPPER_C: &str = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC";
const UPPER_D: &str = "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD";
const ARTIFACT_ID: &str = "22222222-2222-4222-8222-222222222222";
const DRIVER_ID: &str = "33333333-3333-4333-8333-333333333333";
const CLIENT_ID: &str = "44444444-4444-4444-8444-444444444444";
const AGENT_ID: &str = "55555555-5555-4555-8555-555555555555";
const SECRET_ID: &str = "66666666-6666-4666-8666-666666666666";
const CALLBACK_ID: &str = "77777777-7777-4777-8777-777777777777";
const GOLDEN_DIGEST: &str = "47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9";

fn id(value: &str) -> Uuid {
    Uuid::parse_str(value).unwrap()
}
fn artifact_input() -> DeclaredArtifactInput<'static> {
    DeclaredArtifactInput {
        artifact_id: id(ARTIFACT_ID),
        architecture: "amd64",
        build_label: "20260905010203",
        iso_sha256: HASH_A,
        wim_sha256: HASH_B,
        source_image_index: 6,
        output_image_index: Some(1),
    }
}
fn payload(reference_id: &str, sha256: Option<&str>) -> PayloadDeclaration {
    PayloadDeclaration::new(PayloadDeclarationInput {
        reference_id: id(reference_id),
        sha256,
    })
    .unwrap()
}
fn fixture() -> OsDeployPlanInput<'static> {
    let vm = NativeVmPlan::new(
        1,
        NativeVmName::parse("test-cluster").unwrap(),
        NodeName::parse("node-a").unwrap(),
        Vmid::new(900).unwrap(),
        Vmid::new(901).unwrap(),
        NativeVmName::parse("pve-target-01").unwrap(),
        StorageName::parse("disk-store").unwrap(),
        BridgeName::parse("vmbr0").unwrap(),
        VmUuid::parse("11111111-1111-4111-8111-111111111111").unwrap(),
        MacAddress::parse("02:00:00:00:00:01").unwrap(),
        4,
        4096,
        1_073_741_824,
    )
    .unwrap();
    OsDeployPlanInput {
        names: DeploymentNames::new("  --Lab VM_01--  ", vm.name().clone()).unwrap(),
        vm,
        artifact: DeclaredArtifact::new(artifact_input()).unwrap(),
        disk: DiskCapacity::new(120, 85_899_345_920).unwrap(),
        policy: PhasePolicy::production_defaults(false),
        template_config_sha256: HASH_C,
        deployment_iso_volid: "media-store:iso/deploy.iso",
        driver_iso_volid: "drivers:iso/virtio.iso",
        driver_payload: payload(DRIVER_ID, Some(HASH_D)),
        osd_client_payload: payload(CLIENT_ID, None),
        agent_payload: payload(AGENT_ID, None),
        system_serial: "SYS-01",
        disk_serial: "DISK_01",
        os_version: "Windows 11",
        os_edition: "Enterprise",
        os_language: "en-US",
        image_name: "Windows 11 Enterprise",
        secret_profile_id: id(SECRET_ID),
        callback_profile_id: id(CALLBACK_ID),
    }
}
// NativeVmPlan has existing validated deserialization; none of the new outputs do.
fn replace_vm_field(input: &mut OsDeployPlanInput<'_>, field: &str, value: Value) {
    let mut vm = serde_json::to_value(&input.vm).unwrap();
    vm[field] = value;
    input.vm = serde_json::from_value(vm).unwrap();
}
fn expected_snapshot() -> Value {
    serde_json::from_str(
        r#"{
  "contract_version": 1,
  "workflow_kind": "os_deploy",
  "architecture": "amd64",
  "server_role": "base",
  "firmware": "seabios",
  "secure_boot": false,
  "cpu": "host",
  "balloon": false,
  "qga_channel": "virtio",
  "primary_disk": "scsi0",
  "pe_iso_slot": "ide2",
  "driver_iso_slot": "ide3",
  "pe_boot_order": "ide2;scsi0",
  "disk_boot_order": "scsi0",
  "template_device_policy": "reject_extra_devices",
  "evidence_level": "declared_only",
  "vm": {
    "contract_version": 1,
    "cluster_key": "test-cluster",
    "node": "node-a",
    "source_vmid": 900,
    "target_vmid": 901,
    "name": "pve-target-01",
    "storage": "disk-store",
    "bridge": "vmbr0",
    "uuid": "11111111-1111-4111-8111-111111111111",
    "mac": "02:00:00:00:00:01",
    "cores": 4,
    "memory_mib": 4096,
    "minimum_storage_bytes": 1073741824
  },
  "names": {
    "requested_name": "  --Lab VM_01--  ",
    "pve_name": "pve-target-01",
    "windows_name": "LabVM01",
    "expected_agent_id": "agent-labvm01"
  },
  "artifact": {
    "contract_version": 1,
    "artifact_id": "22222222-2222-4222-8222-222222222222",
    "architecture": "amd64",
    "build_label": "20260905010203",
    "iso_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "wim_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "source_image_index": 6,
    "output_image_index": 1,
    "apply_image_index": 1,
    "image_index_source": "output_manifest",
    "evidence_level": "declared_only"
  },
  "disk": {
    "requested_gib": 120,
    "template_bytes": 85899345920,
    "effective_bytes": 128849018880,
    "growth_required": true
  },
  "policy": {
    "registration_seconds": 2400,
    "pe_seconds": 7200,
    "shutdown_grace_seconds": 300,
    "full_os_seconds": 7200,
    "mutation_seconds": 300,
    "evidence_freshness_seconds": 30,
    "allow_force_stop": false
  },
  "template_config_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "deployment_iso_volid": "media-store:iso/deploy.iso",
  "driver_iso_volid": "drivers:iso/virtio.iso",
  "driver_payload": {
    "reference_id": "33333333-3333-4333-8333-333333333333",
    "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "evidence_level": "declared"
  },
  "osd_client_payload": {
    "reference_id": "44444444-4444-4444-8444-444444444444",
    "sha256": null,
    "evidence_level": "unverified"
  },
  "agent_payload": {
    "reference_id": "55555555-5555-4555-8555-555555555555",
    "sha256": null,
    "evidence_level": "unverified"
  },
  "system_serial": "SYS-01",
  "disk_serial": "DISK_01",
  "os_version": "Windows 11",
  "os_edition": "Enterprise",
  "os_language": "en-US",
  "image_name": "Windows 11 Enterprise",
  "secret_profile_id": "66666666-6666-4666-8666-666666666666",
  "callback_profile_id": "77777777-7777-4777-8777-777777777777"
}"#,
    )
    .unwrap()
}
#[test]
fn complete_snapshot_pins_all_inputs_fixed_native_fields_and_nested_claims() {
    let plan = OsDeployPlanV1::new(fixture()).unwrap();
    assert_eq!(serde_json::to_value(&plan).unwrap(), expected_snapshot());
    assert_eq!(plan.vm().memory_mib(), 4096);
    assert_eq!(plan.vm().minimum_storage_bytes(), 1_073_741_824);
    assert_eq!(plan.names().pve_name(), plan.vm().name());
    assert_eq!(plan.artifact().apply_image_index(), 1);
    assert_eq!(plan.disk().effective_bytes(), 128_849_018_880);
    assert_eq!(plan.policy(), &PhasePolicy::production_defaults(false));
    assert_eq!(plan.template_config_sha256(), HASH_C);
    assert_eq!(plan.deployment_iso_volid(), "media-store:iso/deploy.iso");
    assert_eq!(plan.driver_iso_volid(), "drivers:iso/virtio.iso");
    assert_eq!(plan.driver_payload().reference_id(), id(DRIVER_ID));
    assert_eq!(plan.osd_client_payload().reference_id(), id(CLIENT_ID));
    assert_eq!(plan.agent_payload().reference_id(), id(AGENT_ID));
    assert_eq!(plan.system_serial(), "SYS-01");
    assert_eq!(plan.disk_serial(), "DISK_01");
    assert_eq!(plan.os_version(), "Windows 11");
    assert_eq!(plan.os_edition(), "Enterprise");
    assert_eq!(plan.os_language(), "en-US");
    assert_eq!(plan.image_name(), "Windows 11 Enterprise");
    assert_eq!(plan.secret_profile_id(), id(SECRET_ID));
    assert_eq!(plan.callback_profile_id(), id(CALLBACK_ID));
}
#[test]
fn canonical_fingerprint_matches_independently_hashed_literal_snapshot_on_reconstruction() {
    // Independently sorted compact literal JSON was hashed with shasum -a 256.
    for _ in 0..3 {
        let plan = OsDeployPlanV1::new(fixture()).unwrap();
        assert_eq!(plan.fingerprint().unwrap(), GOLDEN_DIGEST);
        assert_eq!(
            plan.fingerprint().unwrap(),
            event_journal::payload_digest(&serde_json::to_value(&plan).unwrap()).unwrap()
        );
    }
}
#[test]
fn payload_hash_presence_controls_only_declared_or_unverified_claim() {
    for (hash, normalized, claim) in [
        (None, None, "unverified"),
        (Some(UPPER_D), Some(HASH_D), "declared"),
    ] {
        let declaration = payload(DRIVER_ID, hash);
        assert_eq!(declaration.reference_id(), id(DRIVER_ID));
        assert_eq!(declaration.sha256(), normalized);
        assert_eq!(
            serde_json::to_value(&declaration).unwrap(),
            json!({
                "reference_id":DRIVER_ID, "sha256":normalized, "evidence_level":claim,
            })
        );
    }
    assert!(
        PayloadDeclaration::new(PayloadDeclarationInput {
            reference_id: Uuid::from_u128(1),
            sha256: None
        })
        .is_ok()
    );
    assert_eq!(
        PayloadDeclaration::new(PayloadDeclarationInput {
            reference_id: Uuid::nil(),
            sha256: None
        }),
        Err(ContractError::InvalidPayloadReference)
    );
}
fn malformed_hashes() -> Vec<String> {
    vec![
        String::new(),
        "a".repeat(63),
        "a".repeat(65),
        "g".repeat(64),
        format!("{} ", "a".repeat(63)),
        format!("{}\n", "a".repeat(63)),
        "é".repeat(32),
        format!("{}0", "０".repeat(21)),
    ]
}
#[test]
fn payload_and_template_hashes_reject_length_nonhex_unicode_and_controls() {
    for hash in malformed_hashes() {
        assert_eq!(
            PayloadDeclaration::new(PayloadDeclarationInput {
                reference_id: id(DRIVER_ID),
                sha256: Some(&hash)
            }),
            Err(ContractError::InvalidPayloadSha256),
            "{hash:?}"
        );
        assert_eq!(
            OsDeployPlanV1::new(OsDeployPlanInput {
                template_config_sha256: &hash,
                ..fixture()
            }),
            Err(ContractError::InvalidTemplateConfigSha256),
            "{hash:?}"
        );
    }
}
#[test]
fn vm_admission_requires_four_gib_and_consistent_independent_pve_name() {
    let mut input = fixture();
    replace_vm_field(&mut input, "memory_mib", json!(3968));
    assert_eq!(
        OsDeployPlanV1::new(input),
        Err(ContractError::InsufficientVmMemory)
    );
    for memory in [4096, 4224, 1_048_576] {
        let mut input = fixture();
        replace_vm_field(&mut input, "memory_mib", json!(memory));
        assert!(OsDeployPlanV1::new(input).is_ok());
    }
    let mut input = fixture();
    input.names =
        DeploymentNames::new("LabVM01", NativeVmName::parse("another-pve").unwrap()).unwrap();
    assert_eq!(
        OsDeployPlanV1::new(input),
        Err(ContractError::VmNameMismatch)
    );
}
#[test]
fn artifact_apply_index_fits_signed_guest_consumer_and_retains_source_provenance() {
    for (source, output, accepted) in [
        (2_147_483_647, None, true),
        (2_147_483_648, None, false),
        (6, Some(2_147_483_647), true),
        (6, Some(2_147_483_648), false),
        (u32::MAX, Some(1), true),
    ] {
        let mut input = fixture();
        input.artifact = DeclaredArtifact::new(DeclaredArtifactInput {
            source_image_index: source,
            output_image_index: output,
            ..artifact_input()
        })
        .unwrap();
        let result = OsDeployPlanV1::new(input);
        if accepted {
            assert!(result.is_ok());
        } else {
            assert_eq!(result, Err(ContractError::InvalidApplyImageIndex));
        }
    }
}
#[test]
fn profile_references_are_required_non_nil_opaque_ids() {
    for secret in [false, true] {
        let mut input = fixture();
        if secret {
            input.secret_profile_id = Uuid::nil();
        } else {
            input.callback_profile_id = Uuid::nil();
        }
        assert_eq!(
            OsDeployPlanV1::new(input),
            Err(ContractError::InvalidProfileReference)
        );
        let mut input = fixture();
        if secret {
            input.secret_profile_id = Uuid::from_u128(1);
        } else {
            input.callback_profile_id = Uuid::from_u128(1);
        }
        assert!(OsDeployPlanV1::new(input).is_ok());
    }
}
#[test]
fn both_media_fields_admit_only_narrow_native_iso_references() {
    let valid = [
        "media:iso/a.iso".to_owned(),
        "Store_1.a-b:iso/._a-v1.2_.iso".to_owned(),
        format!("{}:iso/{}.iso", "s".repeat(64), "f".repeat(128)),
    ];
    let invalid = [
        "",
        "https://host/a.iso",
        "store:iso/.iso",
        "store:iso/---_.iso",
        "store:iso/a..b.iso",
        "store:iso/a..iso",
        "store:iso/../a.iso",
        "store:iso/a/...iso",
        "store:iso/a.iso/",
        "store:iso/a.ISO",
        "store:iso/a.img",
        "store:iso/a.iso.iso/extra",
        "store:iso/a%20b.iso",
        "store:iso/a\\b.iso",
        "store:iso/a b.iso",
        "store:iso/a\nb.iso",
        "store:iso/a\tb.iso",
        "store:iso/a\0b.iso",
        "store:iso/é.iso",
        "store:iso/a:b.iso",
        ":iso/a.iso",
        "-store:iso/a.iso",
        "store/extra:iso/a.iso",
        "store:images/a.iso",
        "store:iso//a.iso",
        "store:iso/a?.iso",
        "store:iso/a$.iso",
        "store:iso/a;.iso",
        "store:iso/a'.iso",
        "store:iso/a\".iso",
        "store:iso/a\u{60}b.iso",
    ]
    .into_iter()
    .map(str::to_owned)
    .chain([
        format!("{}:iso/a.iso", "s".repeat(65)),
        format!("store:iso/{}.iso", "f".repeat(129)),
    ])
    .collect::<Vec<_>>();
    for driver in [false, true] {
        for media in &valid {
            let mut input = fixture();
            if driver {
                input.driver_iso_volid = media;
            } else {
                input.deployment_iso_volid = media;
            }
            assert!(OsDeployPlanV1::new(input).is_ok(), "{media:?}");
        }
        for media in &invalid {
            let mut input = fixture();
            if driver {
                input.driver_iso_volid = media;
            } else {
                input.deployment_iso_volid = media;
            }
            assert_eq!(
                OsDeployPlanV1::new(input),
                Err(ContractError::InvalidMediaVolid),
                "{media:?}"
            );
        }
    }
    let mut input = fixture();
    input.driver_iso_volid = input.deployment_iso_volid;
    assert_eq!(
        OsDeployPlanV1::new(input),
        Err(ContractError::DuplicateMediaVolid)
    );
}
#[test]
fn both_serials_require_bounded_ascii_tokens_with_alphanumeric_endpoints() {
    let valid = ["A".to_owned(), "a_1-Z".to_owned(), "s".repeat(64)];
    let invalid = [
        "", "-A", "A-", "_A", "A_", "A B", "A.B", "A/B", "A:B", "A;B", "A\nB", "é", "A$B",
    ]
    .into_iter()
    .map(str::to_owned)
    .chain(["s".repeat(65)])
    .collect::<Vec<_>>();
    for disk in [false, true] {
        for serial in &valid {
            let mut input = fixture();
            if disk {
                input.disk_serial = serial;
            } else {
                input.system_serial = serial;
            }
            assert!(OsDeployPlanV1::new(input).is_ok());
        }
        for serial in &invalid {
            let mut input = fixture();
            if disk {
                input.disk_serial = serial;
            } else {
                input.system_serial = serial;
            }
            assert_eq!(
                OsDeployPlanV1::new(input),
                Err(ContractError::InvalidSerial),
                "{serial:?}"
            );
        }
    }
}
#[test]
fn os_labels_and_language_are_bounded_declarations_without_commands_or_paths() {
    let valid = [
        "A".to_owned(),
        "Windows 11.2_Enterprise-x64".to_owned(),
        "a".repeat(128),
    ];
    let invalid = [
        "", " A", "A ", "-A", "A_", "A/B", "C:\\A", "A;B", "A$B", "A\nB", "é", "A:B",
    ]
    .into_iter()
    .map(str::to_owned)
    .chain(["a".repeat(129)])
    .collect::<Vec<_>>();
    for field in 0..3 {
        for (label, accepted) in valid
            .iter()
            .map(|x| (x, true))
            .chain(invalid.iter().map(|x| (x, false)))
        {
            let mut input = fixture();
            match field {
                0 => input.os_version = label,
                1 => input.os_edition = label,
                _ => input.image_name = label,
            }
            let result = OsDeployPlanV1::new(input);
            if accepted {
                assert!(result.is_ok());
            } else {
                assert_eq!(result, Err(ContractError::InvalidOsLabel), "{label:?}");
            }
        }
    }
    for language in ["en", "en-US", "abcd-EFGH-ijklmn"] {
        assert!(
            OsDeployPlanV1::new(OsDeployPlanInput {
                os_language: language,
                ..fixture()
            })
            .is_ok()
        );
    }
    for language in [
        "",
        "e",
        "abcdefghijklmnopq",
        "-en",
        "en-",
        "en_Us",
        "e1",
        "en US",
        "éé",
        "en/US",
        "en\nUS",
    ] {
        assert_eq!(
            OsDeployPlanV1::new(OsDeployPlanInput {
                os_language: language,
                ..fixture()
            }),
            Err(ContractError::InvalidOsLanguage),
            "{language:?}"
        );
    }
}
fn assert_changed(label: &str, change: impl FnOnce(&mut OsDeployPlanInput<'static>)) {
    let original = OsDeployPlanV1::new(fixture())
        .unwrap()
        .fingerprint()
        .unwrap();
    let mut input = fixture();
    change(&mut input);
    assert_ne!(
        OsDeployPlanV1::new(input).unwrap().fingerprint().unwrap(),
        original,
        "unbound input: {label}"
    );
}
#[test]
fn fingerprint_binds_every_vm_identity_resource_and_free_storage_field() {
    for (field, value) in [
        ("cluster_key", json!("other-cluster")),
        ("node", json!("node-b")),
        ("source_vmid", json!(902)),
        ("target_vmid", json!(903)),
        ("name", json!("other-pve")),
        ("storage", json!("other-storage")),
        ("bridge", json!("vmbr1")),
        ("uuid", json!("11111111-1111-4111-8111-111111111112")),
        ("mac", json!("02:00:00:00:00:02")),
        ("cores", json!(8)),
        ("memory_mib", json!(8192)),
        ("minimum_storage_bytes", json!(1)),
    ] {
        assert_changed(field, |input| {
            replace_vm_field(input, field, value);
            if field == "name" {
                input.names =
                    DeploymentNames::new("  --Lab VM_01--  ", input.vm.name().clone()).unwrap();
            }
        });
    }
}
#[test]
fn fingerprint_binds_names_capacity_every_phase_and_force_stop_policy() {
    for requested in ["Lab VM_01", "OtherHost", "labvm01"] {
        assert_changed("requested and derived names", |input| {
            input.names = DeploymentNames::new(requested, input.vm.name().clone()).unwrap();
        });
    }
    assert_changed("requested capacity", |input| {
        input.disk = DiskCapacity::new(121, 85_899_345_920).unwrap()
    });
    assert_changed("template capacity", |input| {
        input.disk = DiskCapacity::new(120, 85_899_345_921).unwrap()
    });
    for field in 0..7 {
        assert_changed("phase policy", |input| {
            let mut policy = PhasePolicyInput {
                registration_seconds: 2400,
                pe_seconds: 7200,
                shutdown_grace_seconds: 300,
                full_os_seconds: 7200,
                mutation_seconds: 300,
                evidence_freshness_seconds: 30,
                allow_force_stop: false,
            };
            match field {
                0 => policy.registration_seconds += 1,
                1 => policy.pe_seconds += 1,
                2 => policy.shutdown_grace_seconds += 1,
                3 => policy.full_os_seconds += 1,
                4 => policy.mutation_seconds += 1,
                5 => policy.evidence_freshness_seconds += 1,
                _ => policy.allow_force_stop = true,
            }
            input.policy = PhasePolicy::new(policy).unwrap();
        });
    }
}
#[test]
fn fingerprint_binds_every_artifact_input_and_output_index_provenance() {
    for field in 0..7 {
        assert_changed("artifact declaration", |input| {
            let mut artifact = artifact_input();
            match field {
                0 => artifact.artifact_id = Uuid::from_u128(1),
                1 => artifact.build_label = "20260905010204",
                2 => artifact.iso_sha256 = HASH_C,
                3 => artifact.wim_sha256 = HASH_C,
                4 => artifact.source_image_index = 7,
                5 => artifact.output_image_index = Some(2),
                _ => artifact.output_image_index = None,
            }
            input.artifact = DeclaredArtifact::new(artifact).unwrap();
        });
    }
    let make = |output_image_index| {
        let mut input = fixture();
        input.artifact = DeclaredArtifact::new(DeclaredArtifactInput {
            source_image_index: 1,
            output_image_index,
            ..artifact_input()
        })
        .unwrap();
        OsDeployPlanV1::new(input).unwrap()
    };
    let declared = make(Some(1));
    let fallback = make(None);
    assert_eq!(
        declared.artifact().apply_image_index(),
        fallback.artifact().apply_image_index()
    );
    assert_ne!(
        declared.fingerprint().unwrap(),
        fallback.fingerprint().unwrap()
    );
    assert_eq!(
        serde_json::to_value(fallback).unwrap()["artifact"]["image_index_source"],
        "source_fallback"
    );
}
#[test]
fn fingerprint_binds_every_media_payload_serial_os_and_profile_reference() {
    for field in 0..10 {
        assert_changed("text declaration", |input| match field {
            0 => input.template_config_sha256 = HASH_A,
            1 => input.deployment_iso_volid = "media-store:iso/other.iso",
            2 => input.driver_iso_volid = "drivers:iso/other.iso",
            3 => input.system_serial = "SYS-02",
            4 => input.disk_serial = "DISK_02",
            5 => input.os_version = "Windows 12",
            6 => input.os_edition = "Professional",
            7 => input.os_language = "fr-FR",
            8 => input.image_name = "Windows 11 Pro",
            _ => input.secret_profile_id = Uuid::from_u128(1),
        });
    }
    assert_changed("callback profile", |input| {
        input.callback_profile_id = Uuid::from_u128(1)
    });
    for field in 0..3 {
        for change_hash in [false, true] {
            assert_changed("payload reference or hash", |input| {
                let current = match field {
                    0 => &input.driver_payload,
                    1 => &input.osd_client_payload,
                    _ => &input.agent_payload,
                };
                let replacement = PayloadDeclaration::new(PayloadDeclarationInput {
                    reference_id: if change_hash {
                        current.reference_id()
                    } else {
                        Uuid::from_u128(1)
                    },
                    sha256: if change_hash {
                        Some(HASH_A)
                    } else {
                        current.sha256()
                    },
                })
                .unwrap();
                match field {
                    0 => input.driver_payload = replacement,
                    1 => input.osd_client_payload = replacement,
                    _ => input.agent_payload = replacement,
                }
            });
        }
    }
    assert_changed("missing driver hash", |input| {
        input.driver_payload = payload(DRIVER_ID, None)
    });
}
#[test]
fn equivalent_hash_case_normalizes_before_complete_fingerprinting() {
    let mut input = fixture();
    input.template_config_sha256 = UPPER_C;
    input.driver_payload = payload(DRIVER_ID, Some(UPPER_D));
    input.artifact = DeclaredArtifact::new(DeclaredArtifactInput {
        iso_sha256: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        wim_sha256: "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        ..artifact_input()
    })
    .unwrap();
    let plan = OsDeployPlanV1::new(input).unwrap();
    assert_eq!(plan.template_config_sha256(), HASH_C);
    assert_eq!(plan.driver_payload().sha256(), Some(HASH_D));
    assert_eq!(plan.fingerprint().unwrap(), GOLDEN_DIGEST);
}
