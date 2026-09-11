//! Later callback readiness only. No satisfied PeRegister proof exists yet.
use crate::{AuthenticatedPeWitnessV1, StartPeArmingError};
use serde::Serialize;
use std::collections::BTreeSet;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PeCompleteScopeV1 {
    version: u8,
    run_id: Uuid,
    registration_operation: Uuid,
    completion_operation: Uuid,
    registration_satisfied_event: Uuid,
    session_id: Uuid,
    opened_unix_micros: u64,
    budget_seconds: u32,
    deadline_unix_micros: u64,
}
impl PeCompleteScopeV1 {
    /// Describes an original PeRegister-Satisfied event; does not certify that
    /// event. A future caller must reconstruct it from authenticated history.
    pub fn new(
        run_id: Uuid,
        registration_operation: Uuid,
        completion_operation: Uuid,
        registration_satisfied_event: Uuid,
        session_id: Uuid,
        opened_unix_micros: u64,
        budget_seconds: u32,
    ) -> Result<Self, StartPeArmingError> {
        if [
            run_id,
            registration_operation,
            completion_operation,
            registration_satisfied_event,
            session_id,
        ]
        .iter()
        .any(Uuid::is_nil)
            || registration_operation == completion_operation
            || opened_unix_micros == 0
            || !(1..=86400).contains(&budget_seconds)
        {
            return Err(StartPeArmingError);
        }
        let deadline_unix_micros = opened_unix_micros
            .checked_add(u64::from(budget_seconds) * 1_000_000)
            .ok_or(StartPeArmingError)?;
        Ok(Self {
            version: 1,
            run_id,
            registration_operation,
            completion_operation,
            registration_satisfied_event,
            session_id,
            opened_unix_micros,
            budget_seconds,
            deadline_unix_micros,
        })
    }
    pub fn deadline_unix_micros(&self) -> u64 {
        self.deadline_unix_micros
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PeCompleteStepStateV1 {
    ReportedComplete,
    ReportedFailed,
    Pending,
    Skipped,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeRegisterPrerequisiteV1 {
    Unavailable,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeCompleteRefusal {
    AuthenticatedRegistrationUnavailable,
    OriginalCompletionDeadlineExpired,
    StepSetMismatch,
    FailedSteps,
    IncompleteSteps,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PeCompleteReportV1 {
    version: u8,
    scope: PeCompleteScopeV1,
    report_id: Uuid,
    revision: u64,
    payload_sha256: String,
    steps: Vec<(Uuid, PeCompleteStepStateV1)>,
}
impl PeCompleteReportV1 {
    pub fn new(
        scope: PeCompleteScopeV1,
        report_id: Uuid,
        revision: u64,
        payload_sha256: String,
        steps: Vec<(Uuid, PeCompleteStepStateV1)>,
    ) -> Result<Self, StartPeArmingError> {
        let mut ids = BTreeSet::new();
        if report_id.is_nil()
            || revision == 0
            || revision > i64::MAX as u64
            || payload_sha256.len() != 64
            || !payload_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || steps.len() > 128
            || steps.iter().any(|(id, _)| id.is_nil() || !ids.insert(*id))
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            version: 1,
            scope,
            report_id,
            revision,
            payload_sha256,
            steps,
        })
    }
    /// Matching reports are never accepted. Required step identities and the
    /// original scope must come from durable deployment history, not this report.
    pub fn assess(
        &self,
        original_scope: &PeCompleteScopeV1,
        required: &[Uuid],
        checked_unix_micros: u64,
        prerequisite: PeRegisterPrerequisiteV1,
        witness: AuthenticatedPeWitnessV1,
    ) -> Result<PeCompleteRefusal, StartPeArmingError> {
        if &self.scope != original_scope || checked_unix_micros < self.scope.opened_unix_micros {
            return Err(StartPeArmingError);
        }
        if checked_unix_micros >= self.scope.deadline_unix_micros {
            return Ok(PeCompleteRefusal::OriginalCompletionDeadlineExpired);
        }
        let expected: BTreeSet<_> = required.iter().copied().collect();
        if required.is_empty()
            || required.len() > 128
            || expected.len() != required.len()
            || expected.iter().any(Uuid::is_nil)
        {
            return Err(StartPeArmingError);
        }
        if expected != self.steps.iter().map(|(id, _)| *id).collect() {
            return Ok(PeCompleteRefusal::StepSetMismatch);
        }
        if self
            .steps
            .iter()
            .any(|(_, state)| *state == PeCompleteStepStateV1::ReportedFailed)
        {
            return Ok(PeCompleteRefusal::FailedSteps);
        }
        if self
            .steps
            .iter()
            .any(|(_, state)| *state != PeCompleteStepStateV1::ReportedComplete)
        {
            return Ok(PeCompleteRefusal::IncompleteSteps);
        }
        match (prerequisite, witness) {
            (PeRegisterPrerequisiteV1::Unavailable, AuthenticatedPeWitnessV1::Unavailable) => {
                Ok(PeCompleteRefusal::AuthenticatedRegistrationUnavailable)
            }
        }
    }
}
