#![cfg(feature = "fixture-ipc")]
use osdeploy_adapter::*;
use postgres_store::*;
use uuid::Uuid;

#[tokio::test]
async fn result_contract_refuses_without_connection_or_partial_commit() {
    let store = PgStore::new(
        sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgres://localhost:1/not_used")
            .unwrap(),
    );
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
        session_revision: 2,
    };
    let candidate = PeRegisterCallbackCandidateV1::new(
        context.clone(),
        &anchor,
        request.clone(),
        PeRegisterResultIdentityV1 {
            result_id: Uuid::from_u128(10),
            payload_sha256: "c".repeat(64),
        },
    )
    .unwrap();
    let fence = PeRegisterAuthorityFenceV1 {
        authority_generation: 3,
        worker_id: "worker-a".into(),
        lease_epoch: Uuid::from_u128(11),
        operation_revision: 4,
    };
    let contract =
        PeRegisterVerifierContractV1::new(context, anchor, request, fence.clone(), 5).unwrap();
    for (original, checked, expected) in [
        (
            None,
            2_000_000,
            PeRegisterVerifierRefusal::AuthenticatedWitnessUnavailable,
        ),
        (
            Some((&candidate, 5)),
            2_000_000,
            PeRegisterVerifierRefusal::Duplicate,
        ),
        (
            Some((&candidate, 4)),
            2_000_000,
            PeRegisterVerifierRefusal::Conflict,
        ),
        (
            None,
            61_000_123,
            PeRegisterVerifierRefusal::OriginalDeadlineExpired,
        ),
    ] {
        let result = store
            .assess_pe_register_result_transaction(PeRegisterResultTransactionInputV1 {
                contract: &contract,
                candidate: &candidate,
                current_fence: &fence,
                result_revision: 5,
                original,
                checked_unix_micros: checked,
            })
            .await
            .unwrap();
        assert_eq!(
            result,
            PeRegisterResultRefusalV1::RollbackRequired(expected)
        );
    }
    let mut stale = fence.clone();
    stale.authority_generation += 1;
    assert_eq!(
        store
            .assess_pe_register_result_transaction(PeRegisterResultTransactionInputV1 {
                contract: &contract,
                candidate: &candidate,
                current_fence: &stale,
                result_revision: 5,
                original: Some((&candidate, 5)),
                checked_unix_micros: 2_000_000,
            })
            .await
            .unwrap(),
        PeRegisterResultRefusalV1::RollbackRequired(PeRegisterVerifierRefusal::FenceMismatch)
    );
}
