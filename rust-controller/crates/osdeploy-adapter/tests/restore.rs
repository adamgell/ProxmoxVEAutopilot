use osdeploy_adapter::{ContractError, restore_osdeploy_plan_v1};
use serde_json::{Value, json};
const PLAN: &str = include_str!("fixtures/plan-v1.json");
const SHA: &str = "47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9";

#[test]
fn restores_complete_admitted_plan() {
    let plan = restore_osdeploy_plan_v1(PLAN, SHA).unwrap();
    assert_eq!(plan.fingerprint().unwrap(), SHA);
    assert_eq!(
        serde_json::to_value(plan).unwrap(),
        serde_json::from_str::<serde_json::Value>(PLAN).unwrap()
    );
}

#[test]
fn rejects_duplicate_fields_before_value_normalization() {
    let duplicated = PLAN.replacen(
        "\"contract_version\": 1",
        "\"contract_version\": 1, \"contract_version\": 1",
        1,
    );
    assert_eq!(
        restore_osdeploy_plan_v1(&duplicated, SHA),
        Err(ContractError::InvalidPersistedPlan)
    );
}

fn snapshot() -> Value {
    serde_json::from_str(PLAN).unwrap()
}

fn reject(value: &Value) {
    let text = serde_json::to_string(value).unwrap();
    // A caller cannot admit a forged derived field by hashing the forgery.
    for hash in [
        SHA.to_owned(),
        event_journal::payload_digest(value).unwrap(),
    ] {
        assert_eq!(
            restore_osdeploy_plan_v1(&text, &hash),
            Err(ContractError::InvalidPersistedPlan),
            "{text}"
        );
    }
}

fn roundtrip(value: &Value) -> osdeploy_adapter::OsDeployPlanV1 {
    let hash = event_journal::payload_digest(value).unwrap();
    let restored = restore_osdeploy_plan_v1(&serde_json::to_string(value).unwrap(), &hash).unwrap();
    assert_eq!(serde_json::to_value(&restored).unwrap(), *value);
    assert_eq!(restored.fingerprint().unwrap(), hash);
    restored
}

// Independent field inventories prevent fixture omissions from shrinking coverage.
const OBJECTS: &[(&str, &[&str])] = &[
    (
        "",
        &[
            "contract_version",
            "workflow_kind",
            "architecture",
            "server_role",
            "firmware",
            "secure_boot",
            "cpu",
            "balloon",
            "qga_channel",
            "primary_disk",
            "pe_iso_slot",
            "driver_iso_slot",
            "pe_boot_order",
            "disk_boot_order",
            "template_device_policy",
            "evidence_level",
            "vm",
            "names",
            "artifact",
            "disk",
            "policy",
            "driver_payload",
            "osd_client_payload",
            "agent_payload",
            "template_config_sha256",
            "deployment_iso_volid",
            "driver_iso_volid",
            "system_serial",
            "disk_serial",
            "os_version",
            "os_edition",
            "os_language",
            "image_name",
            "secret_profile_id",
            "callback_profile_id",
        ],
    ),
    (
        "/vm",
        &[
            "contract_version",
            "cluster_key",
            "node",
            "source_vmid",
            "target_vmid",
            "name",
            "storage",
            "bridge",
            "uuid",
            "mac",
            "cores",
            "memory_mib",
            "minimum_storage_bytes",
        ],
    ),
    (
        "/names",
        &[
            "requested_name",
            "pve_name",
            "windows_name",
            "expected_agent_id",
        ],
    ),
    (
        "/artifact",
        &[
            "contract_version",
            "artifact_id",
            "architecture",
            "build_label",
            "iso_sha256",
            "wim_sha256",
            "source_image_index",
            "output_image_index",
            "apply_image_index",
            "image_index_source",
            "evidence_level",
        ],
    ),
    (
        "/disk",
        &[
            "requested_gib",
            "template_bytes",
            "effective_bytes",
            "growth_required",
        ],
    ),
    (
        "/policy",
        &[
            "registration_seconds",
            "pe_seconds",
            "shutdown_grace_seconds",
            "full_os_seconds",
            "mutation_seconds",
            "evidence_freshness_seconds",
            "allow_force_stop",
        ],
    ),
    (
        "/driver_payload",
        &["reference_id", "sha256", "evidence_level"],
    ),
    (
        "/osd_client_payload",
        &["reference_id", "sha256", "evidence_level"],
    ),
    (
        "/agent_payload",
        &["reference_id", "sha256", "evidence_level"],
    ),
];

