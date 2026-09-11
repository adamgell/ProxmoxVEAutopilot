//! Descriptive InstallQga boundary; host responsiveness is not agent authentication.
use crate::StartPeArmingError;
use serde::Serialize;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct InstallQgaScopeV1 {
    version: u8,
    run: Uuid,
    start_disk_operation: Uuid,
    install_qga_operation: Uuid,
    attempt: Uuid,
    start_disk_satisfied_event: Uuid,
    node: String,
    vmid: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HostQgaClaimV1 {
    pub node: String,
    pub vmid: u32,
    pub responsive: bool,
    pub observed_unix_micros: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallQgaRefusal {
    MissingHostEvidence,
    HostQgaUnavailable,
    AuthenticatedPredecessorAndAgentUnavailable,
}
impl InstallQgaScopeV1 {
    /// IDs: run, StartDisk, InstallQga, attempt, StartDisk satisfaction event.
    /// None of these caller-supplied claims certify a durable predecessor.
    pub fn new(ids: [Uuid; 5], node: String, vmid: u32) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || ids[1] == ids[2]
            || node.is_empty()
            || node.len() > 256
            || node.trim() != node
            || node.chars().any(char::is_control)
            || vmid == 0
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            version: 1,
            run: ids[0],
            start_disk_operation: ids[1],
            install_qga_operation: ids[2],
            attempt: ids[3],
            start_disk_satisfied_event: ids[4],
            node,
            vmid,
        })
    }
    /// Read-only diagnostics. No success variant and no verified host evidence
    /// can be constructed here; a responsive claim is never a signed agent result.
    pub fn assess(
        &self,
        original: &Self,
        claim: Option<&HostQgaClaimV1>,
        checked_unix_micros: u64,
    ) -> Result<InstallQgaRefusal, StartPeArmingError> {
        if self != original {
            return Err(StartPeArmingError);
        }
        let Some(claim) = claim else {
            return Ok(InstallQgaRefusal::MissingHostEvidence);
        };
        if claim.node != self.node
            || claim.vmid != self.vmid
            || claim.observed_unix_micros == 0
            || checked_unix_micros < claim.observed_unix_micros
        {
            return Err(StartPeArmingError);
        }
        if !claim.responsive {
            return Ok(InstallQgaRefusal::HostQgaUnavailable);
        }
        Ok(InstallQgaRefusal::AuthenticatedPredecessorAndAgentUnavailable)
    }
}
