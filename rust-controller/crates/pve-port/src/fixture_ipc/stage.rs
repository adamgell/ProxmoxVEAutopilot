//! Message validation through PeEnsureStopped. A stop message binds physical
//! history only; shutdown-grace adjudication remains scheduler-owned. The daemon
//! refuses stop dispatch until durable stop admission and observation exist.
use super::{InvalidFixtureClone, ReceiptWire, decode, encode};
use crate::{MutationReceipt, ProvisioningActionV1, ProvisioningMutationRequestV1};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Immutable proposal for the later supervisor-owned stop release/send step.
/// It carries all evidence joins required by that step but has no send or
/// release capability itself.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureStopReleaseProposalV1 {
    operation: Uuid,
    attempt: Uuid,
    lease_owner: Uuid,
    generation: Uuid,
    request_sha256: String,
    receipt_sha256: String,
    sample_sha256: String,
    provenance: crate::fixture_ipc::FixtureSharedHistoryProvenanceV1,
}

impl FixtureStopReleaseProposalV1 {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        operation: Uuid,
        attempt: Uuid,
        lease_owner: Uuid,
        generation: Uuid,
        request_sha256: String,
        receipt_sha256: String,
        sample_sha256: String,
        provenance: crate::fixture_ipc::FixtureSharedHistoryProvenanceV1,
    ) -> Result<Self, InvalidFixtureClone> {
        if [operation, attempt, lease_owner, generation]
            .iter()
            .any(Uuid::is_nil)
            || request_sha256.is_empty()
            || receipt_sha256.is_empty()
            || sample_sha256.is_empty()
            || provenance.operation() != operation
        {
            return Err(InvalidFixtureClone);
        }
        Ok(Self {
            operation,
            attempt,
            lease_owner,
            generation,
            request_sha256,
            receipt_sha256,
            sample_sha256,
            provenance,
        })
    }

    pub fn operation(&self) -> Uuid {
        self.operation
    }
    pub fn attempt(&self) -> Uuid {
        self.attempt
    }
    pub fn lease_owner(&self) -> Uuid {
        self.lease_owner
    }
    pub fn generation(&self) -> Uuid {
        self.generation
    }
    pub fn request_sha256(&self) -> &str {
        &self.request_sha256
    }
    pub fn receipt_sha256(&self) -> &str {
        &self.receipt_sha256
    }
    pub fn sample_sha256(&self) -> &str {
        &self.sample_sha256
    }
    pub fn provenance(&self) -> &crate::fixture_ipc::FixtureSharedHistoryProvenanceV1 {
        &self.provenance
    }
}

#[cfg(test)]
mod stop_release_proposal_tests {
    use super::*;