#[test]
fn every_top_level_and_nested_field_is_required_unique_and_typed() {
    let original = snapshot();
    for &(path, keys) in OBJECTS {
        let object = original.pointer(path).unwrap().as_object().unwrap();
        assert_eq!(object.len(), keys.len(), "inventory: {path}");
        for key in keys {
            assert!(object.contains_key(*key), "inventory: {path}/{key}");
            let mut missing = original.clone();
            missing
                .pointer_mut(path)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .remove(*key);
            reject(&missing);

            let mut malformed = original.clone();
            malformed.pointer_mut(path).unwrap()[*key] = json!([]);
            reject(&malformed);

            let text = serde_json::to_string(&original).unwrap();
            let object_text = serde_json::to_string(object).unwrap();
            let field = format!("{}:{}", serde_json::to_string(key).unwrap(), object[*key]);
            let duplicated = object_text.replacen(&field, &format!("{field},{field}"), 1);
            assert_ne!(duplicated, object_text);
            let duplicated_plan = text.replacen(&object_text, &duplicated, 1);
            assert_eq!(
                restore_osdeploy_plan_v1(&duplicated_plan, SHA),
                Err(ContractError::InvalidPersistedPlan),
                "duplicate: {path}/{key}"
            );
        }
    }
}

#[test]
fn every_object_rejects_unknown_fields_scalars_null_and_positional_arrays() {
    for &(path, keys) in OBJECTS {
        let original = snapshot();
        let mut unknown = original.clone();
        unknown.pointer_mut(path).unwrap()["unexpected"] = json!("sentinel");
        reject(&unknown);
        let positional = Value::Array(
            keys.iter()
                .map(|key| original.pointer(path).unwrap()[*key].clone())
                .collect(),
        );
        for shape in [
            positional,
            json!([]),
            json!(null),
            json!("object"),
            json!(1),
            json!(false),
        ] {
            let mut changed = original.clone();
            *changed.pointer_mut(path).unwrap() = shape;
            reject(&changed);
        }
    }
}

#[test]
fn fixed_fields_cannot_be_changed_even_with_a_matching_forged_digest() {
    for (key, value) in [
        ("contract_version", json!(2)),
        ("workflow_kind", json!("native_vm")),
        ("architecture", json!("arm64")),
        ("server_role", json!("dc")),
        ("firmware", json!("ovmf")),
        ("secure_boot", json!(true)),
        ("cpu", json!("kvm64")),
        ("balloon", json!(true)),
        ("qga_channel", json!("isa")),
        ("primary_disk", json!("scsi1")),
        ("pe_iso_slot", json!("ide1")),
        ("driver_iso_slot", json!("ide2")),
        ("pe_boot_order", json!("scsi0")),
        ("disk_boot_order", json!("ide2;scsi0")),
        ("template_device_policy", json!("allow_extra_devices")),
        ("evidence_level", json!("verified")),
    ] {
        let mut value_under_test = snapshot();
        value_under_test[key] = value;
        reject(&value_under_test);
    }
}

#[test]
fn every_string_field_rejects_enum_objects_numbers_booleans_and_null() {
    let original = snapshot();
    for &(path, keys) in OBJECTS {
        for key in keys {
            if let Some(string) = original.pointer(path).unwrap()[*key].as_str() {
                for shape in [json!({string: null}), json!(0), json!(true), json!(null)] {
                    let mut value = original.clone();
                    value.pointer_mut(path).unwrap()[*key] = shape;
                    reject(&value);
                }
            }
        }
    }
}

#[test]
fn normalized_and_derived_claims_cannot_be_forged() {
    for (path, value) in [
        ("/names/windows_name", json!("OtherHost")),
        ("/names/expected_agent_id", json!("agent-otherhost")),
        ("/names/pve_name", json!("other-pve")),
        ("/disk/effective_bytes", json!(128849018881_u64)),
        ("/disk/growth_required", json!(false)),
        ("/artifact/contract_version", json!(2)),
        ("/artifact/apply_image_index", json!(6)),
        ("/artifact/image_index_source", json!("source_fallback")),
        ("/artifact/evidence_level", json!("verified")),
        ("/driver_payload/evidence_level", json!("verified")),
        ("/osd_client_payload/evidence_level", json!("declared")),
        ("/agent_payload/evidence_level", json!("declared")),
        ("/driver_payload/sha256", json!(null)),
        ("/artifact/iso_sha256", json!("A".repeat(64))),
        ("/artifact/wim_sha256", json!("B".repeat(64))),
        ("/driver_payload/sha256", json!("D".repeat(64))),
        ("/template_config_sha256", json!("C".repeat(64))),
        ("/vm/mac", json!("02:aa:00:00:00:01")),
    ] {
        let mut changed = snapshot();
        *changed.pointer_mut(path).unwrap() = value;
        reject(&changed);
    }
}

