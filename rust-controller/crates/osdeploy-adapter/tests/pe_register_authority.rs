use osdeploy_adapter::*;
use uuid::Uuid;

#[test]
fn verifier_authority_checks_exact_fence_and_result_revision_without_authentication_success() {
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
    let verifier = PeRegisterVerifierContractV1::new(
        context.clone(),
        anchor.clone(),
        request.clone(),
        fence.clone(),
        5,
    )
    .unwrap();
    let check = |candidate: &PeRegisterCallbackCandidateV1,
                 current: &PeRegisterAuthorityFenceV1,
                 revision,
                 original| {
        verifier.assess(
            candidate,
            current,
            revision,
            AuthenticatedPeWitnessV1::Unavailable,
            original,
            2_000_000,
        )
    };
    assert_eq!(
        check(&candidate, &fence, 5, None),
        Ok(PeRegisterVerifierRefusal::AuthenticatedWitnessUnavailable)
    );
    assert_eq!(
        check(&candidate, &fence, 4, None),
        Ok(PeRegisterVerifierRefusal::ResultRevisionMismatch)
    );
    assert_eq!(
        check(&candidate, &fence, 5, Some((&candidate, 5))),
        Ok(PeRegisterVerifierRefusal::Duplicate)
    );
    assert_eq!(
        check(&candidate, &fence, 5, Some((&candidate, 4))),
        Ok(PeRegisterVerifierRefusal::Conflict)
    );
    for changed in 0..4 {
        let mut wrong = fence.clone();
        match changed {
            0 => wrong.authority_generation += 1,
            1 => wrong.worker_id = "worker-b".into(),
            2 => wrong.lease_epoch = Uuid::from_u128(12),
            _ => wrong.operation_revision += 1,
        };
        assert_eq!(
            check(&candidate, &wrong, 5, None),
            Ok(PeRegisterVerifierRefusal::FenceMismatch)
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
        check(&different, &fence, 5, Some((&candidate, 5))),
        Ok(PeRegisterVerifierRefusal::Conflict)
    );
    let mut wrong_context = context.clone();
    wrong_context.session_id = Uuid::from_u128(99);
    let wrong = PeRegisterCallbackCandidateV1::new(
        wrong_context,
        &anchor,
        request.clone(),
        PeRegisterResultIdentityV1 {
            result_id: Uuid::from_u128(10),
            payload_sha256: "c".repeat(64),
        },
    )
    .unwrap();
    assert_eq!(
        check(&wrong, &fence, 5, None),
        Ok(PeRegisterVerifierRefusal::SessionBindingMismatch)
    );
    assert_eq!(
        verifier.assess(
            &candidate,
            &fence,
            5,
            AuthenticatedPeWitnessV1::Unavailable,
            None,
            61_000_123
        ),
        Ok(PeRegisterVerifierRefusal::OriginalDeadlineExpired)
    );
    for changed in 0..4 {
        let mut invalid = fence.clone();
        match changed {
            0 => invalid.authority_generation = 0,
            1 => invalid.worker_id.clear(),
            2 => invalid.lease_epoch = Uuid::nil(),
            _ => invalid.operation_revision = 0,
        }
        assert!(
            PeRegisterVerifierContractV1::new(
                context.clone(),
                anchor.clone(),
                request.clone(),
                invalid.clone(),
                5
            )
            .is_err()
        );
        assert!(check(&candidate, &invalid, 5, None).is_err());
    }
    assert!(
        PeRegisterVerifierContractV1::new(
            context.clone(),
            anchor.clone(),
            request.clone(),
            fence.clone(),
            0
        )
        .is_err()
    );
    assert!(PeRegisterVerifierContractV1::new(context, anchor, request, fence, u64::MAX).is_err());
}
