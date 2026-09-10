//! Seed-backed Clone capability with an optional supervisor-owned dispatch barrier.
use super::*;
use crate::*;
use chrono::{DateTime, Utc};
use serde_json::json;
use std::{io, path::PathBuf, time::Duration};

/// Local synthetic provisioning capability. Configure `with_checkpoint` before
/// controller use; an unconfigured checkpoint fails closed.
/// ```
/// use pve_port::{fixture_support::FixtureProvisioningPort, fixture_ipc::ControllerFixturePort};
/// fn checkpoint(p: &FixtureProvisioningPort) { let _: &dyn ControllerFixturePort = p; }
/// ```
pub struct FixtureProvisioningPort {
    reads: FixtureReadClient,
    mutation: FixtureMutationClient,
    identity: FixtureProvisioningIdentity,
    checkpoint: Option<(FixtureCheckpointClient, CheckpointBinding)>,
}
fn transport(e: io::Error) -> PveReadError {
    match e.kind() {
        io::ErrorKind::TimedOut => PveReadError::TimedOut,
        io::ErrorKind::InvalidData => PveReadError::InvalidResponse,
        _ => PveReadError::TransportUnavailable,
    }
}
fn observed<T>(read: SeedRead<T>) -> Result<(DateTime<Utc>, T), PveReadError> {
    match read {
        SeedRead::Observed {
            observed_unix_ms,
            value,
        } => Ok((
            i64::try_from(observed_unix_ms)
                .ok()
                .and_then(DateTime::from_timestamp_millis)
                .ok_or(PveReadError::InvalidResponse)?,
            value,
        )),
        SeedRead::Error { error, .. } => Err(match error {
            SeedReadError::Unavailable => PveReadError::TransportUnavailable,
            SeedReadError::Forbidden => PveReadError::Unauthorized,
            SeedReadError::InvalidResponse => PveReadError::InvalidResponse,
            SeedReadError::Timeout => PveReadError::TimedOut,
        }),
    }
}
impl FixtureProvisioningPort {
    pub fn new(
        socket: PathBuf,
        timeout: Duration,
        identity: FixtureProvisioningIdentity,
    ) -> io::Result<Self> {
        identity.validate()?;
        Ok(Self {
            reads: FixtureReadClient::new(socket.clone(), timeout)?,
            mutation: FixtureMutationClient::new(socket, timeout)?,
            identity,
            checkpoint: None,
        })
    }
    /// Bind this single-operation adapter to a supervisor generation and owner.
    pub fn with_checkpoint(
        mut self,
        client: FixtureCheckpointClient,
        binding: CheckpointBinding,
    ) -> io::Result<Self> {
        if binding.generation.is_nil()
            || binding.owner.is_nil()
            || binding.operation != self.identity.operation
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid controller checkpoint binding",
            ));
        }
        self.checkpoint = Some((client, binding));
        Ok(self)
    }
    fn bind(&self, node: &NodeName) -> Result<(), PveReadError> {
        if node.as_str() == self.identity.node {
            Ok(())
        } else {
            Err(PveReadError::InvalidResponse)
        }
    }
    async fn clone_reads(&self) -> Result<FixtureCloneReads, PveReadError> {
        let facts = self
            .reads
            .clone_reads(self.identity.fixture_id)
            .await
            .map_err(transport)?
            .ok_or(PveReadError::TransportUnavailable)?;
        if facts.node != self.identity.node {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(facts)
    }
    async fn provisioning(&self) -> Result<FixtureProvisioningReads, PveReadError> {
        self.reads
            .provisioning_reads(&self.identity)
            .await
            .map_err(transport)?
            .ok_or(PveReadError::TransportUnavailable)
    }
}
impl crate::native::sealed::FakeMutationCapability for FixtureProvisioningPort {}
#[async_trait::async_trait]
impl crate::fixture_ipc::ControllerFixturePort for FixtureProvisioningPort {
    async fn controller_checkpoint(
        &self,
        point: FakeControllerCheckpoint,
    ) -> Result<(), crate::fixture_ipc::CheckpointError> {
        let (client, binding) = self
            .checkpoint
            .as_ref()
            .ok_or(crate::fixture_ipc::CheckpointError::Rejected)?;
        if binding.point != point.into() {
            return Err(crate::fixture_ipc::CheckpointError::Rejected);
        }
        client.controller_checkpoint(*binding).await
    }
}
#[async_trait::async_trait]
impl PveReadPort for FixtureProvisioningPort {
    async fn vm_config(&self, _: &NodeName, _: Vmid) -> Result<VmConfig, PveReadError> {
        Err(PveReadError::TransportUnavailable)
    }
    async fn task_status(&self, _: &NodeName, _: &Upid) -> Result<TaskStatus, PveReadError> {
        Err(PveReadError::TransportUnavailable)
    }
    async fn storage_content(
        &self,
        _: &NodeName,
        _: &StorageName,
    ) -> Result<Vec<Volume>, PveReadError> {
        Err(PveReadError::TransportUnavailable)
    }
    async fn qga_ping(&self, _: &NodeName, _: Vmid) -> Result<QgaStatus, PveReadError> {
        Err(PveReadError::TransportUnavailable)
    }
}
#[async_trait::async_trait]
impl PvePreflightReadPort for FixtureProvisioningPort {
    async fn node_status(&self, node: &NodeName) -> Result<NodeStatus, PveReadError> {
        self.bind(node)?;
        let (time, value) = observed(self.clone_reads().await?.node_status)?;
        if !value.online {
            return Err(PveReadError::TransportUnavailable);
        }
        NodeStatus::from_wire(node.clone(), json!({"uptime":value.uptime_seconds}), time)
    }
    async fn storage_status(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<StorageStatus, PveReadError> {
        self.bind(node)?;
        let (time, values) = observed(self.clone_reads().await?.storage)?;
        let value = values
            .into_iter()
            .find(|s| s.name == storage.as_str())
            .ok_or(PveReadError::TransportUnavailable)?;
        StorageStatus::from_wire(
            node.clone(),
            storage.clone(),
            json!({"active":u8::from(value.active),"enabled":u8::from(value.enabled),"avail":value.available_bytes,"content":value.content.join(",")}),
            time,
        )
    }
    async fn bridges(&self, node: &NodeName) -> Result<BridgeInventory, PveReadError> {
        self.bind(node)?;
        let (time, values) = observed(self.clone_reads().await?.bridges)?;
        BridgeInventory::from_wire(
            node.clone(),
            json!(
                values
                    .into_iter()
                    .map(|b| json!({"type":"bridge","iface":b.name,"active":u8::from(b.active)}))
                    .collect::<Vec<_>>()
            ),
            time,
        )
    }
    async fn cluster_vms(&self) -> Result<ClusterVmInventory, PveReadError> {
        let (time, values) = observed(self.clone_reads().await?.cluster_inventory)?;
        let entries = values.into_iter().map(|v| {
            let (_, power) = observed(v.status)?;
            Ok(json!({"type":"qemu","node":v.node,"vmid":v.vmid,"name":v.name,"template":u8::from(v.template),"status":power}))
        }).collect::<Result<Vec<_>, PveReadError>>()?;
        ClusterVmInventory::from_wire(json!(entries), time)
    }
    async fn native_vm_config(
        &self,
        _: &NodeName,
        _: Vmid,
    ) -> Result<NativeVmConfig, PveReadError> {
        Err(PveReadError::TransportUnavailable)
    }
    async fn vm_status(&self, node: &NodeName, vmid: Vmid) -> Result<VmPowerStatus, PveReadError> {
        self.bind(node)?;
        let facts = self.provisioning().await?;
        let read = if vmid.get() == self.identity.source_vmid {
            facts.source_power
        } else if vmid.get() == self.identity.target_vmid {
            facts.target_power
        } else {
            return Err(PveReadError::TransportUnavailable);
        };
        let (time, value) = observed(read)?;
        VmPowerStatus::from_wire(
            node.clone(),
            vmid,
            json!({"vmid":vmid.get(),"status":value.power,"locked":u8::from(value.locked)}),
            time,
        )
    }
}
#[async_trait::async_trait]
impl ProvisioningFakePort for FixtureProvisioningPort {
    async fn provisioning_vm_config(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<ProvisioningVmConfigV1, PveReadError> {
        self.bind(node)?;
        let facts = self.provisioning().await?;
        let read = if vmid.get() == self.identity.source_vmid {
            facts.source_config
        } else if vmid.get() == self.identity.target_vmid {
            facts.target_config
        } else {
            return Err(PveReadError::TransportUnavailable);
        };
        match observed(read)?.1 {
            SeedConfig::Absent {} => Err(PveReadError::NotFound),
            SeedConfig::Present { config } => Ok(*config),
        }
    }
    async fn provisioning_identity(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<ProvisioningIdentitySnapshotV1, PveReadError> {
        let (time, entries) = observed(self.clone_reads().await?.cluster_inventory)?;
        let v = entries
            .into_iter()
            .find(|v| v.node == node.as_str() && v.vmid == vmid.get())
            .ok_or(PveReadError::TransportUnavailable)?;
        let (_, coverage) = observed(v.coverage)?;
        serde_json::from_value(json!({"contract_version":1,"node":v.node,"vmid":v.vmid,"source":"fake_pve","name":v.name,"is_template":v.template,"config_digest":v.config_sha256,"uuid":v.uuid,"mac":v.mac,"primary_storage":v.primary_storage,"primary_volume":v.primary_volume,"coverage":coverage,"observed_at":time})).map_err(|_| PveReadError::InvalidResponse)
    }
    async fn provisioning_media(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<ProvisioningMediaInventoryV1, PveReadError> {
        self.bind(node)?;
        let facts = self.provisioning().await?;
        // Error envelopes do not carry storage identity. Require both families
        // before selecting; otherwise a successful family could hide an error.
        let deployment = observed(facts.deployment_media)?.1;
        let driver = observed(facts.driver_media)?.1;
        if deployment.storage() == driver.storage() && deployment != driver {
            return Err(PveReadError::InvalidResponse);
        }
        for media in [deployment, driver] {
            if media.storage() == storage {
                return Ok(media);
            }
        }
        Err(PveReadError::TransportUnavailable)
    }
    async fn submit_provisioning(
        &self,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<MutationReceipt, PveWriteError> {
        let ProvisioningMutationRequestV1::Clone(request) = request else {
            return Err(PveWriteError::Rejected);
        };
        let envelope =
            crate::fixture_ipc::FixtureCloneRequest::new(self.identity.fixture_id, request.clone())
                .map_err(|_| PveWriteError::Rejected)?;
        let vm = request.clone_request().vm();
        if envelope.request_sha256() != self.identity.request_sha256
            || request.clone_request().operation_id().as_uuid() != self.identity.operation
            || vm.node().as_str() != self.identity.node
            || vm.source_vmid().get() != self.identity.source_vmid
            || vm.target_vmid().get() != self.identity.target_vmid
        {
            return Err(PveWriteError::Rejected);
        }
        let receipt = self
            .mutation
            .clone_vm(&envelope)
            .await
            .map_err(|_| PveWriteError::OutcomeUnknown)?;
        Ok(receipt.receipt().clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn observations_keep_time_and_error_classes() {
        let (time, value) = observed(SeedRead::Observed {
            observed_unix_ms: 123,
            value: false,
        })
        .unwrap();
        assert_eq!(time.timestamp_millis(), 123);
        assert!(!value);
        assert_eq!(
            observed::<bool>(SeedRead::Error {
                observed_unix_ms: 123,
                error: SeedReadError::Forbidden
            }),
            Err(PveReadError::Unauthorized)
        );
        assert_eq!(
            observed(SeedRead::Observed {
                observed_unix_ms: u64::MAX,
                value: true
            }),
            Err(PveReadError::InvalidResponse)
        );
    }
    #[tokio::test]
    async fn capability_is_sealed_and_unsupported_reads_are_unavailable() {
        let identity = FixtureProvisioningIdentity {
            fixture_id: uuid::Uuid::from_u128(1),
            operation: uuid::Uuid::from_u128(2),
            request_sha256: "a".repeat(64),
            node: "fixture-node".into(),
            source_vmid: 100,
            target_vmid: 101,
        };
        let port = FixtureProvisioningPort::new(
            PathBuf::from("/nonexistent-fixture.sock"),
            Duration::from_millis(100),
            identity,
        )
        .unwrap();
        use crate::fixture_ipc::{CheckpointError, ControllerFixturePort};
        assert_eq!(
            port.controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
                .await,
            Err(CheckpointError::Rejected)
        );
        let capability: &dyn ProvisioningFakePort = &port;
        let node = NodeName::parse("fixture-node").unwrap();
        assert_eq!(
            capability.qga_ping(&node, Vmid::new(100).unwrap()).await,
            Err(PveReadError::TransportUnavailable)
        );
        assert_eq!(
            capability
                .node_status(&NodeName::parse("wrong-node").unwrap())
                .await,
            Err(PveReadError::InvalidResponse)
        );
        let binding = CheckpointBinding {
            generation: uuid::Uuid::now_v7(),
            owner: uuid::Uuid::now_v7(),
            operation: uuid::Uuid::from_u128(2),
            point: CheckpointPoint::DispatchCommitted,
        };
        let port = port
            .with_checkpoint(
                FixtureCheckpointClient::new(
                    PathBuf::from("/nonexistent-fixture.sock"),
                    Duration::from_millis(100),
                )
                .unwrap(),
                binding,
            )
            .unwrap();
        assert_eq!(
            port.controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
                .await,
            Err(CheckpointError::Unavailable)
        );
        assert!(
            port.with_checkpoint(
                FixtureCheckpointClient::new(
                    PathBuf::from("/nonexistent-fixture.sock"),
                    Duration::from_millis(100)
                )
                .unwrap(),
                CheckpointBinding {
                    operation: uuid::Uuid::now_v7(),
                    ..binding
                },
            )
            .is_err()
        );
    }
}
