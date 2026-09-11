//! OSDeploy authority is reconstructed within locked transactions.
use super::*;
use crate::OsDeployExecutionError as Error;
use crate::OsDeployOperationSnapshot;
use crate::osdeploy::execution::{load, wire};
use osdeploy_adapter::OsDeployStage;
use sqlx::Row;

mod decision;
mod discovery;
#[cfg(feature = "fixture-ipc")]
pub(crate) mod fixture_completion;
#[cfg(feature = "fixture-ipc")]
mod fixture_credential;
#[cfg(feature = "fixture-ipc")]
pub use fixture_completion::{
    FixtureBootFilesStagedResult, FixturePeCompletionReport, FixturePeCompletionResult,
};
#[cfg(feature = "fixture-ipc")]
mod fixture_delivery;
#[cfg(feature = "fixture-ipc")]
pub(crate) mod fixture_registration;
#[cfg(feature = "fixture-ipc")]
mod fixture_stop;
#[cfg(feature = "fixture-ipc")]
mod fixture_stop_outbox;
#[cfg(feature = "fixture-ipc")]
pub use fixture_delivery::{
    FixtureCredentialEnvelope, FixtureCredentialSink, FixtureDeliveryAck, FixtureDeliveryRecovery,
};
#[cfg(feature = "fixture-ipc")]
pub use fixture_registration::{FixturePeRegistrationIdentity, FixturePeRegistrationResult};
mod lifecycle;
mod pve;
mod receipt;
mod recovery;
pub use receipt::{OsDeployDispatchPermit, OsDeployResponseCapture};
mod transaction;
mod transition;
use transaction::*;
use transition::*;

#[derive(Default, Debug)]
pub struct OsDeployMaintenanceSummary {
    examined: u32,
    changed: u32,
    rejected: u32,
}
impl OsDeployMaintenanceSummary {
    pub fn examined(&self) -> u32 {
        self.examined
    }
    pub fn changed(&self) -> u32 {
        self.changed
    }
    pub fn rejected(&self) -> u32 {
        self.rejected
    }
}

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
