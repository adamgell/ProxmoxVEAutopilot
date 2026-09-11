//! Pure compatibility decisions, without HTTP, credentials or persistence.
//!
//! Legacy result bodies have no Rust attempt identifier. Callers must obtain
//! `ResultBinding` from authenticated durable exposure, never from agent input.
//! These functions do not establish that authentication or activate any stage.
use controller_domain::{AttemptId, OperationId};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ResultBinding {
    pub operation: OperationId,
    pub attempt: AttemptId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResultStatus {
    Success,
    Failed,
    Skipped,
    RebootRequired,
}

/// Normalized semantic payload; bearer rotation and transport timestamps do not
/// belong here. Message/data changes remain conflicts, even with equal status.
#[derive(Clone, Debug, PartialEq)]
pub struct ResultCandidate {
    pub binding: ResultBinding,
    pub status: ResultStatus,
    pub message: Option<String>,
    pub data: Value,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResultDisposition {
    /// No prior result exists. The caller must still validate admissibility and
    /// atomically compare/insert under the durable exposure/authority fence.
    FirstCandidate,
    EquivalentReplay,
    Conflict,
    BindingMismatch,
}

/// Compare with the original accepted result. This does not select a result or
/// supply an HTTP response policy. In particular, Failed replay stays Failed.
pub fn classify_result(
    expected: ResultBinding,
    original: Option<&ResultCandidate>,
    incoming: &ResultCandidate,
) -> ResultDisposition {
    if incoming.binding != expected || original.is_some_and(|first| first.binding != expected) {
        return ResultDisposition::BindingMismatch;
    }
    match original {
        None => ResultDisposition::FirstCandidate,
        Some(first) if first == incoming => ResultDisposition::EquivalentReplay,
        Some(_) => ResultDisposition::Conflict,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyStepState {
    Pending,
    Running,
    AwaitingReboot,
    Failed,
    Done,
    Skipped,
}

/// Source-derived legacy completion advice for compatibility fixtures only.
/// This does not select a Rust result, schedule a retry, or prove registration.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyCompletionDisposition {
    IgnoreLateFailure,
    AwaitReboot,
    Done,
    Skipped,
    RetryPending,
    Failed,
}

/// Mirrors the ordered status branches in Python `ts_engine_pg.complete_step`.
/// Inputs are already-normalized fixture values. Run continuation and HTTP
/// validation are deliberately outside this step-state classification.
pub fn classify_legacy_completion(
    state: LegacyStepState,
    status: ResultStatus,
    reboot_required: bool,
    attempt: u32,
    retry_count: u32,
) -> LegacyCompletionDisposition {
    use LegacyCompletionDisposition as D;
    match status {
        ResultStatus::Failed if state == LegacyStepState::AwaitingReboot => D::IgnoreLateFailure,
        ResultStatus::Success if reboot_required => D::AwaitReboot,
        ResultStatus::Success => D::Done,
        ResultStatus::Skipped => D::Skipped,
        ResultStatus::RebootRequired => D::AwaitReboot,
        ResultStatus::Failed if attempt <= retry_count => D::RetryPending,
        ResultStatus::Failed => D::Failed,
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LegacyPhaseStep {
    pub step_id: String,
    pub kind: String,
    pub phase: String,
    pub state: LegacyStepState,
}

/// Mirrors the legacy report while retaining failure information that its
/// `phase_complete` boolean omits. Neither field proves Rust PeComplete.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LegacyPhaseReport<'a> {
    pub phase_complete: bool,
    pub incomplete: Vec<&'a LegacyPhaseStep>,
    pub failed: Vec<&'a LegacyPhaseStep>,
}

pub fn classify_phase<'a>(phase: &str, steps: &'a [LegacyPhaseStep]) -> LegacyPhaseReport<'a> {
    let selected = || {
        steps
            .iter()
            .filter(|step| step.phase == phase || step.phase == "any")
    };
    let incomplete: Vec<_> = selected()
        .filter(|step| {
            matches!(
                step.state,
                LegacyStepState::Pending
                    | LegacyStepState::Running
                    | LegacyStepState::AwaitingReboot
            )
        })
        .collect();
    let failed = selected()
        .filter(|step| step.state == LegacyStepState::Failed)
        .collect();
    LegacyPhaseReport {
        phase_complete: incomplete.is_empty(),
        incomplete,
        failed,
    }
}
