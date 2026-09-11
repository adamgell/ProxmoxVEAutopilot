//! Aggregate operational claims: shape checks, never workflow acceptance.
use crate::{HeartbeatBindingV1, StartPeArmingError};
use uuid::Uuid;
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationalBindingV1 {
    heartbeat: HeartbeatBindingV1,
    operation: Uuid,
    attempt: Uuid,
    heartbeat_receipt_sha256: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationalReportV1 {
    pub version: u8,
    pub binding: OperationalBindingV1,
    pub observed_unix_micros: u64,
    pub heartbeat_sequence: u64,
    /// Ordered claims: host QGA responsive, expected package installed,
    /// expected service running, remaining deployment postconditions met.
    /// None means missing, false contradicts the desired postcondition.
    pub postconditions: [Option<bool>; 4],
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationalRefusal {
    MissingReport,
    IncompletePostconditions,
    ContradictoryPostconditions,
    StaleReport,
    AuthenticatedOperationalEvidenceUnavailable,
}
impl OperationalBindingV1 {
    /// Nested heartbeat binding retains exact run/VM/agent/session/package
    /// identity claims. IDs here are VerifyOperational operation and attempt.
    pub fn new(
        heartbeat: HeartbeatBindingV1,
        ids: [Uuid; 2],
        heartbeat_receipt_sha256: String,
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || heartbeat_receipt_sha256.len() != 64
            || !heartbeat_receipt_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            heartbeat,
            operation: ids[0],
            attempt: ids[1],
            heartbeat_receipt_sha256,
        })
    }
    /// Sequence and original binding are claims until reconstructed from trusted
    /// accepted history. Complete booleans cannot authenticate their sources.
    pub fn assess(
        &self,
        report: Option<&OperationalReportV1>,
        expected_sequence: u64,
        checked_unix_micros: u64,
        freshness_micros: u64,
    ) -> Result<OperationalRefusal, StartPeArmingError> {
        if expected_sequence == 0
            || expected_sequence > i64::MAX as u64
            || freshness_micros == 0
            || freshness_micros > 300_000_000
        {
            return Err(StartPeArmingError);
        }
        let Some(report) = report else {
            return Ok(OperationalRefusal::MissingReport);
        };
        if report.version != 1
            || &report.binding != self
            || report.heartbeat_sequence != expected_sequence
            || report.observed_unix_micros == 0
            || checked_unix_micros < report.observed_unix_micros
        {
            return Err(StartPeArmingError);
        }
        if checked_unix_micros - report.observed_unix_micros > freshness_micros {
            return Ok(OperationalRefusal::StaleReport);
        }
        if report.postconditions.contains(&Some(false)) {
            return Ok(OperationalRefusal::ContradictoryPostconditions);
        }
        if report.postconditions.contains(&None) {
            return Ok(OperationalRefusal::IncompletePostconditions);
        }
        Ok(OperationalRefusal::AuthenticatedOperationalEvidenceUnavailable)
    }
}
