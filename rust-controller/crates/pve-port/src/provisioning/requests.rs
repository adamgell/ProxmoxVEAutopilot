use super::*;
use crate::{CloneRequest, VmPowerStatus};
use controller_domain::OperationId;
pub(super) mod state;
mod wire;
use evidence::hash_valid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
/// Validated before-state is readable but cannot be replaced after construction.
/// ```compile_fail
/// use pve_port::{ProvisioningBeforeStateV1, ProvisioningVmConfigV1};
/// fn alter(before: &mut ProvisioningBeforeStateV1, config: ProvisioningVmConfigV1) {
///     before.config = config;
/// }
/// ```
pub struct ProvisioningBeforeStateV1 {
    config: ProvisioningVmConfigV1,
    power: VmPowerStatus,
}
impl ProvisioningBeforeStateV1 {
    pub fn new(
        config: ProvisioningVmConfigV1,
        power: VmPowerStatus,
    ) -> Result<Self, InvalidProvisioning> {
        if config.node() != power.node() || config.vmid() != power.vmid() {
            return Err(InvalidProvisioning);
        }
        Ok(Self { config, power })
    }
    pub fn config(&self) -> &ProvisioningVmConfigV1 {
        &self.config
    }
    pub fn power(&self) -> &VmPowerStatus {
        &self.power
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CloneProvisioningRequestV1 {
    contract_version: u16,
    binding: ProvisioningBindingV1,
    plan: ProvisioningOperationPlanV1,
    clone: CloneRequest,
    expected_before: ProvisioningBeforeStateV1,
}
impl CloneProvisioningRequestV1 {
    pub fn new(
        binding: ProvisioningBindingV1,
        plan: ProvisioningOperationPlanV1,
        clone: CloneRequest,
        expected_before: ProvisioningBeforeStateV1,
        as_of: DateTime<Utc>,
        freshness_seconds: u16,
    ) -> Result<Self, InvalidProvisioning> {
        let r = Self {
            contract_version: 1,
            binding,
            plan,
            clone,
            expected_before,
        };
        r.validate()?;
        if !state::before_fresh(&r.expected_before, as_of, freshness_seconds) {
            return Err(InvalidProvisioning);
        }
        Ok(r)
    }
    fn validate(&self) -> Result<(), InvalidProvisioning> {
        if self.contract_version != 1
            || self.plan.action() != ProvisioningActionV1::Clone
            || !self.binding.validates(&self.plan)
            || self.clone.vm() != self.plan.expected().vm()
            || self.clone.operation_id() != self.binding.operation_id()
            || self.plan.expected().disk_serial().len() > 20
            || !state::template(
                self.expected_before.config(),
                self.expected_before.power(),
                &self.plan,
            )
        {
            return Err(InvalidProvisioning);
        }
        Ok(())
    }
    pub fn clone_request(&self) -> &CloneRequest {
        &self.clone
    }
    pub fn contract_version(&self) -> u16 {
        self.contract_version
    }
}
pub struct ProvisioningOwnedRequestInputV1<'a> {
    pub binding: ProvisioningBindingV1,
    pub plan: ProvisioningOperationPlanV1,
    pub ownership: &'a ProvisioningCloneOwnershipV1,
    pub predecessor: &'a ProvisioningStageBaselineV1,
    pub expected_before: ProvisioningBeforeStateV1,
    pub as_of: DateTime<Utc>,
    pub freshness_seconds: u16,
}

// One private envelope and shared validator; each public action type remains a
// closed validated value. Flattened serialization pins the specified envelope.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct OwnedRequest {
    contract_version: u16,
    binding: ProvisioningBindingV1,
    plan: ProvisioningOperationPlanV1,
    clone_request: CloneRequest,
    clone_binding: ProvisioningBindingV1,
    clone_evidence_sha256: String,
    clone_config: ProvisioningVmConfigV1,
    predecessor_binding: ProvisioningBindingV1,
    predecessor_plan: ProvisioningOperationPlanV1,
    predecessor_evidence_sha256: String,
    predecessor_config: ProvisioningVmConfigV1,
    predecessor_power: VmPowerStatus,
    expected_before: ProvisioningBeforeStateV1,
}
impl OwnedRequest {
    fn new(i: ProvisioningOwnedRequestInputV1<'_>) -> Result<Self, InvalidProvisioning> {
        let r = Self {
            contract_version: 1,
            binding: i.binding,
            plan: i.plan,
            clone_request: i.ownership.request().clone_request().clone(),
            clone_binding: i.ownership.request().binding().clone(),
            clone_evidence_sha256: fingerprint(i.ownership.proof())?,
            clone_config: i.ownership.config().clone(),
            predecessor_binding: i.predecessor.binding().clone(),
            predecessor_plan: i.predecessor.plan().clone(),
            predecessor_evidence_sha256: i.predecessor.evidence_sha256().into(),
            predecessor_config: i.predecessor.config().clone(),
            predecessor_power: i.predecessor.power().clone(),
            expected_before: i.expected_before,
        };
        r.validate()?;
        if !state::before_fresh(&r.expected_before, i.as_of, i.freshness_seconds) {
            return Err(InvalidProvisioning);
        }
        Ok(r)
    }
    fn validate(&self) -> Result<(), InvalidProvisioning> {
        let clone_plan = ProvisioningOperationPlanV1::new(
            ProvisioningActionV1::Clone,
            self.plan.expected().clone(),
        );
        if self.contract_version != 1
            || self.plan.expected().disk_serial().len() > 20
            || !self.binding.validates(&self.plan)
            || (self.plan.action() == ProvisioningActionV1::EnsureCapacity
                && !self
                    .plan
                    .expected()
                    .effective_capacity_bytes()
                    .is_multiple_of(1_073_741_824))
            || !self.clone_binding.validates(&clone_plan)
            || self.clone_request.vm() != self.plan.expected().vm()
            || self.clone_request.operation_id() != self.clone_binding.operation_id()
            || !self.binding.same_workflow(&self.clone_binding)
            || !self.binding.same_workflow(&self.predecessor_binding)
            || self.binding.operation_id() == self.clone_binding.operation_id()
            || self.binding.operation_id() == self.predecessor_binding.operation_id()
            || !self.predecessor_binding.validates(&self.predecessor_plan)
            || self.predecessor_plan.expected() != self.plan.expected()
            || state::predecessor(self.plan.action()) != Some(self.predecessor_plan.action())
            || !hash_valid(&self.clone_evidence_sha256)
            || !hash_valid(&self.predecessor_evidence_sha256)
            || self.clone_config.primary_disk().capacity_bytes()
                != self.plan.expected().template_capacity_bytes()
            || self.predecessor_config.node() != self.predecessor_power.node()
            || self.predecessor_config.vmid() != self.predecessor_power.vmid()
            || !state::stage_before(
                &self.plan,
                &self.clone_request,
                &self.clone_config,
                &self.predecessor_config,
                self.expected_before.config(),
            )
            || !state::power_valid(self.expected_before.power())
            || (self.plan.action() != ProvisioningActionV1::EnsureStopped
                && !state::stopped(self.expected_before.power()))
            || (self.predecessor_plan.action() == ProvisioningActionV1::StartPe
                && self.predecessor_power.power() != crate::PowerState::Running)
            || (self.predecessor_plan.action() != ProvisioningActionV1::StartPe
                && !state::stopped(&self.predecessor_power))
        {
            return Err(InvalidProvisioning);
        }
        Ok(())
    }
    fn matches_history(
        &self,
        owner: &ProvisioningCloneOwnershipV1,
        prior: &ProvisioningStageBaselineV1,
    ) -> bool {
        self.clone_request == *owner.request().clone_request()
            && self.clone_binding == *owner.request().binding()
            && fingerprint(owner.proof()).is_ok_and(|h| h == self.clone_evidence_sha256)
            && self.clone_config == *owner.config()
            && self.predecessor_binding == *prior.binding()
            && self.predecessor_plan == *prior.plan()
            && self.predecessor_evidence_sha256 == prior.evidence_sha256()
            && self.predecessor_config == *prior.config()
            && self.predecessor_power == *prior.power()
    }
}
macro_rules! owned_type {
    ($name:ident,$variant:ident,[$($action:pat_param)|+])=>{
        #[derive(Clone,Debug,Eq,PartialEq,Serialize)] #[serde(transparent)] pub struct $name(OwnedRequest);
        impl $name {
            pub fn contract_version(&self)->u16 {self.0.contract_version}
            pub fn clone_request(&self)->&CloneRequest {&self.0.clone_request}
            pub fn clone_binding(&self)->&ProvisioningBindingV1 {&self.0.clone_binding}
            pub fn clone_evidence_sha256(&self)->&str {&self.0.clone_evidence_sha256}
            pub fn clone_config(&self)->&ProvisioningVmConfigV1 {&self.0.clone_config}
            pub fn predecessor_binding(&self)->&ProvisioningBindingV1 {&self.0.predecessor_binding}
            pub fn predecessor_plan(&self)->&ProvisioningOperationPlanV1 {&self.0.predecessor_plan}
            pub fn predecessor_evidence_sha256(&self)->&str {&self.0.predecessor_evidence_sha256}
            pub fn predecessor_config(&self)->&ProvisioningVmConfigV1 {&self.0.predecessor_config}
            pub fn predecessor_power(&self)->&VmPowerStatus {&self.0.predecessor_power}
            pub fn new(i:ProvisioningOwnedRequestInputV1<'_>)->Result<Self,InvalidProvisioning> {
                if !matches!(i.plan.action(),$($action)|+) {return Err(InvalidProvisioning);} Ok(Self(OwnedRequest::new(i)?))
            }
        }
    }
}
owned_type!(
    GrowDiskRequestV1,
    GrowDisk,
    [ProvisioningActionV1::EnsureCapacity]
);
owned_type!(
    ConfigureProvisioningRequestV1,
    Configure,
    [ProvisioningActionV1::ConfigurePe | ProvisioningActionV1::ConfigureDisk]
);
owned_type!(
    StartProvisioningRequestV1,
    Start,
    [ProvisioningActionV1::StartPe | ProvisioningActionV1::StartDisk]
);
owned_type!(
    StopProvisioningRequestV1,
    Stop,
    [ProvisioningActionV1::EnsureStopped]
);
impl StartProvisioningRequestV1 {
    pub fn profile(&self) -> ProvisioningBootProfile {
        if self.plan().action() == ProvisioningActionV1::StartPe {
            ProvisioningBootProfile::PeMedia
        } else {
            ProvisioningBootProfile::InstalledDisk
        }
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", content = "request", rename_all = "snake_case")]
pub enum ProvisioningMutationRequestV1 {
    Clone(CloneProvisioningRequestV1),
    GrowDisk(GrowDiskRequestV1),
    Configure(ConfigureProvisioningRequestV1),
    Start(StartProvisioningRequestV1),
    Stop(StopProvisioningRequestV1),
}
macro_rules! getters {
    ($name:ident,$variant:ident,$($access:tt)*)=>{
        impl $name {
            pub fn binding(&self)->&ProvisioningBindingV1 {&self $($access)* .binding}
            pub fn plan(&self)->&ProvisioningOperationPlanV1 {&self $($access)* .plan}
            pub fn expected_before(&self)->&ProvisioningBeforeStateV1 {&self $($access)* .expected_before}
            pub fn operation_id(&self)->OperationId {self.binding().operation_id()}
            pub fn method(&self)->&'static str {ProvisioningMutationRequestV1::$variant(self.clone()).method()}
            pub fn path_segments(&self)->Vec<String> {ProvisioningMutationRequestV1::$variant(self.clone()).path_segments()}
            pub fn form(&self)->Vec<(&'static str,String)> {ProvisioningMutationRequestV1::$variant(self.clone()).form()}
            pub fn request_digest(&self)->Result<String,InvalidProvisioning> {ProvisioningMutationRequestV1::$variant(self.clone()).request_digest()}
        }
    }
}
getters!(CloneProvisioningRequestV1, Clone,);
getters!(GrowDiskRequestV1,GrowDisk,.0);
getters!(ConfigureProvisioningRequestV1,Configure,.0);
getters!(StartProvisioningRequestV1,Start,.0);
getters!(StopProvisioningRequestV1,Stop,.0);
impl ProvisioningMutationRequestV1 {
    pub fn binding(&self) -> &ProvisioningBindingV1 {
        match self {
            Self::Clone(r) => r.binding(),
            Self::GrowDisk(r) => r.binding(),
            Self::Configure(r) => r.binding(),
            Self::Start(r) => r.binding(),
            Self::Stop(r) => r.binding(),
        }
    }
    pub fn plan(&self) -> &ProvisioningOperationPlanV1 {
        match self {
            Self::Clone(r) => r.plan(),
            Self::GrowDisk(r) => r.plan(),
            Self::Configure(r) => r.plan(),
            Self::Start(r) => r.plan(),
            Self::Stop(r) => r.plan(),
        }
    }
    pub fn expected_before(&self) -> &ProvisioningBeforeStateV1 {
        match self {
            Self::Clone(r) => r.expected_before(),
            Self::GrowDisk(r) => r.expected_before(),
            Self::Configure(r) => r.expected_before(),
            Self::Start(r) => r.expected_before(),
            Self::Stop(r) => r.expected_before(),
        }
    }
    pub fn operation_id(&self) -> OperationId {
        self.binding().operation_id()
    }
    pub fn request_digest(&self) -> Result<String, InvalidProvisioning> {
        fingerprint(self)
    }
    pub fn method(&self) -> &'static str {
        match self {
            Self::GrowDisk(_) | Self::Configure(_) => "PUT",
            _ => "POST",
        }
    }
    pub fn path_segments(&self) -> Vec<String> {
        if let Self::Clone(r) = self {
            return r.clone_request().path_segments();
        }
        let vm = self.plan().expected().vm();
        let suffix: &[&str] = match self {
            Self::GrowDisk(_) => &["resize"],
            Self::Configure(_) => &["config"],
            Self::Start(_) => &["status", "start"],
            Self::Stop(_) => &["status", "stop"],
            _ => unreachable!(),
        };
        [
            "nodes".into(),
            vm.node().to_string(),
            "qemu".into(),
            vm.target_vmid().to_string(),
        ]
        .into_iter()
        .chain(suffix.iter().map(|s| (*s).into()))
        .collect()
    }
    pub fn form(&self) -> Vec<(&'static str, String)> {
        let e = self.plan().expected();
        let before = self.expected_before().config();
        match self {
            Self::Clone(r) => r.clone_request().form(),
            Self::GrowDisk(_) => vec![
                ("disk", "scsi0".into()),
                (
                    "size",
                    format!("{}G", e.effective_capacity_bytes() / 1_073_741_824),
                ),
                ("digest", before.digest().into()),
            ],
            Self::Configure(_) if self.plan().action() == ProvisioningActionV1::ConfigureDisk => {
                vec![
                    ("digest", before.digest().into()),
                    ("delete", "ide2,ide3".into()),
                    ("boot", "order=scsi0".into()),
                ]
            }
            Self::Configure(_) => vec![
                ("digest", before.digest().into()),
                ("cores", e.vm().cores().to_string()),
                ("memory", e.vm().memory_mib().to_string()),
                ("cpu", "host".into()),
                ("balloon", "0".into()),
                ("bios", "seabios".into()),
                ("agent", "enabled=1,type=virtio".into()),
                (
                    "smbios1",
                    format!("uuid={},serial={}", e.vm().uuid(), e.system_serial()),
                ),
                (
                    "net0",
                    format!(
                        "virtio={},bridge={},firewall=0",
                        e.vm().mac(),
                        e.vm().bridge()
                    ),
                ),
                (
                    "scsi0",
                    format!(
                        "{}:{},serial={}",
                        before.primary_disk().storage(),
                        before.primary_disk().volume(),
                        e.disk_serial()
                    ),
                ),
                ("ide2", format!("{},media=cdrom", e.deployment_iso_volid())),
                ("ide3", format!("{},media=cdrom", e.driver_iso_volid())),
                ("boot", "order=ide2;scsi0".into()),
            ],
            Self::Start(_) | Self::Stop(_) => vec![],
        }
    }
    pub(super) fn matches_history(
        &self,
        owner: &ProvisioningCloneOwnershipV1,
        prior: &ProvisioningStageBaselineV1,
    ) -> bool {
        match self {
            Self::Clone(_) => false,
            Self::GrowDisk(r) => r.0.matches_history(owner, prior),
            Self::Configure(r) => r.0.matches_history(owner, prior),
            Self::Start(r) => r.0.matches_history(owner, prior),
            Self::Stop(r) => r.0.matches_history(owner, prior),
        }
    }
}
