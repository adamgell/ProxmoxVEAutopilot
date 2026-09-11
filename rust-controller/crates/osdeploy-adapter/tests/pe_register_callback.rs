use osdeploy_adapter::*;
use serde_json::json;
use uuid::Uuid;

#[test]
fn callback_witness_stays_unavailable_and_replay_conflicts_keep_original_deadline() {
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
        PeRegistrationAnchorV2::new(context.start_operation, Uuid::from_u128(8), 1_000_123, 60)
            .unwrap();
    let request = PeRegisterRequestIdentityV1 {
        request_id: Uuid::from_u128(9),
        request_sha256: "b".repeat(64),
        session_revision: 1,
    };
    let result = PeRegisterResultIdentityV1 {
        result_id: Uuid::from_u128(10),
        payload_sha256: "c".repeat(64),
    };
    let candidate =
        PeRegisterCallbackCandidateV1::new(context.clone(), &anchor, request.clone(), result)
            .unwrap();
    let bytes = serde_json::to_vec(&candidate).unwrap();
    let decoded =
        PeRegisterCallbackCandidateV1::decode(&bytes, &context, &anchor, &request).unwrap();
    assert_eq!(
        decoded.assess(2_000_000, AuthenticatedPeWitnessV1::Unavailable, None),
        Ok(PeRegisterCallbackRefusal::AuthenticatedWitnessUnavailable)
    );
    assert_eq!(
        decoded.assess(
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable,
            Some(&candidate)
        ),
        Ok(PeRegisterCallbackRefusal::Duplicate)
    );
    assert_eq!(
        decoded.assess(61_000_123, AuthenticatedPeWitnessV1::Unavailable, None),
        Ok(PeRegisterCallbackRefusal::OriginalDeadlineExpired)
    );
    for (pointer, value) in [
        ("/context/session_id", json!(Uuid::from_u128(99))),
        ("/context/owner", json!(Uuid::from_u128(99))),
        ("/request/request_id", json!(Uuid::from_u128(99))),
        ("/request/session_revision", json!(2)),
        ("/deadline_unix_micros", json!(61_000_124)),
        ("/dispatch_event", json!(Uuid::from_u128(99))),
    ] {
        let mut bad = serde_json::to_value(&candidate).unwrap();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(
            PeRegisterCallbackCandidateV1::decode(
                &serde_json::to_vec(&bad).unwrap(),
                &context,
                &anchor,
                &request
            )
            .is_err()
        );
    }
    let different = PeRegisterCallbackCandidateV1::new(
        context.clone(),
        &anchor,
        request.clone(),
        PeRegisterResultIdentityV1 {
            result_id: Uuid::from_u128(10),
            payload_sha256: "d".repeat(64),
        },
    )
    .unwrap();
    assert_eq!(
        different.assess(
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable,
            Some(&candidate)
        ),
        Ok(PeRegisterCallbackRefusal::Conflict)
    );
    let mut bad = serde_json::to_value(&candidate).unwrap();
    bad["authenticated"] = json!(true);
    assert!(
        PeRegisterCallbackCandidateV1::decode(
            &serde_json::to_vec(&bad).unwrap(),
            &context,
            &anchor,
            &request
        )
        .is_err()
    );
    assert!(
        PeRegisterCallbackCandidateV1::decode(&vec![b' '; 4097], &context, &anchor, &request)
            .is_err()
    );
    for (pointer, value) in [
        ("/context/attempt", json!(Uuid::from_u128(99))),
        ("/context/generation", json!(Uuid::from_u128(99))),
        ("/request/request_sha256", json!("d".repeat(64))),
        ("/request/session_revision", json!(0)),
        ("/result/result_id", json!(Uuid::nil())),
        ("/result/payload_sha256", json!("A".repeat(64))),
    ] {
        let mut bad = serde_json::to_value(&candidate).unwrap();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(
            PeRegisterCallbackCandidateV1::decode(
                &serde_json::to_vec(&bad).unwrap(),
                &context,
                &anchor,
                &request
            )
            .is_err(),
            "{pointer}"
        );
    }
    assert!(
        candidate
            .assess(1_000_122, AuthenticatedPeWitnessV1::Unavailable, None)
            .is_err()
    );
    let changed_id = PeRegisterCallbackCandidateV1::new(
        context.clone(),
        &anchor,
        request.clone(),
        PeRegisterResultIdentityV1 {
            result_id: Uuid::from_u128(11),
            payload_sha256: "c".repeat(64),
        },
    )
    .unwrap();
    assert_eq!(
        changed_id.assess(
            2_000_000,
            AuthenticatedPeWitnessV1::Unavailable,
            Some(&candidate)
        ),
        Ok(PeRegisterCallbackRefusal::Conflict)
    );
}
