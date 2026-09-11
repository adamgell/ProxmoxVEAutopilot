//! Descriptive shutdown scope only: no authenticated completion or stop authority.
use crate::{AuthenticatedPeWitnessV1, StartPeArmingError};
use serde::Serialize;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PeShutdownScopeV1 {
    version: u8,
    run_id: Uuid,
    completion_operation: Uuid,
    grace_operation: Uuid,
    ensure_stopped_operation: Uuid,
    completion_satisfied_event: Uuid,
    opened_unix_micros: u64,
    budget_seconds: u32,
    deadline_unix_micros: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeShutdownObservationV1 {
    Missing,
    ReportedRunning,
    ReportedStopped,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeShutdownRefusal {
    AuthenticatedCompletionUnavailable,
    GuardedEscalationUnavailable,
}

impl PeShutdownScopeV1 {
    /// Identities are run, PeComplete, PeShutdownGrace, PeEnsureStopped, and
    /// original PeComplete-Satisfied event, respectively. This does not certify
    /// that event or grant permission to stop a VM.
    pub fn new(
        ids: [Uuid; 5],
        opened_unix_micros: u64,
        budget_seconds: u32,
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || ids[1] == ids[2]
            || ids[1] == ids[3]
            || ids[2] == ids[3]
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
            run_id: ids[0],
            completion_operation: ids[1],
            grace_operation: ids[2],
            ensure_stopped_operation: ids[3],
            completion_satisfied_event: ids[4],
            opened_unix_micros,
            budget_seconds,
            deadline_unix_micros,
        })
    }

    /// Original scope must be reconstructed from durable trusted history.
    /// Unauthenticated stopped reports and elapsed grace never authorize effects.
    pub fn assess(
        &self,
        original: &Self,
        checked_unix_micros: u64,
        observation: PeShutdownObservationV1,
        witness: AuthenticatedPeWitnessV1,
    ) -> Result<PeShutdownRefusal, StartPeArmingError> {
        if self != original || checked_unix_micros < self.opened_unix_micros {
            return Err(StartPeArmingError);
        }
        match (observation, witness) {
            (
                PeShutdownObservationV1::Missing
                | PeShutdownObservationV1::ReportedRunning
                | PeShutdownObservationV1::ReportedStopped,
                AuthenticatedPeWitnessV1::Unavailable,
            ) => Ok(if checked_unix_micros >= self.deadline_unix_micros {
                PeShutdownRefusal::GuardedEscalationUnavailable
            } else {
                PeShutdownRefusal::AuthenticatedCompletionUnavailable
            }),
        }
    }
}
