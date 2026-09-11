//! Heartbeat shape/freshness diagnostics, never authenticated acceptance.
use crate::{AuthenticatedPeWitnessV1, InstallAgentBindingV1, StartPeArmingError};
use uuid::Uuid;
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HeartbeatBindingV1 {
    install: InstallAgentBindingV1,
    operation: Uuid,
    attempt: Uuid,
    session: Uuid,
    install_receipt_sha256: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HeartbeatReportV1 {
    pub version: u8,
    pub binding: HeartbeatBindingV1,
    pub sequence: u64,
    pub observed_unix_micros: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HeartbeatRefusal {
    MissingReport,
    ReplayOrOutOfOrder,
    StaleReport,
    AuthenticatedAgentUnavailable,
}
impl HeartbeatBindingV1 {
    /// IDs: heartbeat operation, attempt, agent session. Nested installation
    /// retains VM/agent and artifact claims, not authenticated identities.
    pub fn new(
        install: InstallAgentBindingV1,
        ids: [Uuid; 3],
        install_receipt_sha256: String,
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || install_receipt_sha256.len() != 64
            || !install_receipt_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            install,
            operation: ids[0],
            attempt: ids[1],
            session: ids[2],
            install_receipt_sha256,
        })
    }
    /// Caller supplies the original trusted binding and durable last sequence
    /// in a future integration. This method neither persists nor accepts either.
    pub fn assess(
        &self,
        report: Option<&HeartbeatReportV1>,
        last_sequence: u64,
        checked_unix_micros: u64,
        freshness_micros: u64,
        witness: AuthenticatedPeWitnessV1,
    ) -> Result<HeartbeatRefusal, StartPeArmingError> {
        if freshness_micros == 0
            || freshness_micros > 300_000_000
            || last_sequence > i64::MAX as u64
        {
            return Err(StartPeArmingError);
        }
        let Some(report) = report else {
            return Ok(HeartbeatRefusal::MissingReport);
        };
        if report.version != 1
            || &report.binding != self
            || report.sequence == 0
            || report.sequence > i64::MAX as u64
            || report.observed_unix_micros == 0
            || checked_unix_micros < report.observed_unix_micros
        {
            return Err(StartPeArmingError);
        }
        if report.sequence <= last_sequence {
            return Ok(HeartbeatRefusal::ReplayOrOutOfOrder);
        }
        if checked_unix_micros - report.observed_unix_micros > freshness_micros {
            return Ok(HeartbeatRefusal::StaleReport);
        }
        match witness {
            AuthenticatedPeWitnessV1::Unavailable => {
                Ok(HeartbeatRefusal::AuthenticatedAgentUnavailable)
            }
        }
    }
}
