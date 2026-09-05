use super::*;
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningIdentitySnapshotV1 {
    pub(super) contract_version: u16,
    pub(super) node: NodeName,
    pub(super) vmid: Vmid,
    pub(super) source: NativeEvidenceSource,
    pub(super) name: NativeVmName,
    pub(super) is_template: bool,
    pub(super) config_digest: String,
    pub(super) uuid: VmUuid,
    pub(super) mac: MacAddress,
    pub(super) primary_storage: StorageName,
    pub(super) primary_volume: String,
    pub(super) coverage: ProvisioningCoverageV1,
    pub(super) observed_at: DateTime<Utc>,
}
impl ProvisioningIdentitySnapshotV1 {
    pub fn from_provisioning(c: &ProvisioningVmConfigV1) -> Self {
        Self {
            contract_version: 1,
            node: c.node().clone(),
            vmid: c.vmid(),
            source: c.source(),
            name: c.name().clone(),
            is_template: c.is_template(),
            config_digest: c.digest().into(),
            uuid: c.uuid(),
            mac: c.mac().clone(),
            primary_storage: c.primary_disk().storage().clone(),
            primary_volume: c.primary_disk().volume().into(),
            coverage: if c.unsupported().is_empty() {
                ProvisioningCoverageV1::Complete
            } else {
                ProvisioningCoverageV1::Partial
            },
            observed_at: c.observed_at(),
        }
    }
    pub fn from_native(
        c: &NativeVmConfig,
        source: NativeEvidenceSource,
    ) -> Result<Self, InvalidProvisioning> {
        if source == NativeEvidenceSource::PveApi && c.fake_clone_provenance().is_some() {
            return Err(InvalidProvisioning);
        }
        Ok(Self {
            contract_version: 1,
            node: c.node().clone(),
            vmid: c.vmid(),
            source,
            name: c.name().clone(),
            is_template: c.is_template(),
            config_digest: c.digest().into(),
            uuid: c.uuid(),
            mac: c.mac().clone(),
            primary_storage: c.boot_disk().storage().clone(),
            primary_volume: c.boot_disk().volume().into(),
            coverage: if c.unsupported().is_empty() {
                ProvisioningCoverageV1::Complete
            } else {
                ProvisioningCoverageV1::Partial
            },
            observed_at: c.observed_at(),
        })
    }
    pub fn contract_version(&self) -> u16 {
        self.contract_version
    }
    pub fn node(&self) -> &NodeName {
        &self.node
    }
    pub fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub fn source(&self) -> NativeEvidenceSource {
        self.source
    }
    pub fn name(&self) -> &NativeVmName {
        &self.name
    }
    pub fn is_template(&self) -> bool {
        self.is_template
    }
    pub fn config_digest(&self) -> &str {
        &self.config_digest
    }
    pub fn uuid(&self) -> VmUuid {
        self.uuid
    }
    pub fn mac(&self) -> &MacAddress {
        &self.mac
    }
    pub fn primary_storage(&self) -> &StorageName {
        &self.primary_storage
    }
    pub fn primary_volume(&self) -> &str {
        &self.primary_volume
    }
    pub fn coverage(&self) -> ProvisioningCoverageV1 {
        self.coverage
    }
    pub fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningIdentityReadV1 {
    pub(super) node: NodeName,
    pub(super) vmid: Vmid,
    pub(super) read: NativeRead<ProvisioningIdentitySnapshotV1>,
}
impl ProvisioningIdentityReadV1 {
    pub fn new(
        node: NodeName,
        vmid: Vmid,
        read: NativeRead<ProvisioningIdentitySnapshotV1>,
    ) -> Result<Self, InvalidProvisioning> {
        if read
            .result
            .as_ref()
            .is_ok_and(|c| c.node() != &node || c.vmid() != vmid)
        {
            return Err(InvalidProvisioning);
        }
        Ok(Self { node, vmid, read })
    }
    pub fn node(&self) -> &NodeName {
        &self.node
    }
    pub fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub fn read(&self) -> &NativeRead<ProvisioningIdentitySnapshotV1> {
        &self.read
    }
}
