//! One-use fixture StartPe dispatch with explicit validated full readback.
use super::*;
use crate::fixture_ipc::{CheckpointError, FixtureStageRequest};
use std::sync::Mutex;
use uuid::Uuid;

/// Original fixture response captured only after successful typed StartPe IPC.
/// It carries observation/persistence data, never checkpoint or send authority.
/// ```compile_fail
/// let _: pve_port::fixture_support::FixtureStartPeResponseV1 = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail
/// let _ = pve_port::fixture_support::FixtureStartPeResponseV1 { receipt: vec![] };
/// ```
#[derive(Clone)]
pub struct FixtureStartPeResponseV1 {
    provenance_sha256: String,
    identity: FixtureStageIdentity,
    request: FixtureStageRequest,
    predecessor_identity: FixtureStageIdentity,
    predecessor: FixtureStageRequest,
    receipt: Vec<u8>,
}
impl std::fmt::Debug for FixtureStartPeResponseV1 {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("FixtureStartPeResponseV1([private original response])")
    }
}
impl FixtureStartPeResponseV1 {
    pub fn provenance_sha256(&self) -> &str {
        &self.provenance_sha256
    }
    pub fn identity(&self) -> &FixtureStageIdentity {
        &self.identity
    }
    pub fn request(&self) -> &FixtureStageRequest {
        &self.request
    }
    pub fn predecessor_identity(&self) -> &FixtureStageIdentity {
        &self.predecessor_identity
    }
    pub fn predecessor(&self) -> &FixtureStageRequest {
        &self.predecessor
    }
    pub fn original_receipt(&self) -> &[u8] {
        &self.receipt
    }
}

pub(super) struct LateStartContext {
    client: FixtureCheckpointClient,
    operation: Uuid,
    generation: Uuid,
    owner: Uuid,
    predecessor_identity: FixtureStageIdentity,
    predecessor: FixtureStageRequest,
    state: Mutex<Option<Bound>>,
}
#[derive(Clone)]
struct Bound {
    identity: FixtureStageIdentity,
    request: FixtureStageRequest,
    released: bool,
    submitted: bool,
    receipt: Option<Vec<u8>>,
    original_response: Option<FixtureStartPeResponseV1>,
}
impl FixtureProvisioningPort {
    /// Return captured original IPC data. Restoring externally supplied receipt
    /// bytes never populates this value, even if their later readback succeeds.
    pub fn captured_start_pe_response(&self) -> Option<FixtureStartPeResponseV1> {
        self.late_start
            .as_ref()?
            .state
            .lock()
            .unwrap()
            .as_ref()?
            .original_response
            .clone()
    }
    pub(super) async fn start_inventory(&self) -> Result<FixtureCloneReads, PveReadError> {
        let full = self
            .validate_bound_start_pe()
            .await?
            .ok_or(PveReadError::TransportUnavailable)?
            .observation;
        let at = full.inventory.observed_unix_ms;
        let members = full
            .inventory
            .members
            .into_iter()
            .map(|member| {
                let config = member.config;
                SeedIdentity {
                    status: SeedRead::Observed {
                        observed_unix_ms: at,
                        value: member.power,
                    },
                    coverage: SeedRead::Observed {
                        observed_unix_ms: at,
                        value: member.coverage,
                    },
                    node: config.node().to_string(),
                    vmid: config.vmid().get(),
                    name: config.name().to_string(),
                    template: config.is_template(),
                    config_sha256: config.digest().to_owned(),
                    uuid: config.uuid().as_uuid(),
                    mac: config.mac().to_string(),
                    primary_storage: config.primary_disk().storage().to_string(),
                    primary_volume: config.primary_disk().volume().to_owned(),
                }
            })
            .collect();
        // The full bundle has no separate collection timestamp for these
        // infrastructure fields. Do not stamp them with the inventory clock.
        Ok(FixtureCloneReads {
            version: 1,
            fixture_id: self.identity.fixture_id,
            node: self.identity.node.clone(),
            node_status: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            storage: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            bridges: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            cluster_inventory: SeedRead::Observed {
                observed_unix_ms: at,
                value: members,
            },
        })
    }

