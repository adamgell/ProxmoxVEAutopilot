use osdeploy_adapter::*;
use serde_json::json;
use uuid::Uuid;

#[test]
fn arming_preserves_original_deadline_and_never_invents_authenticated_witness() {
    let context = StartPeArmingContextV1 {
        run_id: Uuid::from_u128(1),
        start_operation: Uuid::from_u128(2),
        registration_operation: Uuid::from_u128(3),
        attempt: Uuid::from_u128(4),
        generation: Uuid::from_u128(5),
        owner: Uuid::from_u128(6),
        session_id: Uuid::from_u128(7),
        request_sha256: "a".repeat(64),
    };
    let anchor =
        PeRegistrationAnchorV1::new(context.start_operation, Uuid::from_u128(8), 1000, 60).unwrap();
    assert_eq!(anchor.deadline_unix_ms(), 61000);
    let proposal = StartPeAtomicArmingProposalV1::new(context.clone(), anchor.clone()).unwrap();
    let bytes = serde_json::to_vec(&proposal).unwrap();
    let restored = StartPeAtomicArmingProposalV1::decode(&bytes, &context, &anchor).unwrap();
    let witness = AuthenticatedPeWitnessV1::Unavailable;
    assert_eq!(
        restored.assess(60000, witness),
        Ok(StartPeArmingRefusal::AuthenticatedWitnessUnavailable)
    );
    assert_eq!(
        restored.assess(61000, witness),
        Ok(StartPeArmingRefusal::OriginalRegistrationDeadlineExpired)
    );
    assert_eq!(restored.anchor().deadline_unix_ms(), 61000);
    assert!(restored.assess(999, witness).is_err());
    for path in [
        "/context/owner",
        "/context/generation",
        "/context/attempt",
        "/context/session_id",
        "/context/run_id",
        "/context/registration_operation",
        "/anchor/dispatch_event",
    ] {
        let mut bad = serde_json::to_value(&proposal).unwrap();
        *bad.pointer_mut(path).unwrap() = json!(Uuid::from_u128(99));
        assert!(
            StartPeAtomicArmingProposalV1::decode(
                &serde_json::to_vec(&bad).unwrap(),
                &context,
                &anchor
            )
            .is_err(),
            "{path}"
        );
    }
    for (path, value) in [
        ("/anchor/opened_unix_ms", json!(2000)),
        ("/anchor/deadline_unix_ms", json!(62000)),
        ("/anchor/budget_seconds", json!(61)),
        ("/version", json!(2)),
        ("/context/request_sha256", json!("b".repeat(64))),
    ] {
        let mut bad = serde_json::to_value(&proposal).unwrap();
        *bad.pointer_mut(path).unwrap() = value;
        assert!(
            StartPeAtomicArmingProposalV1::decode(
                &serde_json::to_vec(&bad).unwrap(),
                &context,
                &anchor
            )
            .is_err()
        );
    }
    assert!(serde_json::from_value::<AuthenticatedPeWitnessV1>(json!("authenticated")).is_err());
    let mut extra = serde_json::to_value(&proposal).unwrap();
    extra["armed"] = json!(true);
    assert!(
        StartPeAtomicArmingProposalV1::decode(
            &serde_json::to_vec(&extra).unwrap(),
            &context,
            &anchor
        )
        .is_err()
    );
    assert!(StartPeAtomicArmingProposalV1::decode(&vec![b' '; 4097], &context, &anchor).is_err());
    assert!(StartPeAtomicArmingProposalV1::decode(b"{}", &context, &anchor).is_err());
    for field in [
        "run_id",
        "start_operation",
        "registration_operation",
        "attempt",
        "generation",
        "owner",
        "session_id",
    ] {
        let mut bad = serde_json::to_value(&context).unwrap();
        bad[field] = json!(Uuid::nil());
        assert!(
            StartPeAtomicArmingProposalV1::new(
                serde_json::from_value(bad).unwrap(),
                anchor.clone()
            )
            .is_err()
        );
    }
    let mut bad = context.clone();
    bad.request_sha256 = "A".repeat(64);
    assert!(StartPeAtomicArmingProposalV1::new(bad, anchor.clone()).is_err());
    assert!(
        PeRegistrationAnchorV1::new(context.start_operation, Uuid::from_u128(8), u64::MAX, 60)
            .is_err()
    );
    assert!(
        PeRegistrationAnchorV1::new(context.start_operation, Uuid::from_u128(8), 1000, 0).is_err()
    );
    assert!(PeRegistrationAnchorV1::new(Uuid::nil(), Uuid::from_u128(8), 1000, 60).is_err());
}
