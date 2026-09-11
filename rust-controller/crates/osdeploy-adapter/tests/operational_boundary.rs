use osdeploy_adapter::*;
use uuid::Uuid;
#[test]
fn aggregate_claims_never_establish_operational_acceptance() {
    let watchdog = WatchdogBindingV1::new(
        [1, 2, 3, 4, 5, 6].map(Uuid::from_u128),
        ["a".repeat(64), "b".repeat(64)],
        "node1".into(),
        109,
    )
    .unwrap();
    let install = InstallAgentBindingV1::new(
        watchdog,
        [7, 8, 9].map(Uuid::from_u128),
        ["c".repeat(64), "d".repeat(64)],
    )
    .unwrap();
    let heartbeat =
        HeartbeatBindingV1::new(install, [10, 11, 12].map(Uuid::from_u128), "e".repeat(64))
            .unwrap();
    let binding = OperationalBindingV1::new(
        heartbeat.clone(),
        [13, 14].map(Uuid::from_u128),
        "f".repeat(64),
    )
    .unwrap();
    let report = OperationalReportV1 {
        version: 1,
        binding: binding.clone(),
        observed_unix_micros: 1_000_000,
        heartbeat_sequence: 5,
        postconditions: [Some(true); 4],
    };
    assert_eq!(
        binding.assess(None, 5, 2_000_000, 2_000_000),
        Ok(OperationalRefusal::MissingReport)
    );
    assert_eq!(
        binding.assess(Some(&report), 5, 2_000_000, 2_000_000),
        Ok(OperationalRefusal::AuthenticatedOperationalEvidenceUnavailable)
    );
    for i in 0..4 {
        let mut wrong = report.clone();
        wrong.postconditions[i] = None;
        assert_eq!(
            binding.assess(Some(&wrong), 5, 2_000_000, 2_000_000),
            Ok(OperationalRefusal::IncompletePostconditions)
        );
        wrong.postconditions[i] = Some(false);
        assert_eq!(
            binding.assess(Some(&wrong), 5, 2_000_000, 2_000_000),
            Ok(OperationalRefusal::ContradictoryPostconditions)
        );
    }
    assert_eq!(
        binding.assess(Some(&report), 5, 4_000_000, 2_000_000),
        Ok(OperationalRefusal::StaleReport)
    );
    assert!(
        binding
            .assess(Some(&report), 6, 2_000_000, 2_000_000)
            .is_err()
    );
    let wrong = OperationalReportV1 {
        binding: OperationalBindingV1::new(
            heartbeat,
            [13, 99].map(Uuid::from_u128),
            "f".repeat(64),
        )
        .unwrap(),
        ..report.clone()
    };
    assert!(
        binding
            .assess(Some(&wrong), 5, 2_000_000, 2_000_000)
            .is_err()
    );
    assert!(
        binding
            .assess(
                Some(&OperationalReportV1 {
                    version: 2,
                    ..report
                }),
                5,
                2_000_000,
                2_000_000
            )
            .is_err()
    );
}