    /// Restore the original StartPe receipt for observation only. Structural
    /// receipt matching does not prove acceptance; every read asks the daemon
    /// to validate its durable accepted effect and exact full publication.
    /// Restoration consumes this adapter's send/checkpoint state permanently.
    pub fn with_late_start_receipt(
        self,
        request: &ProvisioningMutationRequestV1,
        receipt: Vec<u8>,
    ) -> io::Result<Self> {
        let invalid =
            || io::Error::new(io::ErrorKind::InvalidInput, "StartPe restoration rejected");
        let context = self.late_start.as_ref().ok_or_else(invalid)?;
        let mut bound = context.bind(request).map_err(|_| invalid())?;
        bound
            .request
            .decode_receipt(&receipt)
            .map_err(|_| invalid())?;
        bound.submitted = true;
        bound.receipt = Some(receipt);
        let mut state = context.state.lock().unwrap();
        if state.is_some() {
            return Err(invalid());
        }
        *state = Some(bound);
        drop(state);
        Ok(self)
    }

    /// Bind the original ConfigurePe request; the daemon independently proves
    /// its acceptance and power authorization. This does not authorize sending.
    pub fn with_late_start_after_configure(
        mut self,
        client: FixtureCheckpointClient,
        generation: Uuid,
        owner: Uuid,
        predecessor_identity: FixtureStageIdentity,
        predecessor: FixtureStageRequest,
    ) -> io::Result<Self> {
        predecessor_identity.validate_request(&predecessor)?;
        let vm = predecessor.request().plan().expected().vm();
        if predecessor_identity.stage != FixtureLedgerStage::ConfigurePe
            || generation.is_nil()
            || owner.is_nil()
            || self.exact_identity.is_some()
            || self.checkpoint.is_some()
            || self.resize.is_some()
            || self.legacy_resize.is_some()
            || self.late_configure.is_some()
            || self.late_start.is_some()
            || self.dispatched.lock().unwrap().is_some()
            || predecessor.fixture_id() != self.identity.fixture_id
            || predecessor_identity.operation == self.identity.operation
            || vm.node().as_str() != self.identity.node
            || vm.source_vmid().get() != self.identity.source_vmid
            || vm.target_vmid().get() != self.identity.target_vmid
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "StartPe binding rejected",
            ));
        }
        self.late_start = Some(LateStartContext {
            client,
            operation: self.identity.operation,
            generation,
            owner,
            predecessor_identity,
            predecessor,
            state: Mutex::new(None),
        });
        Ok(self)
    }

    /// Explicit readback only; absence is not success. Every mapped provisioning
    /// read also validates this same accepted publication before returning facts.
    pub async fn validate_bound_start_pe(
        &self,
    ) -> Result<Option<FixtureStartPeFullPublicationV1>, PveReadError> {
        let context = self
            .late_start
            .as_ref()
            .ok_or(PveReadError::InvalidResponse)?;
        let bound = context.state.lock().unwrap().clone();
        let Some(bound) = bound else {
            return Ok(None);
        };
        let Some(receipt) = bound.receipt else {
            return Ok(None);
        };
        self.reads
            .start_pe_full(&bound.identity, &bound.request, &receipt)
            .await
            .map_err(transport)
    }

    pub(super) async fn start_provisioning(
        &self,
    ) -> Result<FixtureProvisioningReadsV2, PveReadError> {
        let full = self
            .validate_bound_start_pe()
            .await?
            .ok_or(PveReadError::TransportUnavailable)?
            .observation;
        let inventory = &full.inventory;
        let member = |vmid| {
            inventory
                .members
                .iter()
                .find(|member| {
                    member.config.node().as_str() == self.identity.node
                        && member.config.vmid().get() == vmid
                })
                .ok_or(PveReadError::InvalidResponse)
        };
        let source = member(self.identity.source_vmid)?;
        let target = member(self.identity.target_vmid)?;
        let config = |member: &FixtureInventoryMemberV2| SeedRead::Observed {
            observed_unix_ms: inventory.observed_unix_ms,
            value: SeedConfig::Present {
                config: Box::new(member.config.clone()),
            },
        };
        let power = |member: &FixtureInventoryMemberV2| SeedRead::Observed {
            observed_unix_ms: inventory.observed_unix_ms,
            value: SeedPower {
                power: member.power,
                locked: member.config.locked(),
            },
        };
        let coverage = |member: &FixtureInventoryMemberV2| SeedRead::Observed {
            observed_unix_ms: inventory.observed_unix_ms,
            value: member.coverage,
        };
        let media = |value: ProvisioningMediaInventoryV1| -> Result<_, PveReadError> {
            Ok(SeedRead::Observed {
                observed_unix_ms: value
                    .observed_at()
                    .timestamp_millis()
                    .try_into()
                    .map_err(|_| PveReadError::InvalidResponse)?,
                value,
            })
        };
        Ok(FixtureProvisioningReadsV2 {
            version: 2,
            identity: self.identity.clone(),
            source_config: config(source),
            target_config: config(target),
            source_power: power(source),
            target_power: power(target),
            source_coverage: coverage(source),
            target_coverage: coverage(target),
            deployment_media: media(full.deployment_media)?,
            driver_media: media(full.driver_media)?,
        })
    }
}
impl LateStartContext {
    pub(super) fn provenance(
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
        let ProvisioningMutationRequestV1::Start(start) = request else {
            return Err(CheckpointError::Rejected);
        };
        let prior = self.predecessor.request();
        if request.plan().action() != ProvisioningActionV1::StartPe
            || request.binding().operation_id().as_uuid() != self.operation
            || !prior
                .binding()
                .same_operation_attempt(start.predecessor_binding())
            || prior.plan() != start.predecessor_plan()
            || prior.plan().expected() != request.plan().expected()
        {
            return Err(CheckpointError::Rejected);
        }
        let request = FixtureStageRequest::new(self.predecessor.fixture_id(), request.clone())
            .map_err(|_| CheckpointError::Rejected)?;
        let identity = FixtureStageIdentity {
            operation: self.operation,
            stage: FixtureLedgerStage::StartPe,
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
            released: false,
            submitted: false,
            receipt: None,
            original_response: None,
        })
    }
    pub(super) async fn checkpoint(
        &self,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<(), CheckpointError> {
        let bound = self.bind(request)?;
        {
            let mut state = self.state.lock().unwrap();
            if state.is_some() {
                return Err(CheckpointError::Rejected);
            }
            *state = Some(bound.clone());
        }
        self.client
            .stage_checkpoint(&bound.identity)
            .await
            .map_err(CheckpointError::from)?;
        self.state.lock().unwrap().as_mut().unwrap().released = true;
        Ok(())
    }
    pub(super) async fn submit(
        &self,
        mutation: &FixtureMutationClient,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<MutationReceipt, PveWriteError> {
        let provenance_sha256 = self
            .provenance()
            .map_err(|_| PveWriteError::Rejected)?
            .sha256();
        let bound = {
            let mut state = self.state.lock().unwrap();
            let bound = state.as_mut().ok_or(PveWriteError::Rejected)?;
            if !bound.released || bound.submitted || bound.request.request() != request {
                return Err(PveWriteError::Rejected);
            }
            bound.submitted = true;
            bound.clone()
        };
        let receipt = mutation
            .stage_late_with_predecessor(
                bound.identity.clone(),
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
        let response = FixtureStartPeResponseV1 {
            provenance_sha256,
            identity: bound.identity,
            request: bound.request,
            predecessor_identity: self.predecessor_identity.clone(),
            predecessor: self.predecessor.clone(),
            receipt: bytes.clone(),
        };
        let mut state = self.state.lock().unwrap();
        let bound = state.as_mut().unwrap();
        bound.receipt = Some(bytes);
        bound.original_response = Some(response);
        Ok(receipt.receipt().clone())
    }
}