    #[test]
    fn proposal_requires_matching_operation_and_nonempty_digests() {
        let operation = Uuid::now_v7();
        let provenance = crate::fixture_ipc::FixtureSharedHistoryProvenanceV1::new(
            operation,
            Uuid::now_v7(),
            Uuid::now_v7(),
            "/tmp/fixture-stop.sock".to_owned(),
        )
        .unwrap();
        let proposal = FixtureStopReleaseProposalV1::new(
            operation,
            Uuid::now_v7(),
            Uuid::now_v7(),
            Uuid::now_v7(),
            "request".to_owned(),
            "receipt".to_owned(),
            "sample".to_owned(),
            provenance.clone(),
        )
        .unwrap();
        assert_eq!(proposal.operation(), operation);
        assert_eq!(proposal.provenance(), &provenance);
        assert!(
            FixtureStopReleaseProposalV1::new(
                Uuid::now_v7(),
                Uuid::now_v7(),
                Uuid::now_v7(),
                Uuid::now_v7(),
                "request".to_owned(),
                "receipt".to_owned(),
                "sample".to_owned(),
                provenance,
            )
            .is_err()
        );
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureStageRequest {
    fixture_id: Uuid,
    request: ProvisioningMutationRequestV1,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StageWire {
    version: u8,
    fixture_id: Uuid,
    request: ProvisioningMutationRequestV1,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureStageReceipt {
    sequence: u64,
    receipt: MutationReceipt,
}

impl FixtureStageRequest {
    pub fn new(
        fixture_id: Uuid,
        request: ProvisioningMutationRequestV1,
    ) -> Result<Self, InvalidFixtureClone> {
        use ProvisioningActionV1::*;
        if fixture_id.is_nil()
            || !matches!(
                request.plan().action(),
                Clone | EnsureCapacity | ConfigurePe | StartPe | EnsureStopped
            )
        {
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
    pub fn request(&self) -> &ProvisioningMutationRequestV1 {
        &self.request
    }
    /// Validate the exact ConfigurePe predecessor carried by a StartPe request.
    /// Receipt decoding is structural; the daemon must independently establish
    /// durable acceptance before any future power transition can be authorized.
    pub fn validate_start_pe_predecessor(
        &self,
        predecessor: &Self,
        receipt: &[u8],
    ) -> Result<(), InvalidFixtureClone> {
        let ProvisioningMutationRequestV1::Start(start) = &self.request else {
            return Err(InvalidFixtureClone);
        };
        let ProvisioningMutationRequestV1::Configure(configure) = predecessor.request() else {
            return Err(InvalidFixtureClone);
        };
        if self.request.plan().action() != crate::ProvisioningActionV1::StartPe
            || predecessor.request.plan().action() != crate::ProvisioningActionV1::ConfigurePe
            || self.fixture_id != predecessor.fixture_id
            || !predecessor
                .request
                .binding()
                .same_operation_attempt(start.predecessor_binding())
            || predecessor.request.plan() != start.predecessor_plan()
            || !configure
                .clone_binding()
                .same_operation_attempt(start.clone_binding())
            || predecessor.request.plan().expected() != self.request.plan().expected()
            || predecessor.decode_receipt(receipt)?.receipt()
                != &MutationReceipt::SynchronousAccepted
        {
            return Err(InvalidFixtureClone);
        }
        Ok(())
    }
    /// Check a StartPe request and receipt against the stop's physical-history
    /// binding. This does not prove durable acceptance, StartPe completion,
    /// elapsed shutdown grace, current lease ownership, or a stopped VM.
    /// Those are separate runtime gates.
    pub fn validate_ensure_stopped_physical_predecessor(
        &self,
        predecessor: &Self,
        receipt: &[u8],
    ) -> Result<(), InvalidFixtureClone> {
        let ProvisioningMutationRequestV1::Stop(stop) = &self.request else {
            return Err(InvalidFixtureClone);
        };
        let ProvisioningMutationRequestV1::Start(start) = predecessor.request() else {
            return Err(InvalidFixtureClone);
        };
        if self.request.plan().action() != crate::ProvisioningActionV1::EnsureStopped
            || predecessor.request.plan().action() != crate::ProvisioningActionV1::StartPe
            || self.fixture_id != predecessor.fixture_id
            || !predecessor
                .request
                .binding()
                .same_operation_attempt(stop.predecessor_binding())
            || predecessor.request.plan() != stop.predecessor_plan()
            || !start
                .clone_binding()
                .same_operation_attempt(stop.clone_binding())
            || predecessor.request.plan().expected() != self.request.plan().expected()
            || !matches!(
                predecessor.decode_receipt(receipt)?.receipt(),
                MutationReceipt::Task(_)
            )
        {
            return Err(InvalidFixtureClone);
        }
        Ok(())
    }
    fn wire(&self) -> StageWire {
        StageWire {
            version: 2,
            fixture_id: self.fixture_id,
            request: self.request.clone(),
        }
    }
    pub fn request_sha256(&self) -> String {
        crate::native::canonical_digest(&self.wire())
    }
    pub fn encode(&self) -> Result<Vec<u8>, InvalidFixtureClone> {
        encode(&self.wire())
    }
    pub fn decode(bytes: &[u8]) -> Result<Self, InvalidFixtureClone> {
        let wire: StageWire = decode(bytes)?;
        if wire.version != 2 {
            return Err(InvalidFixtureClone);
        }
        Self::new(wire.fixture_id, wire.request)
    }
    fn validate_receipt(
        &self,
        sequence: u64,
        receipt: &MutationReceipt,
    ) -> Result<(), InvalidFixtureClone> {
        use ProvisioningActionV1::*;
        if sequence == 0 {
            return Err(InvalidFixtureClone);
        }
        let action = self.request.plan().action();
        let vm = self.request.plan().expected().vm();
        match (action, receipt) {
            (ConfigurePe, MutationReceipt::SynchronousAccepted) => Ok(()),
            (Clone | EnsureCapacity | StartPe | EnsureStopped, MutationReceipt::Task(upid)) => {
                let (kind, vmid) = if action == Clone {
                    ("qmclone", vm.source_vmid())
                } else if action == EnsureCapacity {
                    ("resize", vm.target_vmid())
                } else if action == EnsureStopped {
                    ("qmstop", vm.target_vmid())
                } else {
                    ("qmstart", vm.target_vmid())
                };
                if upid.node() == vm.node()
                    && upid.worker_type() == kind
                    && upid.worker_id() == Some(vmid.to_string().as_str())
                    && upid.authenticated_user() == "fake@pve"
                {
                    Ok(())
                } else {
                    Err(InvalidFixtureClone)
                }
            }
            _ => Err(InvalidFixtureClone),
        }
    }
    /// Serialization is not persistence; the daemon must commit acceptance first.
    pub fn encode_receipt(
        &self,
        sequence: u64,
        receipt: MutationReceipt,
    ) -> Result<Vec<u8>, InvalidFixtureClone> {
        self.validate_receipt(sequence, &receipt)?;
        encode(&ReceiptWire {
            version: 2,
            fixture_id: self.fixture_id,
            request_sha256: self.request_sha256(),
            submission_sequence: sequence,
            receipt,
        })
    }
    pub fn decode_receipt(&self, bytes: &[u8]) -> Result<FixtureStageReceipt, InvalidFixtureClone> {
        let wire: ReceiptWire = decode(bytes)?;
        if wire.version != 2
            || wire.fixture_id != self.fixture_id
            || wire.request_sha256 != self.request_sha256()
        {
            return Err(InvalidFixtureClone);
        }
        self.validate_receipt(wire.submission_sequence, &wire.receipt)?;
        Ok(FixtureStageReceipt {
            sequence: wire.submission_sequence,
            receipt: wire.receipt,
        })
    }
}
impl FixtureStageReceipt {
    pub fn submission_sequence(&self) -> u64 {
        self.sequence
    }
    pub fn receipt(&self) -> &MutationReceipt {
        &self.receipt
    }
}
