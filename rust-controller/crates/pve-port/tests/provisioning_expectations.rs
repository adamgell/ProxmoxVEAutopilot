use pve_port::{
    NativeVmPlan, ProvisioningActionV1, ProvisioningExpectationsInputV1,
    ProvisioningExpectationsV1, ProvisioningOperationPlanV1,
};
use serde_json::{Value, json};

const GIB: u64 = 1_073_741_824;
const SHA: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";

fn vm_json() -> Value {
    json!({"contract_version":1,"cluster_key":"lab","node":"pve-test",
        "source_vmid":900,"target_vmid":101,"name":"deploy-101","storage":"local-lvm",
        "bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "mac":"02:00:00:00:01:01","cores":4,"memory_mib":4096,"minimum_storage_bytes":1})
}

fn input() -> ProvisioningExpectationsInputV1<'static> {
    ProvisioningExpectationsInputV1 {
        vm: serde_json::from_value::<NativeVmPlan>(vm_json()).unwrap(),
        template_config_sha256: SHA,
        template_capacity_bytes: 80 * GIB,
        effective_capacity_bytes: 120 * GIB,
        system_serial: "SYS-101",
        disk_serial: "DISK-101",
        deployment_iso_volid: "local:iso/deployment.iso",
        driver_iso_volid: "local:iso/virtio.iso",
    }
}

#[test]
fn admits_explicit_capacity_and_normalizes_template_sha() {
    let upper = SHA.to_ascii_uppercase();
    let mut data = input();
    data.template_config_sha256 = &upper;
    let expected = ProvisioningExpectationsV1::new(data).unwrap();
    assert_eq!(expected.template_config_sha256(), SHA);
    assert_eq!(expected.template_capacity_bytes(), 80 * GIB);
    assert_eq!(expected.effective_capacity_bytes(), 120 * GIB);
    assert_eq!(expected.vm().minimum_storage_bytes(), 1);
    assert_eq!(expected.system_serial(), "SYS-101");
    assert_eq!(expected.disk_serial(), "DISK-101");
    assert_eq!(expected.deployment_iso_volid(), "local:iso/deployment.iso");
    assert_eq!(expected.driver_iso_volid(), "local:iso/virtio.iso");
}

#[test]
fn rejects_invalid_growth_but_retains_exact_template_bytes() {
    for (template, effective, valid) in [
        (0, 120 * GIB, false),
        (80 * GIB, 79 * GIB, false),
        (80 * GIB, 120 * GIB + 1, false),
        (160 * GIB + 17, 160 * GIB + 17, true),
        (80 * GIB + 17, 120 * GIB, true),
        (u64::MAX, u64::MAX, true),
    ] {
        let mut data = input();
        data.template_capacity_bytes = template;
        data.effective_capacity_bytes = effective;
        assert_eq!(
            ProvisioningExpectationsV1::new(data).is_ok(),
            valid,
            "{template}/{effective}"
        );
    }
}

#[test]
fn enforces_approved_hash_serial_and_iso_token_rules() {
    for bad in [
        "",
        "abcd",
        &"g".repeat(64),
        &"é".repeat(32),
        &"a".repeat(65),
    ] {
        let mut data = input();
        data.template_config_sha256 = bad;
        assert!(ProvisioningExpectationsV1::new(data).is_err());
    }
    for bad in [
        "",
        "a,b",
        "a=b",
        "a b",
        "_a",
        "a-",
        "é",
        "a.b",
        &"a".repeat(65),
    ] {
        let mut data = input();
        data.system_serial = bad;
        assert!(ProvisioningExpectationsV1::new(data).is_err());
        let mut data = input();
        data.disk_serial = bad;
        assert!(ProvisioningExpectationsV1::new(data).is_err());
    }
    for bad in [
        "",
        "local:deployment.iso",
        "local:iso/../a.iso",
        "local:iso/a..b.iso",
        "local:iso/a/b.iso",
        "local:iso/.iso",
        "local:iso/a.ISO",
        "local:iso/---.iso",
        "local:iso/a.iso,media=cdrom",
        "local:iso/a b.iso",
        "bad/storage:iso/a.iso",
    ] {
        let mut data = input();
        data.deployment_iso_volid = bad;
        assert!(ProvisioningExpectationsV1::new(data).is_err(), "{bad}");
        let mut data = input();
        data.driver_iso_volid = bad;
        assert!(ProvisioningExpectationsV1::new(data).is_err(), "{bad}");
    }
    let mut data = input();
    data.driver_iso_volid = data.deployment_iso_volid;
    assert!(ProvisioningExpectationsV1::new(data).is_err());
}

#[test]
fn canonical_shape_roundtrips_and_all_inputs_bind_the_fingerprint() {
    let expected = ProvisioningExpectationsV1::new(input()).unwrap();
    let golden = json!({"contract_version":1,"vm":vm_json(),"template_config_sha256":SHA,
        "template_capacity_bytes":85899345920_u64,"effective_capacity_bytes":128849018880_u64,
        "system_serial":"SYS-101","disk_serial":"DISK-101",
        "deployment_iso_volid":"local:iso/deployment.iso","driver_iso_volid":"local:iso/virtio.iso"});
    assert_eq!(serde_json::to_value(&expected).unwrap(), golden);
    let decoded: ProvisioningExpectationsV1 = serde_json::from_value(golden.clone()).unwrap();
    assert_eq!(decoded, expected);
    let baseline = expected.fingerprint().unwrap();
    for (key, value) in [
        ("template_config_sha256", json!("a".repeat(64))),
        ("template_capacity_bytes", json!(79 * GIB)),
        ("effective_capacity_bytes", json!(121 * GIB)),
        ("system_serial", json!("SYS-102")),
        ("disk_serial", json!("DISK-102")),
        ("deployment_iso_volid", json!("other:iso/deployment.iso")),
        ("driver_iso_volid", json!("other:iso/virtio.iso")),
    ] {
        let mut changed = golden.clone();
        changed[key] = value;
        let changed: ProvisioningExpectationsV1 = serde_json::from_value(changed).unwrap();
        assert_ne!(changed.fingerprint().unwrap(), baseline, "{key}");
    }
    for (key, value) in [
        ("cluster_key", json!("other")),
        ("node", json!("other")),
        ("source_vmid", json!(901)),
        ("target_vmid", json!(102)),
        ("name", json!("deploy-102")),
        ("storage", json!("other")),
        ("bridge", json!("vmbr1")),
        ("uuid", json!("3f2504e0-4f89-41d3-9a0c-0305e82c3302")),
        ("mac", json!("02:00:00:00:01:02")),
        ("cores", json!(8)),
        ("memory_mib", json!(8192)),
        ("minimum_storage_bytes", json!(2)),
    ] {
        let mut changed = golden.clone();
        changed["vm"][key] = value;
        let changed: ProvisioningExpectationsV1 = serde_json::from_value(changed).unwrap();
        assert_ne!(changed.fingerprint().unwrap(), baseline, "vm.{key}");
    }
    let mut digests = std::collections::BTreeSet::new();
    for action in [
        ProvisioningActionV1::Clone,
        ProvisioningActionV1::EnsureCapacity,
        ProvisioningActionV1::ConfigurePe,
        ProvisioningActionV1::StartPe,
        ProvisioningActionV1::EnsureStopped,
        ProvisioningActionV1::ConfigureDisk,
        ProvisioningActionV1::StartDisk,
    ] {
        let plan = ProvisioningOperationPlanV1::new(action, expected.clone());
        assert_eq!(plan.action(), action);
        assert_eq!(plan.expected(), &expected);
        let wire = serde_json::to_value(&plan).unwrap();
        assert_eq!(wire.as_object().unwrap().len(), 3);
        assert_eq!(wire["contract_version"], 1);
        assert_eq!(wire["expected"], golden);
        assert_eq!(
            serde_json::from_value::<ProvisioningOperationPlanV1>(wire).unwrap(),
            plan
        );
        assert!(digests.insert(plan.fingerprint().unwrap()));
    }
}

#[test]
fn persisted_expectations_and_plans_cannot_bypass_admission() {
    let valid = serde_json::to_value(ProvisioningExpectationsV1::new(input()).unwrap()).unwrap();
    for (key, value) in [
        ("contract_version", json!(2)),
        ("template_config_sha256", json!("bad")),
        ("template_capacity_bytes", json!(0)),
        ("effective_capacity_bytes", json!(1)),
        ("system_serial", json!("bad,secret")),
        ("disk_serial", json!("bad,secret")),
        ("deployment_iso_volid", json!("bad,secret")),
        ("driver_iso_volid", json!("bad,secret")),
        ("authority", json!(true)),
    ] {
        let mut changed = valid.clone();
        changed[key] = value;
        let error = serde_json::from_value::<ProvisioningExpectationsV1>(changed).unwrap_err();
        assert!(!error.to_string().contains("secret"));
    }
    for key in valid.as_object().unwrap().keys() {
        let mut missing = valid.clone();
        missing.as_object_mut().unwrap().remove(key);
        assert!(
            serde_json::from_value::<ProvisioningExpectationsV1>(missing).is_err(),
            "{key}"
        );
    }
    let text = serde_json::to_string(&valid).unwrap();
    let duplicate = text.replacen('{', "{\"contract_version\":1,", 1);
    assert!(serde_json::from_str::<ProvisioningExpectationsV1>(&duplicate).is_err());
    for invalid in [
        json!({"contract_version":2,"action":"clone","expected":valid}),
        json!({"contract_version":1,"action":"arbitrary","expected":valid}),
        json!({"contract_version":1,"action":"clone","expected":valid,"authority":true}),
    ] {
        assert!(serde_json::from_value::<ProvisioningOperationPlanV1>(invalid).is_err());
    }
}

#[test]
fn persisted_expectation_and_operation_shape_requires_an_object() {
    let expected = serde_json::to_value(ProvisioningExpectationsV1::new(input()).unwrap()).unwrap();
    let keys = [
        "contract_version",
        "vm",
        "template_config_sha256",
        "template_capacity_bytes",
        "effective_capacity_bytes",
        "system_serial",
        "disk_serial",
        "deployment_iso_volid",
        "driver_iso_volid",
    ];
    let array = Value::Array(keys.iter().map(|k| expected[k].clone()).collect());
    assert!(serde_json::from_value::<ProvisioningExpectationsV1>(array).is_err());
    assert!(
        serde_json::from_value::<ProvisioningOperationPlanV1>(json!([1, "start_pe", expected]))
            .is_err()
    );
}

#[test]
fn nested_vm_plan_must_keep_its_named_fields_in_new_contract() {
    let vm = vm_json();
    let keys = [
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
    ];
    let mut expected =
        serde_json::to_value(ProvisioningExpectationsV1::new(input()).unwrap()).unwrap();
    expected["vm"] = Value::Array(keys.iter().map(|k| vm[k].clone()).collect());
    assert!(serde_json::from_value::<ProvisioningExpectationsV1>(expected).is_err());
}
