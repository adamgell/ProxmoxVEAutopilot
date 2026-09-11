//! Read-only descriptive disk boot gate, not mutation authorization.
use crate::{OsDeployStage, StartPeArmingError};
use serde::Serialize;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DiskBootScopeV1 {
    version: u8,
    stage: OsDeployStage,
    run: Uuid,
    predecessor_operation: Uuid,
    operation: Uuid,
    predecessor_satisfied_event: Uuid,
    node: String,
    vmid: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiskBootEvidenceV1 {
    pub node: String,
    pub vmid: u32,
    pub reported_stopped: bool,
    pub disk_volid: String,
    /// Opaque configuration identity; never interpreted as a canonical SHA-256.
    pub configuration_identity: String,
    pub observed_unix_micros: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiskBootRefusal {
    MissingEvidence,
    NotStopped,
    StaleEvidence,
    DurablePredecessorAndAuthorityUnavailable,
}
fn token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value.trim() == value
        && !value.chars().any(char::is_control)
}
impl DiskBootScopeV1 {
    /// IDs: run, predecessor operation, current operation, predecessor event.
    /// ConfigureDisk requires PeEnsureStopped; StartDisk requires ConfigureDisk.
    /// These identities are claims until reconstructed from durable history.
    pub fn new(
        stage: OsDeployStage,
        ids: [Uuid; 4],
        node: String,
        vmid: u32,
    ) -> Result<Self, StartPeArmingError> {
        if !matches!(
            stage,
            OsDeployStage::ConfigureDisk | OsDeployStage::StartDisk
        ) || ids.iter().any(Uuid::is_nil)
            || ids[1] == ids[2]
            || !token(&node)
            || vmid == 0
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            version: 1,
            stage,
            run: ids[0],
            predecessor_operation: ids[1],
            operation: ids[2],
            predecessor_satisfied_event: ids[3],
            node,
            vmid,
        })
    }
    pub fn assess(
        &self,
        original: &Self,
        evidence: Option<&DiskBootEvidenceV1>,
        checked_unix_micros: u64,
        freshness_micros: u64,
    ) -> Result<DiskBootRefusal, StartPeArmingError> {
        if self != original || freshness_micros == 0 || freshness_micros > 300_000_000 {
            return Err(StartPeArmingError);
        }
        let Some(e) = evidence else {
            return Ok(DiskBootRefusal::MissingEvidence);
        };
        if e.node != self.node
            || e.vmid != self.vmid
            || !token(&e.disk_volid)
            || !token(&e.configuration_identity)
            || e.observed_unix_micros == 0
            || checked_unix_micros < e.observed_unix_micros
        {
            return Err(StartPeArmingError);
        }
        if checked_unix_micros - e.observed_unix_micros > freshness_micros {
            return Ok(DiskBootRefusal::StaleEvidence);
        }
        if !e.reported_stopped {
            return Ok(DiskBootRefusal::NotStopped);
        }
        // Volume ownership, accepted predecessor, fence, and independent evidence
        // are not established by a caller-supplied stopped/configuration claim.
        Ok(DiskBootRefusal::DurablePredecessorAndAuthorityUnavailable)
    }
}
