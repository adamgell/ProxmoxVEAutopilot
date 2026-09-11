use osdeploy_adapter::*;
use uuid::Uuid;
#[test]
fn fresh_heartbeat_claims_never_authenticate_and_stale_or_replayed_claims_refuse() {
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
    let binding = HeartbeatBindingV1::new(
        install.clone(),
        [10, 11, 12].map(Uuid::from_u128),
        "e".repeat(64),
    )
    .unwrap();
    let report = HeartbeatReportV1 {
        version: 1,
        binding: binding.clone(),
        sequence: 2,
        observed_unix_micros: 1_000_000,
    };
    assert_eq!(
        binding.assess(
            None,
            1,
            2_000_000,
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(HeartbeatRefusal::MissingReport)
    );
    assert_eq!(
        binding.assess(
            Some(&report),
            1,
            2_000_000,
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(HeartbeatRefusal::AuthenticatedAgentUnavailable)
    );
    assert_eq!(
        binding.assess(
            Some(&report),
            2,
            2_000_000,
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(HeartbeatRefusal::ReplayOrOutOfOrder)
    );
    assert_eq!(
        binding.assess(
            Some(&report),
            1,
            4_000_001,
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(HeartbeatRefusal::StaleReport)
    );
    assert!(
        binding
            .assess(
                Some(&report),
                1,
                999_999,
                2_000_000,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
    let wrong = HeartbeatReportV1 {
        binding: HeartbeatBindingV1::new(
            install,
            [10, 11, 99].map(Uuid::from_u128),
            "e".repeat(64),
        )
        .unwrap(),
        ..report.clone()
    };
    assert!(
        binding
            .assess(
                Some(&wrong),
                1,
                2_000_000,
                2_000_000,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
    let wrong = HeartbeatReportV1 {
        version: 2,
        ..report
    };
    assert!(
        binding
            .assess(
                Some(&wrong),
                1,
                2_000_000,
                2_000_000,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
}