#[test]
fn constructors_revalidate_identities_resources_and_input_limits() {
    for (path, value) in [
        ("/vm/contract_version", json!(2)),
        ("/vm/cluster_key", json!("")),
        ("/vm/node", json!("bad/node")),
        ("/vm/source_vmid", json!(0)),
        ("/vm/target_vmid", json!(900)),
        ("/vm/name", json!("bad/name")),
        ("/vm/storage", json!("")),
        ("/vm/bridge", json!("bad/bridge")),
        ("/vm/uuid", json!("00000000-0000-0000-0000-000000000000")),
        ("/vm/mac", json!("not-a-mac")),
        ("/vm/cores", json!(0)),
        ("/vm/cores", json!(129)),
        ("/vm/memory_mib", json!(3968)),
        ("/vm/memory_mib", json!(4097)),
        ("/vm/minimum_storage_bytes", json!(0)),
        ("/names/requested_name", json!("12345")),
        ("/names/requested_name", json!("a".repeat(257))),
        (
            "/artifact/artifact_id",
            json!("00000000-0000-0000-0000-000000000000"),
        ),
        ("/artifact/architecture", json!("arm64")),
        ("/artifact/build_label", json!("2026")),
        ("/artifact/iso_sha256", json!("x".repeat(64))),
        ("/artifact/wim_sha256", json!("b".repeat(63))),
        ("/artifact/source_image_index", json!(0)),
        ("/artifact/output_image_index", json!(0)),
        ("/artifact/output_image_index", json!(2147483648_u64)),
        ("/disk/requested_gib", json!(79)),
        ("/disk/requested_gib", json!(u64::MAX)),
        ("/disk/template_bytes", json!(0)),
        ("/policy/registration_seconds", json!(0)),
        ("/policy/pe_seconds", json!(86401)),
        ("/policy/shutdown_grace_seconds", json!(0)),
        ("/policy/full_os_seconds", json!(86401)),
        ("/policy/mutation_seconds", json!(29)),
        ("/policy/evidence_freshness_seconds", json!(0)),
        ("/policy/evidence_freshness_seconds", json!(301)),
        ("/template_config_sha256", json!("bad")),
        ("/deployment_iso_volid", json!("https://host/a.iso")),
        ("/driver_iso_volid", json!("media-store:iso/deploy.iso")),
        ("/system_serial", json!("bad serial")),
        ("/disk_serial", json!("s".repeat(65))),
        ("/os_version", json!("")),
        ("/os_edition", json!("../Enterprise")),
        ("/os_language", json!("en_US")),
        ("/image_name", json!("Windows;command")),
        (
            "/secret_profile_id",
            json!("00000000-0000-0000-0000-000000000000"),
        ),
        (
            "/callback_profile_id",
            json!("00000000-0000-0000-0000-000000000000"),
        ),
    ] {
        let mut changed = snapshot();
        *changed.pointer_mut(path).unwrap() = value;
        reject(&changed);
    }
    for payload in ["driver_payload", "osd_client_payload", "agent_payload"] {
        for (key, value) in [
            (
                "reference_id",
                json!("00000000-0000-0000-0000-000000000000"),
            ),
            ("sha256", json!("not-a-hash")),
        ] {
            let mut changed = snapshot();
            changed[payload][key] = value;
            reject(&changed);
        }
    }
}

#[test]
fn valid_source_fallback_and_all_required_null_payloads_roundtrip() {
    let mut value = snapshot();
    value["artifact"]["output_image_index"] = json!(null);
    value["artifact"]["apply_image_index"] = json!(6);
    value["artifact"]["image_index_source"] = json!("source_fallback");
    value["driver_payload"]["sha256"] = json!(null);
    value["driver_payload"]["evidence_level"] = json!("unverified");
    roundtrip(&value);
    for path in [
        "/artifact/output_image_index",
        "/driver_payload/sha256",
        "/osd_client_payload/sha256",
        "/agent_payload/sha256",
    ] {
        let (object, key) = path.rsplit_once('/').unwrap();
        let mut missing = value.clone();
        missing
            .pointer_mut(object)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(key);
        reject(&missing);
    }
}

