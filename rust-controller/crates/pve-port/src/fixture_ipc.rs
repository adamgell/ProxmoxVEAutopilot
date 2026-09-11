//! Opt-in local fixture messages, with no socket or production transport capability.
//!
//! Decoding a receipt establishes message binding only. The fixture daemon must
//! separately commit its attempt, task, and receipt before sending acceptance.
//! These messages do not authorize a submission or prove a completed VM effect.

use crate::{CloneProvisioningRequestV1, MutationReceipt, Upid};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

mod stage;
pub use stage::{
    FixtureStageReceipt, FixtureStageRequest, FixtureStopReleaseDispositionV1,
    FixtureStopReleaseProposalV1,
};

const MAX_MESSAGE_BYTES: usize = 65_536;

/// Sealed provisioning fixture with a controller dispatch checkpoint.
///
/// This trait preserves the private mutation seal on `ProvisioningFakePort`.
/// The checkpoint is a synchronization hook, not mutation authority or evidence
/// that a request was accepted. IPC implementations must supply their own
/// supervisor-controlled barrier before they can implement this seam.
///
/// A production observer cannot acquire this fixture capability.
/// ```compile_fail
/// use pve_port::{fixture_ipc::ControllerFixturePort, ReqwestPveObserver};
/// fn observer_is_not_a_fixture(observer: &ReqwestPveObserver) {
///     let _: &dyn ControllerFixturePort = observer;
/// }
/// ```
#[async_trait::async_trait]
pub trait ControllerFixturePort: crate::ProvisioningFakePort {
    /// Return an opaque proof that this port and its checkpoint client refer to
    /// the same supervisor journal channel. `None` is the fail-closed default.
    fn shared_history_provenance(&self) -> Option<FixtureSharedHistoryProvenanceV1> {
        None
    }
    /// Called after durable dispatch with the exact request committed by the scheduler.
    /// Implementations must explicitly opt in; a legacy point-only barrier cannot
    /// authorize an arbitrary provisioning stage.
    async fn provisioning_checkpoint(
        &self,
        _request: &crate::ProvisioningMutationRequestV1,
    ) -> Result<(), CheckpointError> {
        Err(CheckpointError::Rejected)
    }

    async fn controller_checkpoint(
        &self,
        point: crate::FakeControllerCheckpoint,
    ) -> Result<(), CheckpointError>;
}

/// Opaque, operation-scoped join between a controller port and supervisor
/// checkpoint channel. The fields are private so callers cannot manufacture a
/// positive assertion by copying decoded receipt or stage identity fields.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureSharedHistoryProvenanceV1 {
    operation: Uuid,
    generation: Uuid,
    owner: Uuid,
    channel: String,
}

impl FixtureSharedHistoryProvenanceV1 {
    pub fn operation(&self) -> Uuid {
        self.operation
    }

    pub(crate) fn new(
        operation: Uuid,
        generation: Uuid,
        owner: Uuid,
        channel: String,
    ) -> Result<Self, CheckpointError> {
        if operation.is_nil() || generation.is_nil() || owner.is_nil() || channel.is_empty() {
            return Err(CheckpointError::Rejected);
        }
        Ok(Self {
            operation,
            generation,
            owner,
            channel,
        })
    }
}

/// Failure to obtain supervisor release never authorizes dispatch.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum CheckpointError {
    #[error("checkpoint transport unavailable")]
    Unavailable,
    #[error("checkpoint deadline elapsed")]
    TimedOut,
    #[error("checkpoint binding or state rejected")]
    Rejected,
}
impl From<std::io::Error> for CheckpointError {
    fn from(error: std::io::Error) -> Self {
        match error.kind() {
            std::io::ErrorKind::TimedOut => Self::TimedOut,
            std::io::ErrorKind::InvalidData | std::io::ErrorKind::InvalidInput => Self::Rejected,
            _ => Self::Unavailable,
        }
    }
}

#[async_trait::async_trait]
impl ControllerFixturePort for crate::NativeFakePve {
    async fn provisioning_checkpoint(
        &self,
        _request: &crate::ProvisioningMutationRequestV1,
    ) -> Result<(), CheckpointError> {
        self.controller_checkpoint(crate::FakeControllerCheckpoint::DispatchCommitted)
            .await;
        Ok(())
    }

