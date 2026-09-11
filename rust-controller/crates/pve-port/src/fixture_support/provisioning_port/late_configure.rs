use super::*;
use crate::fixture_ipc::{CheckpointError, FixtureStageRequest};
use std::sync::Mutex;
use uuid::Uuid;

pub(super) struct LateConfigureContext {
    client: FixtureCheckpointClient,
    operation: Uuid,
    generation: Uuid,
    owner: Uuid,
    predecessor: FixtureStageRequest,
    predecessor_identity: FixtureStageIdentity,
    receipt: Vec<u8>,
    bound: Mutex<Option<Bound>>,
}
#[derive(Clone)]
struct Bound {
    identity: FixtureStageIdentity,
    request: FixtureStageRequest,
    submitted: bool,
    released: bool,
    receipt: Option<Vec<u8>>,
}
impl FixtureProvisioningPort {
    /// Restore the exact journaled ConfigurePe response for observation only.
    /// The daemon still validates acceptance and current publication on every read.
    pub fn with_late_configure_receipt(
        self,
        request: &ProvisioningMutationRequestV1,
        receipt: Vec<u8>,
    ) -> io::Result<Self> {
        let configure = self.late_configure.as_ref().ok_or_else(invalid)?;
        let mut bound = configure.bind(request).map_err(|_| invalid())?;
        bound
            .request
            .decode_receipt(&receipt)
            .map_err(|_| invalid())?;
        bound.submitted = true;
        bound.receipt = Some(receipt);
        let mut slot = configure.bound.lock().unwrap();
        if slot.is_some() {
            return Err(invalid());
        }
        *slot = Some(bound);
        drop(slot);
        Ok(self)
    }
    /// Collect the original accepted resize publication before the controller
    /// generates a ConfigurePe attempt. The exact request binds at dispatch checkpoint.
    /// No caller-supplied before-state or manufactured attempt is needed.
    pub fn with_late_configure_after_resize(
        mut self,
        client: FixtureCheckpointClient,
        generation: Uuid,
        owner: Uuid,
        predecessor_identity: FixtureStageIdentity,
        predecessor: FixtureStageRequest,
        original_receipt: Vec<u8>,
    ) -> io::Result<Self> {
        predecessor_identity.validate_request(&predecessor)?;
        if predecessor_identity.stage != FixtureLedgerStage::DiskCapacity {
            return Err(invalid());
        }
        let vm = predecessor.request().plan().expected().vm();
        if generation.is_nil()
            || owner.is_nil()
            || self.exact_identity.is_some()
            || self.checkpoint.is_some()
            || self.resize.is_some()
            || self.late_configure.is_some()
            || self.late_start.is_some()
            || self.legacy_resize.is_some()
            || self.dispatched.lock().unwrap().is_some()
            || predecessor.fixture_id() != self.identity.fixture_id
            || predecessor.request().binding().operation_id().as_uuid() == self.identity.operation
            || vm.node().as_str() != self.identity.node
            || vm.source_vmid().get() != self.identity.source_vmid
            || vm.target_vmid().get() != self.identity.target_vmid
        {
            return Err(invalid());
        }
        predecessor
            .decode_receipt(&original_receipt)
            .map_err(|_| invalid())?;
        self.late_configure = Some(LateConfigureContext {
            client,
            operation: self.identity.operation,
            generation,
            owner,
            predecessor,
            predecessor_identity,
            receipt: original_receipt,
            bound: Mutex::new(None),
        });
        Ok(self)
    }
}
fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, "ConfigurePe binding rejected")
}
impl LateConfigureContext {
    pub(super) fn shared_history_provenance(
        &self,
    ) -> Result<crate::fixture_ipc::FixtureSharedHistoryProvenanceV1, CheckpointError> {
        self.client.shared_history_provenance(&CheckpointBinding {
            operation: self.operation,
            generation: self.generation,
            owner: self.owner,
            point: CheckpointPoint::DispatchCommitted,
        })
    }