#[test]
fn nonintegral_retained_capacity_and_phase_policy_boundaries_roundtrip() {
    let mut retained = snapshot();
    retained["disk"] = json!({"requested_gib":80, "template_bytes":128849018881_u64, "effective_bytes":128849018881_u64, "growth_required":false});
    roundtrip(&retained);
    for (seconds, freshness) in [(1, 1), (86400, 300)] {
        let mut boundary = snapshot();
        boundary["policy"] = json!({"registration_seconds":seconds, "pe_seconds":seconds, "shutdown_grace_seconds":seconds, "full_os_seconds":seconds, "mutation_seconds":seconds, "evidence_freshness_seconds":freshness, "allow_force_stop":true});
        roundtrip(&boundary);
    }
}

#[test]
fn broad_serial_declaration_restores_but_does_not_grant_dispatch_compatibility() {
    let mut value = snapshot();
    value["disk_serial"] = json!("s".repeat(21));
    let restored = roundtrip(&value);
    assert_eq!(
        osdeploy_adapter::pve_expectations(&restored),
        Err(ContractError::UnsupportedPveDiskSerial)
    );
}

#[test]
fn equivalent_json_layout_roundtrips_but_wrong_or_noncanonical_hashes_do_not() {
    let original = snapshot();
    let entries = original
        .as_object()
        .unwrap()
        .iter()
        .rev()
        .map(|(key, value)| format!("{}:{value}", json!(key)))
        .collect::<Vec<_>>()
        .join(",\n");
    let reordered = format!(" \n{{{entries}}}\t\r\n");
    assert_eq!(
        restore_osdeploy_plan_v1(&reordered, SHA)
            .unwrap()
            .fingerprint()
            .unwrap(),
        SHA
    );
    for hash in [
        "".to_owned(),
        "a".repeat(63),
        "a".repeat(65),
        "g".repeat(64),
        "a".repeat(64),
        SHA.to_uppercase(),
        format!(" {SHA}"),
        "é".repeat(32),
    ] {
        assert_eq!(
            restore_osdeploy_plan_v1(PLAN, &hash),
            Err(ContractError::InvalidPersistedPlan)
        );
    }
}

#[test]
fn rejects_malformed_trailing_and_deep_json_with_payload_free_errors() {
    for text in [
        "".to_owned(),
        "{".to_owned(),
        format!("{PLAN}{{}}"),
        format!("{PLAN}sentinel-secret-123"),
        format!("{}0{}", "{\"a\":".repeat(129), "}".repeat(129)),
        PLAN.replace("SYS-01", "sentinel-secret-123\\n"),
    ] {
        let error = restore_osdeploy_plan_v1(&text, SHA).unwrap_err();
        assert_eq!(error, ContractError::InvalidPersistedPlan);
        assert_eq!(error.to_string(), "persisted deployment plan is invalid");
        assert_eq!(format!("{error:?}"), "InvalidPersistedPlan");
    }
    let escaped_duplicate = PLAN.replacen(
        "\"contract_version\": 1",
        "\"contract_version\": 1, \"contract_\\u0076ersion\": 1",
        1,
    );
    assert_eq!(
        restore_osdeploy_plan_v1(&escaped_duplicate, SHA),
        Err(ContractError::InvalidPersistedPlan)
    );
}

#[test]
fn input_limit_counts_utf8_bytes_and_accepts_exactly_65536() {
    let mut value = snapshot();
    value["names"]["requested_name"] = json!("Labé");
    value["names"]["windows_name"] = json!("Lab");
    value["names"]["expected_agent_id"] = json!("agent-lab");
    let hash = event_journal::payload_digest(&value).unwrap();
    let mut text = serde_json::to_string(&value).unwrap();
    text.push_str(&" ".repeat(65536 - text.len()));
    assert_eq!(text.len(), 65536);
    assert!(restore_osdeploy_plan_v1(&text, &hash).is_ok());
    text.push(' ');
    assert_eq!(text.chars().count(), 65536);
    assert_eq!(
        restore_osdeploy_plan_v1(&text, &hash),
        Err(ContractError::InvalidPersistedPlan)
    );
}