    async fn controller_checkpoint(
        &self,
        point: crate::FakeControllerCheckpoint,
    ) -> Result<(), CheckpointError> {
        crate::NativeFakePve::controller_checkpoint(self, point).await;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("invalid local fixture clone message")]
pub struct InvalidFixtureClone;

/// The fixture identity must survive daemon restart; it is not a worker identity.
/// Only the already validated Clone request can enter this envelope.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureCloneRequest {
    fixture_id: Uuid,
    request: CloneProvisioningRequestV1,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestWire {
    version: u8,
    fixture_id: Uuid,
    request: CloneProvisioningRequestV1,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReceiptWire {
    version: u8,
    fixture_id: Uuid,
    request_sha256: String,
    submission_sequence: u64,
    receipt: MutationReceipt,
}

/// Accepted task identity, checked against the exact original request envelope.
/// No Deserialize implementation bypasses that contextual validation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureCloneReceipt {
    submission_sequence: u64,
    receipt: MutationReceipt,
}

impl FixtureCloneRequest {
    pub fn new(
        fixture_id: Uuid,
        request: CloneProvisioningRequestV1,
    ) -> Result<Self, InvalidFixtureClone> {
        if fixture_id.is_nil() {
            return Err(InvalidFixtureClone);
        }
        Ok(Self {
            fixture_id,
            request,
        })
    }

    pub fn fixture_id(&self) -> Uuid {
        self.fixture_id
    }

    pub fn request(&self) -> &CloneProvisioningRequestV1 {
        &self.request
    }

    fn wire(&self) -> RequestWire {
        RequestWire {
            version: 1,
            fixture_id: self.fixture_id,
            request: self.request.clone(),
        }
    }

    /// Includes full provisioning binding, original attempt and before-state.
    pub fn request_sha256(&self) -> String {
        crate::native::canonical_digest(&self.wire())
    }

    pub fn encode(&self) -> Result<Vec<u8>, InvalidFixtureClone> {
        encode(&self.wire())
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, InvalidFixtureClone> {
        let wire: RequestWire = decode(bytes)?;
        if wire.version != 1 {
            return Err(InvalidFixtureClone);
        }
        Self::new(wire.fixture_id, wire.request)
    }

    /// Daemon-side serialization only; callers must first durably commit this receipt.
    pub fn encode_receipt(
        &self,
        submission_sequence: u64,
        upid: Upid,
    ) -> Result<Vec<u8>, InvalidFixtureClone> {
        self.validate_task(submission_sequence, &upid)?;
        encode(&ReceiptWire {
            version: 1,
            fixture_id: self.fixture_id,
            request_sha256: self.request_sha256(),
            submission_sequence,
            receipt: MutationReceipt::Task(upid),
        })
    }

    pub fn decode_receipt(&self, bytes: &[u8]) -> Result<FixtureCloneReceipt, InvalidFixtureClone> {
        let wire: ReceiptWire = decode(bytes)?;
        if wire.version != 1
            || wire.fixture_id != self.fixture_id
            || wire.request_sha256 != self.request_sha256()
        {
            return Err(InvalidFixtureClone);
        }
        let MutationReceipt::Task(ref upid) = wire.receipt else {
            return Err(InvalidFixtureClone);
        };
        self.validate_task(wire.submission_sequence, upid)?;
        Ok(FixtureCloneReceipt {
            submission_sequence: wire.submission_sequence,
            receipt: wire.receipt,
        })
    }

    fn validate_task(&self, sequence: u64, upid: &Upid) -> Result<(), InvalidFixtureClone> {
        let vm = self.request.clone_request().vm();
        // Match the existing NativeFakePve contract: qmclone names the source VM.
        if sequence == 0
            || upid.node() != vm.node()
            || upid.worker_type() != "qmclone"
            || upid.worker_id() != Some(vm.source_vmid().to_string().as_str())
            || upid.authenticated_user() != "fake@pve"
        {
            return Err(InvalidFixtureClone);
        }
        Ok(())
    }
}

impl FixtureCloneReceipt {
    pub fn submission_sequence(&self) -> u64 {
        self.submission_sequence
    }

    pub fn receipt(&self) -> &MutationReceipt {
        &self.receipt
    }
}

fn encode(value: &impl Serialize) -> Result<Vec<u8>, InvalidFixtureClone> {
    let bytes = serde_json::to_vec(value).map_err(|_| InvalidFixtureClone)?;
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(InvalidFixtureClone);
    }
    Ok(bytes)
}

fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T, InvalidFixtureClone> {
    if bytes.is_empty() || bytes.len() > MAX_MESSAGE_BYTES {
        return Err(InvalidFixtureClone);
    }
    serde_json::from_slice(bytes).map_err(|_| InvalidFixtureClone)
}
