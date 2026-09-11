use osdeploy_adapter::*;
use uuid::Uuid;

#[test]
fn malformed_and_incomplete_reports_are_refused() {
    let scope = PeCompleteScopeV1::new(
        Uuid::from_u128(1),
        Uuid::from_u128(2),
        Uuid::from_u128(3),
        Uuid::from_u128(4),
        Uuid::from_u128(5),
        1_000_123,
        60,
    )
    .unwrap();
    let step = Uuid::from_u128(6);
    for state in [
        PeCompleteStepStateV1::Pending,
        PeCompleteStepStateV1::Skipped,
    ] {
        let report = PeCompleteReportV1::new(
            scope.clone(),
            Uuid::from_u128(8),
            1,
            "a".repeat(64),
            vec![(step, state)],
        )
        .unwrap();
        for _ in 0..2 {
            assert_eq!(
                report.assess(
                    &scope,
                    &[step],
                    2_000_000,
                    PeRegisterPrerequisiteV1::Unavailable,
                    AuthenticatedPeWitnessV1::Unavailable
                ),
                Ok(PeCompleteRefusal::IncompleteSteps)
            );
        }
    }
    for steps in [
        vec![(step, PeCompleteStepStateV1::ReportedComplete); 2],
        vec![(Uuid::nil(), PeCompleteStepStateV1::ReportedComplete)],
    ] {
        assert!(
            PeCompleteReportV1::new(scope.clone(), Uuid::from_u128(8), 1, "a".repeat(64), steps)
                .is_err()
        );
    }
    assert!(PeCompleteReportV1::new(scope, Uuid::from_u128(8), 0, "a".repeat(64), vec![]).is_err());
    assert!(
        PeCompleteScopeV1::new(
            Uuid::from_u128(1),
            Uuid::from_u128(2),
            Uuid::from_u128(3),
            Uuid::from_u128(4),
            Uuid::from_u128(5),
            u64::MAX,
            60
        )
        .is_err()
    );
}

#[test]
fn complete_looking_report_cannot_replace_authenticated_registration_predecessor() {
    let scope = PeCompleteScopeV1::new(
        Uuid::from_u128(1),
        Uuid::from_u128(2),
        Uuid::from_u128(3),
        Uuid::from_u128(4),
        Uuid::from_u128(5),
        1_000_123,
        60,
    )
    .unwrap();
    assert_eq!(scope.deadline_unix_micros(), 61_000_123);
    let required = [Uuid::from_u128(6), Uuid::from_u128(7)];
    let report = PeCompleteReportV1::new(
        scope.clone(),
        Uuid::from_u128(8),
        1,
        "a".repeat(64),
        vec![
            (required[0], PeCompleteStepStateV1::ReportedComplete),
            (required[1], PeCompleteStepStateV1::ReportedComplete),
        ],
    )
    .unwrap();
    assert_eq!(
        report.assess(
            &scope,
            &required,
            2_000_000,
            PeRegisterPrerequisiteV1::Unavailable,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(PeCompleteRefusal::AuthenticatedRegistrationUnavailable)
    );
    assert_eq!(
        report.assess(
            &scope,
            &required,
            61_000_123,
            PeRegisterPrerequisiteV1::Unavailable,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(PeCompleteRefusal::OriginalCompletionDeadlineExpired)
    );
    let failed = PeCompleteReportV1::new(
        scope.clone(),
        Uuid::from_u128(8),
        1,
        "a".repeat(64),
        vec![
            (required[0], PeCompleteStepStateV1::ReportedFailed),
            (required[1], PeCompleteStepStateV1::ReportedComplete),
        ],
    )
    .unwrap();
    assert_eq!(
        failed.assess(
            &scope,
            &required,
            2_000_000,
            PeRegisterPrerequisiteV1::Unavailable,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(PeCompleteRefusal::FailedSteps)
    );
    assert_eq!(
        report.assess(
            &scope,
            &required[..1],
            2_000_000,
            PeRegisterPrerequisiteV1::Unavailable,
            AuthenticatedPeWitnessV1::Unavailable
        ),
        Ok(PeCompleteRefusal::StepSetMismatch)
    );
    let shifted = PeCompleteScopeV1::new(
        Uuid::from_u128(1),
        Uuid::from_u128(2),
        Uuid::from_u128(3),
        Uuid::from_u128(4),
        Uuid::from_u128(5),
        2_000_123,
        60,
    )
    .unwrap();
    assert!(
        report
            .assess(
                &shifted,
                &required,
                2_000_000,
                PeRegisterPrerequisiteV1::Unavailable,
                AuthenticatedPeWitnessV1::Unavailable
            )
            .is_err()
    );
}
