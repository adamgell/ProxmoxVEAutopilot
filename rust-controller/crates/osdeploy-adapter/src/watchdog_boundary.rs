//! Descriptive watchdog report validation; no installation capability.
use crate::StartPeArmingError;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WatchdogBindingV1 {
    run: Uuid,
    verify_qga_operation: Uuid,
    verify_qga_result: Uuid,
    watchdog_operation: Uuid,
    watchdog_attempt: Uuid,
    agent_identity: Uuid,
    verify_receipt_sha256: String,
    verify_result_sha256: String,
    node: String,
    vmid: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WatchdogReportV1 {
    pub version: u8,
    pub binding: WatchdogBindingV1,
    pub claimed_installed: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WatchdogRefusal {
    MissingReport,
    IncompleteReport,
    AuthenticatedPredecessorAndAgentUnavailable,
}
impl WatchdogBindingV1 {
    /// IDs: run, VerifyQga operation/result, watchdog operation/attempt, agent.
    /// Digests: claimed VerifyQga receipt and result byte identities.
    pub fn new(
        ids: [Uuid; 6],
        digests: [String; 2],
        node: String,
        vmid: u32,
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || ids[1] == ids[3]
            || digests.iter().any(|d| {
                d.len() != 64
                    || !d
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            || node.is_empty()
            || node.len() > 256
            || node.trim() != node
            || node.chars().any(char::is_control)
            || vmid == 0
        {
            return Err(StartPeArmingError);
        }
        let [verify_receipt_sha256, verify_result_sha256] = digests;
        Ok(Self {
            run: ids[0],
            verify_qga_operation: ids[1],
            verify_qga_result: ids[2],
            watchdog_operation: ids[3],
            watchdog_attempt: ids[4],
            agent_identity: ids[5],
            verify_receipt_sha256,
            verify_result_sha256,
            node,
            vmid,
        })
    }
    /// Exact expected binding must ultimately be reconstructed from trusted
    /// receipts and agent enrollment. Caller claims cannot supply that trust.
    pub fn assess(
        &self,
        report: Option<&WatchdogReportV1>,
    ) -> Result<WatchdogRefusal, StartPeArmingError> {
        let Some(report) = report else {
            return Ok(WatchdogRefusal::MissingReport);
        };
        if report.version != 1 || &report.binding != self {
            return Err(StartPeArmingError);
        }
        if !report.claimed_installed {
            return Ok(WatchdogRefusal::IncompleteReport);
        }
        Ok(WatchdogRefusal::AuthenticatedPredecessorAndAgentUnavailable)
    }
}
