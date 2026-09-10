//! Candidate protocol only: validation does not grant a mutation capability.
//! The daemon intentionally does not route this command until durable supervisor
//! admission and atomic checkpoint release are implemented together.
use super::{CheckpointBinding, CheckpointPhase, CheckpointState, FixtureProvisioningIdentity};
use crate::fixture_ipc::FixtureCloneRequest;
use serde::{Deserialize, Serialize};
use std::io;
use uuid::Uuid;

/// Collection identity deliberately contains no future request digest.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureReadIdentity {
    pub fixture_id: Uuid,
    pub operation: Uuid,
    pub node: String,
    pub source_vmid: u32,
    pub target_vmid: u32,
}
impl FixtureReadIdentity {
    pub fn validate(&self) -> io::Result<()> {
        FixtureProvisioningIdentity {
            fixture_id: self.fixture_id,
            operation: self.operation,
            node: self.node.clone(),
            source_vmid: self.source_vmid,
            target_vmid: self.target_vmid,
            // Reuse identity syntax validation; this value is never transmitted
            // or used to authorize a request.
            request_sha256: "0".repeat(64),
        }
        .validate()
    }
}

/// Versioned supervisor proposal. No worker API consumes this type.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LateCloneAuthorizationV1 {
    pub version: u8,
    pub binding: CheckpointBinding,
    pub identity: FixtureReadIdentity,
    pub request_sha256: String,
    pub request: Vec<u8>,
}
impl LateCloneAuthorizationV1 {
    /// Validate a proposal against independently supplied dispatch bytes and
    /// current barrier state. This performs no persistence or release.
    pub fn validate_candidate(
        &self,
        expected_identity: &FixtureReadIdentity,
        expected_binding: CheckpointBinding,
        barrier: &CheckpointState,
        committed_request: &FixtureCloneRequest,
    ) -> io::Result<()> {
        let invalid = || io::Error::new(io::ErrorKind::InvalidData, "late authorization rejected");
        expected_identity.validate()?;
        let request = FixtureCloneRequest::decode(&self.request).map_err(|_| invalid())?;
        let vm = request.request().clone_request().vm();
        if self.version != 1
            || self.identity != *expected_identity
            || self.binding != expected_binding
            || self.binding.generation.is_nil()
            || self.binding.owner.is_nil()
            || self.binding.operation != expected_identity.operation
            || barrier.generation != self.binding.generation
            || barrier.binding != Some(self.binding)
            || barrier.phase != CheckpointPhase::Entered
            || request != *committed_request
            || self.request_sha256 != request.request_sha256()
            || request.fixture_id() != self.identity.fixture_id
            || request.request().binding().operation_id().as_uuid() != self.identity.operation
            || vm.node().as_str() != self.identity.node
            || vm.source_vmid().get() != self.identity.source_vmid
            || vm.target_vmid().get() != self.identity.target_vmid
        {
            return Err(invalid());
        }
        Ok(())
    }
}
