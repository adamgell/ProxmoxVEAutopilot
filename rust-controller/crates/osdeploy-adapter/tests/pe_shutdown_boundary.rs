use osdeploy_adapter::*;
use uuid::Uuid;

#[test]
fn stopped_claim_and_expiry_never_grant_shutdown_or_force_stop() {
    let ids = [1, 2, 3, 4, 5].map(Uuid::from_u128);
    let scope = PeShutdownScopeV1::new(ids, 1_000_123, 60).unwrap();
    for observation in [
        PeShutdownObservationV1::Missing,
        PeShutdownObservationV1::ReportedRunning,
        PeShutdownObservationV1::ReportedStopped,
    ] {
        assert_eq!(
            scope.assess(
                &scope,
                2_000_000,
                observation,
                AuthenticatedPeWitnessV1::Unavailable
            ),
            Ok(PeShutdownRefusal::AuthenticatedCompletionUnavailable)
        );
        assert_eq!(
            scope.assess(
                &scope,
                61_000_123,
                observation,
                AuthenticatedPeWitnessV1::Unavailable
            ),
            Ok(PeShutdownRefusal::GuardedEscalationUnavailable)
        );
    }
    let shifted = PeShutdownScopeV1::new(ids, 2_000_123, 60).unwrap();
    assert!(
        scope
            .assess(
                &shifted,
                3_000_000,
                PeShutdownObservationV1::ReportedStopped,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
    assert!(
        scope
            .assess(
                &scope,
                1_000_122,
                PeShutdownObservationV1::Missing,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
}

#[test]
fn malformed_scope_is_not_constructible() {
    let ids = [1, 2, 3, 4, 5].map(Uuid::from_u128);
    for (opened, budget) in [(0, 60), (1, 0), (1, 86401), (u64::MAX, 60)] {
        assert!(PeShutdownScopeV1::new(ids, opened, budget).is_err());
    }
    let mut duplicate = ids;
    duplicate[2] = duplicate[1];
    assert!(PeShutdownScopeV1::new(duplicate, 1, 60).is_err());
    for index in 0..5 {
        let mut nil = ids;
        nil[index] = Uuid::nil();
        assert!(PeShutdownScopeV1::new(nil, 1, 60).is_err());
    }
}
