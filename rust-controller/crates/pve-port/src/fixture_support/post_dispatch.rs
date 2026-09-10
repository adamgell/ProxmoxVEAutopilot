//! Bound post-dispatch facts. Decoding validates provenance, never decides success.
use super::{
    FixtureProvisioningIdentity, FixtureProvisioningReads, FixtureTaskObservation, SeedRead,
};
use crate::{MutationReceipt, fixture_ipc::FixtureCloneRequest};
use serde::{Deserialize, Serialize};
use std::io;

pub const MAX_POST_DISPATCH_BYTES: usize = 131_072;

/// A supervisor observation associated with an already accepted daemon effect.
/// The accepted receipt and time bounds are supplied independently by the
/// consumer; bytes inside this document cannot establish their own authority.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturePostDispatchV1 {
    pub version: u8,
    pub task: FixtureTaskObservation,
    pub provisioning: FixtureProvisioningReads,
    pub inventory: super::FixtureCloneReads,
}

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid post-dispatch observation",
    )
}

fn timestamp<T>(read: &SeedRead<T>) -> u64 {
    match read {
        SeedRead::Observed {
            observed_unix_ms, ..
        }
        | SeedRead::Error {
            observed_unix_ms, ..
        } => *observed_unix_ms,
    }
}

impl FixturePostDispatchV1 {
    /// Validate exact request, receipt, VM identities and every observation time.
    /// Error/absence/partial-coverage observations remain explicit. This method
    /// never interprets task completion as proof of the desired VM postcondition.
    pub fn decode(
        bytes: &[u8],
        request: &FixtureCloneRequest,
        accepted_receipt: &[u8],
        accepted_unix_ms: u64,
        now_unix_ms: u64,
    ) -> io::Result<Self> {
        if bytes.is_empty()
            || bytes.len() > MAX_POST_DISPATCH_BYTES
            || accepted_unix_ms == 0
            || accepted_unix_ms > now_unix_ms
            || i64::try_from(now_unix_ms)
                .ok()
                .and_then(chrono::DateTime::from_timestamp_millis)
                .is_none()
        {
            return Err(invalid());
        }
        let result: Self = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        let receipt = request
            .decode_receipt(accepted_receipt)
            .map_err(|_| invalid())?;
        let MutationReceipt::Task(upid) = receipt.receipt() else {
            return Err(invalid());
        };
        let vm = request.request().clone_request().vm();
        let identity = FixtureProvisioningIdentity {
            fixture_id: request.fixture_id(),
            operation: request.request().binding().operation_id().as_uuid(),
            request_sha256: request.request_sha256(),
            node: vm.node().as_str().to_owned(),
            source_vmid: vm.source_vmid().get(),
            target_vmid: vm.target_vmid().get(),
        };
        if result.version != 1
            || result.task.identity.fixture_id != identity.fixture_id
            || result.task.identity.operation != identity.operation
            || result.task.identity.request_sha256 != identity.request_sha256
            || result.task.identity.node != identity.node
            || result.task.identity.upid != upid.as_str()
            || result.inventory.node != identity.node
        {
            return Err(invalid());
        }
        FixtureTaskObservation::decode(&serde_json::to_vec(&result.task).map_err(|_| invalid())?)?;
        FixtureProvisioningReads::decode(
            &serde_json::to_vec(&result.provisioning).map_err(|_| invalid())?,
            &identity,
        )?;
        super::FixtureCloneReads::decode(
            &serde_json::to_vec(&result.inventory).map_err(|_| invalid())?,
            identity.fixture_id,
        )?;
        let p = &result.provisioning;
        let i = &result.inventory;
        let times = [
            result.task.observed_unix_ms,
            timestamp(&p.source_config),
            timestamp(&p.target_config),
            timestamp(&p.source_power),
            timestamp(&p.target_power),
            timestamp(&p.source_coverage),
            timestamp(&p.target_coverage),
            timestamp(&p.deployment_media),
            timestamp(&p.driver_media),
            timestamp(&i.node_status),
            timestamp(&i.storage),
            timestamp(&i.bridges),
            timestamp(&i.cluster_inventory),
        ];
        if times
            .into_iter()
            .any(|time| time < accepted_unix_ms || time > now_unix_ms)
        {
            return Err(invalid());
        }
        Ok(result)
    }
}
