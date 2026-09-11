use artifact_index::{DeclaredArtifact, DeclaredArtifactInput};
use osdeploy_adapter::*;
use pve_port::*;
use serde_json::json;
use uuid::Uuid;
const SHA: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
const GIB: u64 = 1_073_741_824;
fn input(serial: &str, template: u64) -> OsDeployPlanInput<'_> {
    let vm: NativeVmPlan = serde_json::from_value(json!({"contract_version":1,"cluster_key":"lab","node":"pve-test","source_vmid":900,"target_vmid":101,"name":"deploy-101","storage":"local-lvm","bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:01:01","cores":4,"memory_mib":4096,"minimum_storage_bytes":1})).unwrap();
    let payload = || {
        PayloadDeclaration::new(PayloadDeclarationInput {
            reference_id: Uuid::from_u128(1),
            sha256: None,
        })
        .unwrap()
    };
    OsDeployPlanInput {
        names: DeploymentNames::new("deploy-101", vm.name().clone()).unwrap(),
        vm,
        artifact: DeclaredArtifact::new(DeclaredArtifactInput {
            artifact_id: Uuid::from_u128(2),
            architecture: "amd64",
            build_label: "20260905010203",
            iso_sha256: SHA,
            wim_sha256: SHA,
            source_image_index: 6,
            output_image_index: Some(1),
        })
        .unwrap(),
        disk: DiskCapacity::new(120, template).unwrap(),
        policy: PhasePolicy::production_defaults(false),
        template_config_sha256: SHA,
        deployment_iso_volid: "local:iso/deployment.iso",
        driver_iso_volid: "drivers:iso/virtio.iso",
        driver_payload: payload(),
        osd_client_payload: payload(),
        agent_payload: payload(),
        system_serial: "SYS-101",
        disk_serial: serial,
        os_version: "Windows 11",
        os_edition: "Enterprise",
        os_language: "en-US",
        image_name: "Windows 11 Enterprise",
        secret_profile_id: Uuid::from_u128(3),
        callback_profile_id: Uuid::from_u128(4),
    }
}
#[test]
fn derives_every_pve_field_and_retains_effective_capacity() {
    for (template, want) in [
        (80 * GIB, 120 * GIB),
        (160 * GIB, 160 * GIB),
        (160 * GIB + 17, 160 * GIB + 17),
    ] {
        let p = OsDeployPlanV1::new(input("DISK-101", template)).unwrap();
        let e = pve_expectations(&p).unwrap();
        assert_eq!(e.vm(), p.vm());
        assert_eq!(e.template_config_sha256(), p.template_config_sha256());
        assert_eq!(e.template_capacity_bytes(), template);
        assert_eq!(e.effective_capacity_bytes(), want);
        assert_eq!(e.system_serial(), p.system_serial());
        assert_eq!(e.disk_serial(), p.disk_serial());
        assert_eq!(e.deployment_iso_volid(), p.deployment_iso_volid());
        assert_eq!(e.driver_iso_volid(), p.driver_iso_volid());
    }
}
#[test]
fn serial_20_succeeds_and_21_fails_without_narrowing_declarations() {
    let p20 = OsDeployPlanV1::new(input("12345678901234567890", 80 * GIB)).unwrap();
    assert_eq!(
        pve_expectations(&p20).unwrap().disk_serial(),
        "12345678901234567890"
    );
    let p21 = OsDeployPlanV1::new(input("123456789012345678901", 80 * GIB)).unwrap();
    assert_eq!(
        pve_expectations(&p21),
        Err(ContractError::UnsupportedPveDiskSerial)
    );
    assert_eq!(
        ContractError::UnsupportedPveDiskSerial.to_string(),
        "desired PVE disk serial exceeds the 20-byte dispatch limit"
    );
    assert_eq!(
        ContractError::InvalidPveExpectations.to_string(),
        "PVE expectations could not be derived from the deployment plan"
    );
}
#[test]
fn non_pve_inputs_change_workflow_digest_without_changing_pve_subset() {
    let p = OsDeployPlanV1::new(input("DISK-101", 80 * GIB)).unwrap();
    for kind in 0..4 {
        let mut changed = input("DISK-101", 80 * GIB);
        match kind {
            0 => changed.secret_profile_id = Uuid::now_v7(),
            1 => changed.callback_profile_id = Uuid::now_v7(),
            2 => changed.policy = PhasePolicy::production_defaults(true),
            _ => changed.os_language = "en-GB",
        }
        let changed = OsDeployPlanV1::new(changed).unwrap();
        assert_ne!(p.fingerprint().unwrap(), changed.fingerprint().unwrap());
        assert_eq!(
            pve_expectations(&p).unwrap(),
            pve_expectations(&changed).unwrap()
        );
        assert_ne!(
            p.fingerprint().unwrap(),
            pve_expectations(&p).unwrap().fingerprint().unwrap()
        );
    }
}
