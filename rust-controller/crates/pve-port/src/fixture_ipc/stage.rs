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

/// Outcome classification for the future supervisor release/send boundary.
/// `Ambiguous` must be reconciled before retry; it is never a success claim.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FixtureStopReleaseDispositionV1 {
    Accepted,
    Refused,
    Ambiguous,
}

impl FixtureStopReleaseDispositionV1 {
    pub fn from_checkpoint_result(result: Result<bool, super::CheckpointError>) -> Self {
        match result {
            Ok(true) => Self::Accepted,
            Ok(false) | Err(super::CheckpointError::Rejected) => Self::Refused,
            Err(super::CheckpointError::Unavailable | super::CheckpointError::TimedOut) => {
                Self::Ambiguous
            }
        }
    }
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
            || !valid_sha256(&request_sha256)
            || !valid_sha256(&receipt_sha256)
            || !valid_sha256(&sample_sha256)
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

    /// Canonical digest of the private, sealed shared-history identity.
    pub fn provenance_sha256(&self) -> String {
        self.provenance.sha256()
    }

    /// Compare the proposal with independently recomputed evidence digests.
    /// This is intentionally a pure equality check; it cannot authorize a
    /// release or physical submission by itself.
    pub fn matches_evidence_digests(
        &self,
        request_sha256: &str,
        receipt_sha256: &str,
        sample_sha256: &str,
    ) -> bool {
        valid_sha256(request_sha256)
            && valid_sha256(receipt_sha256)
            && valid_sha256(sample_sha256)
            && self.request_sha256 == request_sha256
            && self.receipt_sha256 == receipt_sha256
            && self.sample_sha256 == sample_sha256
    }
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
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
            "a".repeat(64),
            "b".repeat(64),
            "c".repeat(64),
            provenance.clone(),
        )
        .unwrap();
        assert_eq!(proposal.operation(), operation);
        assert!(!proposal.attempt().is_nil());
        assert!(!proposal.lease_owner().is_nil());
        assert!(!proposal.generation().is_nil());
        assert_eq!(proposal.request_sha256(), "a".repeat(64));
        assert_eq!(proposal.receipt_sha256(), "b".repeat(64));
        assert_eq!(proposal.sample_sha256(), "c".repeat(64));
        assert_eq!(proposal.provenance(), &provenance);
        assert_eq!(proposal.provenance_sha256(), provenance.sha256());
        assert_eq!(provenance.sha256().len(), 64);
        let other_channel = crate::fixture_ipc::FixtureSharedHistoryProvenanceV1::new(
            operation,
            provenance.generation,
            provenance.owner,
            "/tmp/other-stop.sock".to_owned(),
        )
        .unwrap();
        assert_ne!(provenance.sha256(), other_channel.sha256());
        assert!(proposal.matches_evidence_digests(
            &"a".repeat(64),
            &"b".repeat(64),
            &"c".repeat(64),
        ));
        assert!(!proposal.matches_evidence_digests(
            &"d".repeat(64),
            &"b".repeat(64),
            &"c".repeat(64),
        ));
        assert!(
            FixtureStopReleaseProposalV1::new(
                Uuid::now_v7(),
                Uuid::now_v7(),
                Uuid::now_v7(),
                Uuid::now_v7(),
                "a".repeat(64),
                "b".repeat(64),
                "c".repeat(64),
                provenance,
            )
            .is_err()
        );

        for (request, receipt, sample) in [
            (String::new(), "b".repeat(64), "c".repeat(64)),
            ("a".repeat(64), String::new(), "c".repeat(64)),
            ("a".repeat(64), "b".repeat(64), String::new()),
            ("a".repeat(63), "b".repeat(64), "c".repeat(64)),
            ("A".repeat(64), "b".repeat(64), "c".repeat(64)),
            ("g".repeat(64), "b".repeat(64), "c".repeat(64)),
        ] {
            let provenance = crate::fixture_ipc::FixtureSharedHistoryProvenanceV1::new(
                operation,
                Uuid::now_v7(),
                Uuid::now_v7(),
                "/tmp/fixture-stop.sock".to_owned(),
            )
            .unwrap();
            assert!(
                FixtureStopReleaseProposalV1::new(
                    operation,
                    Uuid::now_v7(),
                    Uuid::now_v7(),
                    Uuid::now_v7(),
                    request,
                    receipt,
                    sample,
                    provenance,
                )
                .is_err()
            );
        }
    }

    #[test]
    fn checkpoint_outcomes_keep_transport_loss_ambiguous() {
        use crate::fixture_ipc::CheckpointError;
        assert_eq!(
            FixtureStopReleaseDispositionV1::from_checkpoint_result(Ok(true)),
            FixtureStopReleaseDispositionV1::Accepted
        );
        assert_eq!(
            FixtureStopReleaseDispositionV1::from_checkpoint_result(Ok(false)),
            FixtureStopReleaseDispositionV1::Refused
        );
        assert_eq!(
            FixtureStopReleaseDispositionV1::from_checkpoint_result(Err(CheckpointError::Rejected)),
            FixtureStopReleaseDispositionV1::Refused
        );
        for error in [CheckpointError::Unavailable, CheckpointError::TimedOut] {
            assert_eq!(
                FixtureStopReleaseDispositionV1::from_checkpoint_result(Err(error)),
                FixtureStopReleaseDispositionV1::Ambiguous
            );
        }
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
