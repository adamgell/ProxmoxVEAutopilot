use osdeploy_adapter::*;
use uuid::Uuid;

fn scope(stage: OsDeployStage) -> DiskBootScopeV1 {
    DiskBootScopeV1::new(
        stage,
        [1, 2, 3, 4].map(Uuid::from_u128),
        "node1".into(),
        109,
    )
    .unwrap()
}
fn evidence() -> DiskBootEvidenceV1 {
    DiskBootEvidenceV1 {
        node: "node1".into(),
        vmid: 109,
        reported_stopped: true,
        disk_volid: "local-lvm:vm-109-disk-0".into(),
        configuration_identity: "opaque-pve-digest".into(),
        observed_unix_micros: 1_000_000,
    }
}
#[test]
fn both_stages_refuse_even_matching_claims_and_repeated_reads() {
    for stage in [OsDeployStage::ConfigureDisk, OsDeployStage::StartDisk] {
        let scope = scope(stage);
        for _ in 0..2 {
            assert_eq!(
                scope.assess(&scope, Some(&evidence()), 2_000_000, 2_000_000),
                Ok(DiskBootRefusal::DurablePredecessorAndAuthorityUnavailable)
            );
        }
        assert_eq!(
            scope.assess(&scope, None, 2_000_000, 2_000_000),
            Ok(DiskBootRefusal::MissingEvidence)
        );
        let mut wrong = evidence();
        wrong.vmid += 1;
        assert!(
            scope
                .assess(&scope, Some(&wrong), 2_000_000, 2_000_000)
                .is_err()
        );
        wrong = evidence();
        wrong.reported_stopped = false;
        assert_eq!(
            scope.assess(&scope, Some(&wrong), 2_000_000, 2_000_000),
            Ok(DiskBootRefusal::NotStopped)
        );
        assert!(
            scope
                .assess(&scope, Some(&evidence()), 999_999, 2_000_000)
                .is_err()
        );
        assert_eq!(
            scope.assess(&scope, Some(&evidence()), 4_000_000, 2_000_000),
            Ok(DiskBootRefusal::StaleEvidence)
        );
    }
}
#[test]
fn substitutions_and_invalid_evidence_fail_closed() {
    let configure = scope(OsDeployStage::ConfigureDisk);
    assert!(
        configure
            .assess(
                &scope(OsDeployStage::StartDisk),
                Some(&evidence()),
                2_000_000,
                2_000_000
            )
            .is_err()
    );
    for field in ["", "bad\nvalue"] {
        let mut bad = evidence();
        bad.disk_volid = field.into();
        assert!(
            configure
                .assess(&configure, Some(&bad), 2_000_000, 2_000_000)
                .is_err()
        );
    }
    let mut ids = [1, 2, 3, 4].map(Uuid::from_u128);
    ids[2] = ids[1];
    assert!(DiskBootScopeV1::new(OsDeployStage::ConfigureDisk, ids, "node1".into(), 109).is_err());
    assert!(
        DiskBootScopeV1::new(
            OsDeployStage::StartPe,
            [1, 2, 3, 4].map(Uuid::from_u128),
            "node1".into(),
            109
        )
        .is_err()
    );
}
