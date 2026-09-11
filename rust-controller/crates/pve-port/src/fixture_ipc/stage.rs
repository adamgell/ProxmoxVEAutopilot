//! Message validation through PeEnsureStopped. A stop message binds physical
//! history only; shutdown-grace adjudication remains scheduler-owned. The daemon
//! refuses stop dispatch until durable stop admission and observation exist.
use super::{InvalidFixtureClone, ReceiptWire, decode, encode};
use crate::{MutationReceipt, ProvisioningActionV1, ProvisioningMutationRequestV1};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

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
