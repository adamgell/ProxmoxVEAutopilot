//! OSDeploy authority is reconstructed within locked transactions.
use super::*;
use crate::OsDeployExecutionError as Error;
use crate::OsDeployOperationSnapshot;
use crate::osdeploy::execution::{load, wire};
use osdeploy_adapter::OsDeployStage;
use sqlx::Row;

mod decision;
mod lifecycle;
mod pve;
mod receipt;
pub use receipt::{OsDeployDispatchPermit, OsDeployResponseCapture};
mod transaction;
mod transition;
use transaction::*;
use transition::*;

/// A DB-time observation, never a transition or dispatch capability.
///
/// ```compile_fail
/// let _ = postgres_store::scheduler::osdeploy::transition::OsDeployTransitionProof::OriginalDispatchReconciliation;
/// ```
pub struct OsDeployLeaseStatus {
    grant: LeaseGrant,
    revision: i64,
    checked_at: DateTime<Utc>,
}
impl OsDeployLeaseStatus {
    pub fn grant(&self) -> &LeaseGrant {
        &self.grant
    }
    pub fn revision(&self) -> i64 {
        self.revision
    }
    pub fn checked_at(&self) -> DateTime<Utc> {
        self.checked_at
    }
    pub fn remaining(&self) -> std::time::Duration {
        (self.grant.lease_expires_at.min(self.grant.deadline_at) - self.checked_at)
            .to_std()
            .unwrap_or_default()
    }
}
