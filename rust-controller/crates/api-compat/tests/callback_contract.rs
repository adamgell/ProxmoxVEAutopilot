use api_compat::callback_contract::*;
use controller_domain::{AttemptId, OperationId};
use serde_json::json;

fn candidate() -> ResultCandidate {
    ResultCandidate {
        binding: ResultBinding {
            operation: OperationId::new(),
            attempt: AttemptId::new(),
        },
        status: ResultStatus::Failed,
        message: Some("failed once".into()),
        data: json!({"exit_code": 1, "detail": "synthetic"}),
    }
}

#[test]
fn first_candidate_and_failed_replay_do_not_rewrite_original() {
    let first = candidate();
    assert_eq!(
        classify_result(first.binding, None, &first),
        ResultDisposition::FirstCandidate
    );
    let mut reordered = first.clone();
    reordered.data = json!({"detail":"synthetic", "exit_code":1});
    assert_eq!(
        classify_result(first.binding, Some(&first), &reordered),
        ResultDisposition::EquivalentReplay
    );
    assert_eq!(first.status, ResultStatus::Failed);
}

#[test]
fn status_message_and_data_conflicts_cannot_replace_first_result() {
    let first = candidate();
    let mut status = first.clone();
    status.status = ResultStatus::Success;
    let mut message = first.clone();
    message.message = None;
    let mut data = first.clone();
    data.data = json!({"exit_code":0});
    for incoming in [status, message, data] {
        assert_eq!(
            classify_result(first.binding, Some(&first), &incoming),
            ResultDisposition::Conflict
        );
    }
}

#[test]
fn cross_operation_stale_attempt_and_misbound_original_fail_closed() {
    let first = candidate();
    let mut operation = first.clone();
    operation.binding.operation = OperationId::new();
    let mut attempt = first.clone();
    attempt.binding.attempt = AttemptId::new();
    for incoming in [operation, attempt] {
        assert_eq!(
            classify_result(first.binding, None, &incoming),
            ResultDisposition::BindingMismatch
        );
        assert_eq!(
            classify_result(first.binding, Some(&incoming), &first),
            ResultDisposition::BindingMismatch
        );
    }
}

#[test]
fn legacy_phase_complete_preserves_order_any_phase_and_failed_distinction() {
    let steps: Vec<_> = [
        ("a", "winpe", LegacyStepState::Done),
        ("b", "any", LegacyStepState::AwaitingReboot),
        ("c", "full_os", LegacyStepState::Running),
        ("d", "winpe", LegacyStepState::Failed),
        ("e", "winpe", LegacyStepState::Pending),
    ]
    .into_iter()
    .map(|(id, phase, state)| LegacyPhaseStep {
        step_id: id.into(),
        kind: "synthetic".into(),
        phase: phase.into(),
        state,
    })
    .collect();
    let report = classify_phase("winpe", &steps);
    assert!(!report.phase_complete);
    assert_eq!(
        report
            .incomplete
            .iter()
            .map(|s| s.step_id.as_str())
            .collect::<Vec<_>>(),
        ["b", "e"]
    );
    assert_eq!(report.failed, [&steps[3]]);
    let failed_only = classify_phase("winpe", &steps[3..4]);
    assert!(failed_only.phase_complete);
    assert_eq!(failed_only.failed.len(), 1);
    assert!(classify_phase("winpe", &[]).phase_complete);
}

#[test]
fn legacy_completion_branch_order_retains_reboot_and_retry_distinctions() {
    use LegacyCompletionDisposition as D;
    use LegacyStepState as S;
    use ResultStatus as R;
    for (state, status, reboot, attempt, retries, expected) in [
        (
            S::AwaitingReboot,
            R::Failed,
            false,
            1,
            0,
            D::IgnoreLateFailure,
        ),
        (
            S::AwaitingReboot,
            R::Failed,
            true,
            1,
            4,
            D::IgnoreLateFailure,
        ),
        (S::Running, R::Success, true, 1, 0, D::AwaitReboot),
        (S::Running, R::Success, false, 1, 4, D::Done),
        (S::Running, R::Skipped, true, 1, 4, D::Skipped),
        (S::Running, R::RebootRequired, false, 1, 0, D::AwaitReboot),
        (S::Running, R::Failed, false, 1, 1, D::RetryPending),
        (S::Running, R::Failed, true, 2, 1, D::Failed),
        // Legacy code does not guard all terminal states against another result.
        // Classifying that behavior must not weaken Rust immutable selection.
        (S::Done, R::Failed, false, 1, 1, D::RetryPending),
        (S::Failed, R::Success, false, 2, 0, D::Done),
    ] {
        assert_eq!(
            classify_legacy_completion(state, status, reboot, attempt, retries),
            expected
        );
    }
}