    fn bind(&self, request: &ProvisioningMutationRequestV1) -> Result<Bound, CheckpointError> {
        let ProvisioningMutationRequestV1::Configure(configure) = request else {
            return Err(CheckpointError::Rejected);
        };
        let prior = self.predecessor.request();
        let ProvisioningMutationRequestV1::GrowDisk(grow) = prior else {
            return Err(CheckpointError::Rejected);
        };
        if request.plan().action() != ProvisioningActionV1::ConfigurePe {
            return Err(CheckpointError::Rejected);
        }
        if request.binding().operation_id().as_uuid() != self.operation
            || !prior
                .binding()
                .same_operation_attempt(configure.predecessor_binding())
            || !grow
                .clone_binding()
                .same_operation_attempt(configure.clone_binding())
            || prior.plan() != configure.predecessor_plan()
            || prior.plan().expected() != request.plan().expected()
        {
            return Err(CheckpointError::Rejected);
        }
        let request = FixtureStageRequest::new(self.predecessor.fixture_id(), request.clone())
            .map_err(|_| CheckpointError::Rejected)?;
        let identity = FixtureStageIdentity {
            operation: self.operation,
            stage: FixtureLedgerStage::ConfigurePe,
            attempt: request.request().binding().attempt_id().as_uuid(),
            generation: self.generation,
            owner: self.owner,
            request_sha256: request.request_sha256(),
        };
        identity
            .validate_request(&request)
            .map_err(|_| CheckpointError::Rejected)?;
        Ok(Bound {
            identity,
            request,
            submitted: false,
            released: false,
            receipt: None,
        })
    }
    pub(super) async fn checkpoint(
        &self,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<(), CheckpointError> {
        let bound = self.bind(request)?;
        {
            let mut slot = self.bound.lock().unwrap();
            if slot.is_some() {
                return Err(CheckpointError::Rejected);
            }
            *slot = Some(bound.clone());
        }
        self.client
            .stage_checkpoint(&bound.identity)
            .await
            .map_err(CheckpointError::from)?;
        self.bound.lock().unwrap().as_mut().unwrap().released = true;
        Ok(())
    }
    pub(super) async fn observation(
        &self,
        reads: &FixtureReadClient,
    ) -> Result<FixtureSynchronousPostDispatchV1, PveReadError> {
        let bound = self.bound.lock().unwrap().clone();
        match bound {
            None => {
                let observed = reads
                    .stage_post_dispatch(
                        &self.predecessor_identity,
                        &self.predecessor,
                        &self.receipt,
                    )
                    .await
                    .map_err(transport)?
                    .ok_or(PveReadError::TransportUnavailable)?
                    .observation;
                Ok(FixtureSynchronousPostDispatchV1 {
                    version: 1,
                    provisioning: observed.provisioning,
                    inventory: observed.inventory,
                })
            }
            Some(bound) => {
                let receipt = bound.receipt.ok_or(PveReadError::TransportUnavailable)?;
                reads
                    .synchronous_stage_post_dispatch(&bound.identity, &bound.request, &receipt)
                    .await
                    .map_err(transport)?
                    .map(|p| p.observation)
                    .ok_or(PveReadError::TransportUnavailable)
            }
        }
    }

    pub(super) async fn submit(
        &self,
        mutation: &FixtureMutationClient,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<MutationReceipt, PveWriteError> {
        let bound = {
            let mut slot = self.bound.lock().unwrap();
            let bound = slot.as_mut().ok_or(PveWriteError::Rejected)?;
            if !bound.released || bound.submitted || bound.request.request() != request {
                return Err(PveWriteError::Rejected);
            }
            bound.submitted = true;
            bound.clone()
        };
        let receipt = mutation
            .stage_late_with_predecessor(
                bound.identity,
                &bound.request,
                &self.predecessor_identity,
                &self.predecessor,
            )
            .await
            .map_err(|_| PveWriteError::OutcomeUnknown)?;
        let bytes = bound
            .request
            .encode_receipt(receipt.submission_sequence(), receipt.receipt().clone())
            .map_err(|_| PveWriteError::OutcomeUnknown)?;
        self.bound.lock().unwrap().as_mut().unwrap().receipt = Some(bytes);
        Ok(receipt.receipt().clone())
    }
}
