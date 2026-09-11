use super::*;

pub enum FakeProvisioningConfigReadV1 {
    Error(PveReadError),
    Snapshot(Box<ProvisioningVmConfigV1>),
    Pause(Arc<FakePause>),
}
pub enum FakeProvisioningIdentityReadV1 {
    Error(PveReadError),
    Snapshot(Box<ProvisioningIdentitySnapshotV1>),
    Pause(Arc<FakePause>),
}
pub enum FakeProvisioningMediaReadV1 {
    Error(PveReadError),
    Snapshot(Box<ProvisioningMediaInventoryV1>),
    Pause(Arc<FakePause>),
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProvisioningFaultSelectorV1 {
    action: ProvisioningActionV1,
    operation_id: Option<OperationId>,
}
impl ProvisioningFaultSelectorV1 {
    fn key(&self) -> SelectorKey {
        (self.action, self.operation_id.map(OperationId::as_uuid))
    }
    pub fn new(action: ProvisioningActionV1, operation_id: Option<OperationId>) -> Self {
        Self {
            action,
            operation_id,
        }
    }
    pub fn action(&self) -> ProvisioningActionV1 {
        self.action
    }
    pub fn operation_id(&self) -> Option<OperationId> {
        self.operation_id
    }
    pub fn profile(&self) -> Option<ProvisioningBootProfile> {
        use ProvisioningActionV1::*;
        match self.action {
            ConfigurePe | StartPe | EnsureStopped => Some(ProvisioningBootProfile::PeMedia),
            ConfigureDisk | StartDisk => Some(ProvisioningBootProfile::InstalledDisk),
            Clone | EnsureCapacity => None,
        }
    }
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProvisioningAcceptanceV1 {
    pub(super) accepted_at: DateTime<Utc>,
    pub(super) receipt: MutationReceipt,
}
impl ProvisioningAcceptanceV1 {
    pub fn accepted_at(&self) -> DateTime<Utc> {
        self.accepted_at
    }
    pub fn receipt(&self) -> &MutationReceipt {
        &self.receipt
    }
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProvisioningSubmissionRecordV1 {
    pub(super) submission_number: u64,
    pub(super) request: ProvisioningMutationRequestV1,
    pub(super) request_sha256: String,
    pub(super) submitted_at: DateTime<Utc>,
    pub(super) acceptance: Option<ProvisioningAcceptanceV1>,
    pub(super) returned: Result<MutationReceipt, PveWriteError>,
}
impl ProvisioningSubmissionRecordV1 {
    pub fn submission_number(&self) -> u64 {
        self.submission_number
    }
    pub fn request(&self) -> &ProvisioningMutationRequestV1 {
        &self.request
    }
    pub fn request_sha256(&self) -> &str {
        &self.request_sha256
    }
    pub fn submitted_at(&self) -> DateTime<Utc> {
        self.submitted_at
    }
    pub fn acceptance(&self) -> Option<&ProvisioningAcceptanceV1> {
        self.acceptance.as_ref()
    }
    pub fn returned(&self) -> &Result<MutationReceipt, PveWriteError> {
        &self.returned
    }
}
impl NativeFakePve {
    pub fn insert_provisioning_vm(
        &self,
        config: ProvisioningVmConfigV1,
        power: PowerState,
    ) -> Result<(), PveWriteError> {
        self.provisioning_fixture(config, power, false)
    }
    pub fn replace_provisioning_vm(
        &self,
        config: ProvisioningVmConfigV1,
        power: PowerState,
    ) -> Result<(), PveWriteError> {
        self.provisioning_fixture(config, power, true)
    }
    fn provisioning_fixture(
        &self,
        config: ProvisioningVmConfigV1,
        power: PowerState,
        replace: bool,
    ) -> Result<(), PveWriteError> {
        if config.source() != NativeEvidenceSource::FakePve {
            return Err(PveWriteError::Rejected);
        }
        let mut s = self.state.lock().unwrap();
        match (replace, s.vms.contains_key(&config.vmid())) {
            (false, true) => return Err(PveWriteError::Conflict),
            (true, false) => return Err(PveWriteError::Rejected),
            _ => {}
        }
        let incarnation = s.next_incarnation()?;
        s.vms.insert(
            config.vmid(),
            FakeVm {
                config: FakeConfig::ProvisioningV1(config),
                power,
                incarnation,
                qga_reachable: false,
            },
        );
        Ok(())
    }
    pub fn set_provisioning_power(
        &self,
        node: &NodeName,
        vmid: Vmid,
        power: PowerState,
    ) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        lookup(&s, node, vmid)
            .and_then(|v| v.config.provisioning())
            .map_err(|_| PveWriteError::Rejected)?;
        let incarnation = s.next_incarnation()?;
        let vm = s.vms.get_mut(&vmid).ok_or(PveWriteError::Rejected)?;
        vm.power = power;
        vm.incarnation = incarnation;
        Ok(())
    }
    pub fn set_qga_reachable(
        &self,
        node: &NodeName,
        vmid: Vmid,
        reachable: bool,
    ) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        lookup(&s, node, vmid).map_err(|_| PveWriteError::Rejected)?;
        s.vms
            .get_mut(&vmid)
            .ok_or(PveWriteError::Rejected)?
            .qga_reachable = reachable;
        Ok(())
    }
    pub fn set_provisioning_media(&self, inventory: ProvisioningMediaInventoryV1) {
        self.state.lock().unwrap().provisioning.media.insert(
            (inventory.node().clone(), inventory.storage().clone()),
            inventory,
        );
    }
    pub fn enqueue_provisioning_config_read(
        &self,
        node: NodeName,
        vmid: Vmid,
        read: FakeProvisioningConfigReadV1,
    ) {
        self.state
            .lock()
            .unwrap()
            .provisioning
            .config_reads
            .entry((node, vmid))
            .or_default()
            .push_back(read);
    }
    pub fn enqueue_provisioning_identity_read(
        &self,
        node: NodeName,
        vmid: Vmid,
        read: FakeProvisioningIdentityReadV1,
    ) {
        self.state
            .lock()
            .unwrap()
            .provisioning
            .identity_reads
            .entry((node, vmid))
            .or_default()
            .push_back(read);
    }
    pub fn enqueue_provisioning_media_read(
        &self,
        node: NodeName,
        storage: StorageName,
        read: FakeProvisioningMediaReadV1,
    ) {
        self.state
            .lock()
            .unwrap()
            .provisioning
            .media_reads
            .entry((node, storage))
            .or_default()
            .push_back(read);
    }
    pub fn pause_provisioning_submission(
        &self,
        selector: ProvisioningFaultSelectorV1,
    ) -> Arc<FakePause> {
        let gate = Arc::new(FakePause::default());
        self.state
            .lock()
            .unwrap()
            .provisioning
            .pauses
            .entry(selector.key())
            .or_default()
            .push_back(gate.clone());
        gate
    }
    pub fn enqueue_provisioning_outcome(
        &self,
        selector: ProvisioningFaultSelectorV1,
        outcome: FakeMutationOutcome,
    ) -> Result<(), UnsupportedFakeOutcome> {
        if matches!(
            selector.action(),
            ProvisioningActionV1::ConfigurePe | ProvisioningActionV1::ConfigureDisk
        ) && matches!(
            outcome,
            FakeMutationOutcome::AcceptedTaskFails | FakeMutationOutcome::AcceptedTaskDelayed
        ) {
            return Err(UnsupportedFakeOutcome);
        }
        self.state
            .lock()
            .unwrap()
            .provisioning
            .outcomes
            .entry(selector.key())
            .or_default()
            .push_back(outcome);
        Ok(())
    }
    pub fn recorded_provisioning_submissions(&self) -> Vec<ProvisioningSubmissionRecordV1> {
        self.state.lock().unwrap().provisioning.records.clone()
    }
    pub fn pending_provisioning_tasks(&self) -> Vec<Upid> {
        self.state
            .lock()
            .unwrap()
            .pending
            .iter()
            .filter_map(|p| match p {
                PendingMutation::ProvisioningV1 { upid, .. } => Some(upid.clone()),
                _ => None,
            })
            .collect()
    }
}
