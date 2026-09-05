use super::expectations::{is_serial, media_storage};
use super::*;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

impl ProvisioningVmConfigV1 {
    /// Parse the explicit synthetic wire contract. `Value` has already lost any
    /// duplicate top-level JSON keys; comma properties are checked here before
    /// projection. Persisted JSON has a separate strict struct decoder.
    pub fn from_wire(
        node: NodeName,
        vmid: Vmid,
        source: NativeEvidenceSource,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = data.as_object().ok_or(PveReadError::InvalidResponse)?;
        if data
            .get("node")
            .is_some_and(|v| v.as_str() != Some(node.as_str()))
            || data
                .get("vmid")
                .is_some_and(|v| v.as_u64() != Some(u64::from(vmid.get())))
        {
            return Err(PveReadError::InvalidResponse);
        }
        let mut unsupported = BTreeSet::new();
        let (disk, disk_props) = disk_parts(text(data, "scsi0")?)?;
        let (storage, volume) = disk.split_once(':').ok_or(PveReadError::InvalidResponse)?;
        let primary_disk = ProvisioningPrimaryDiskV1 {
            storage: StorageName::parse(storage).map_err(|_| PveReadError::InvalidResponse)?,
            volume: volume.to_owned(),
            capacity_bytes: capacity(required(&disk_props, "size")?)?,
            serial: serial_property(&disk_props)?,
        };
        if disk_props.keys().any(|k| !matches!(*k, "size" | "serial")) {
            unsupported.insert(ProvisioningUnsupportedV1::DiskProperties);
        }
        let smbios = properties(text(data, "smbios1")?)?;
        let uuid =
            VmUuid::parse(required(&smbios, "uuid")?).map_err(|_| PveReadError::InvalidResponse)?;
        let system_serial = serial_property(&smbios)?;
        if smbios.keys().any(|k| !matches!(*k, "uuid" | "serial")) {
            unsupported.insert(ProvisioningUnsupportedV1::SmbiosProperties);
        }
        let nic = properties(text(data, "net0")?)?;
        let mac = MacAddress::parse(required(&nic, "virtio")?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        let bridge = BridgeName::parse(required(&nic, "bridge")?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        if nic
            .iter()
            .any(|(k, v)| !matches!((*k, *v), ("virtio" | "bridge", _) | ("firewall", "0")))
        {
            unsupported.insert(ProvisioningUnsupportedV1::NicProperties);
        }
        let agent = properties(text(data, "agent")?)?;
        let qga_enabled = match required(&agent, "enabled")? {
            "0" => false,
            "1" => true,
            _ => return Err(PveReadError::InvalidResponse),
        };
        let qga_channel = match required(&agent, "type")? {
            "virtio" => ProvisioningQgaChannelV1::Virtio,
            _ => ProvisioningQgaChannelV1::Unsupported,
        };
        if agent.keys().any(|k| !matches!(*k, "enabled" | "type")) {
            unsupported.insert(ProvisioningUnsupportedV1::AgentProperties);
        }
        let boot = properties(text(data, "boot")?)?;
        let boot_profile = if boot.len() == 1 {
            match boot.get("order") {
                Some(&"scsi0") => Some(ProvisioningBootProfile::InstalledDisk),
                Some(&"ide2;scsi0") => Some(ProvisioningBootProfile::PeMedia),
                _ => None,
            }
        } else {
            None
        };
        let firmware = match text(data, "bios")? {
            "seabios" => ProvisioningFirmwareV1::Seabios,
            _ => ProvisioningFirmwareV1::Unsupported,
        };
        let cpu = match text(data, "cpu")? {
            "host" => ProvisioningCpuV1::Host,
            _ => ProvisioningCpuV1::Unsupported,
        };
        let locked = match data.get("lock") {
            None => false,
            Some(v) if v.as_str().is_some_and(|s| safe_atom(s, 64)) => true,
            Some(_) => return Err(PveReadError::InvalidResponse),
        };
        let fake_clone_provenance = match data.get("fake_clone_provenance") {
            None => None,
            Some(_) if source == NativeEvidenceSource::PveApi => {
                return Err(PveReadError::InvalidResponse);
            }
            Some(v) if !v.is_object() => return Err(PveReadError::InvalidResponse),
            Some(v) => {
                Some(serde_json::from_value(v.clone()).map_err(|_| PveReadError::InvalidResponse)?)
            }
        };
        for key in data.keys() {
            if matches!(
                key.as_str(),
                "node"
                    | "vmid"
                    | "digest"
                    | "name"
                    | "cores"
                    | "memory"
                    | "scsi0"
                    | "smbios1"
                    | "net0"
                    | "bios"
                    | "cpu"
                    | "balloon"
                    | "agent"
                    | "boot"
                    | "template"
                    | "lock"
                    | "ide2"
                    | "ide3"
                    | "fake_clone_provenance"
            ) {
                continue;
            }
            let reason = if device_key(key, "net") {
                properties(text(data, key)?)?;
                ProvisioningUnsupportedV1::AdditionalNic
            } else if [
                "scsi", "sata", "ide", "virtio", "efidisk", "tpmstate", "unused",
            ]
            .iter()
            .any(|p| device_key(key, p))
            {
                disk_parts(text(data, key)?)?;
                if device_key(key, "efidisk") {
                    ProvisioningUnsupportedV1::EfiDisk
                } else if device_key(key, "tpmstate") {
                    ProvisioningUnsupportedV1::TpmState
                } else if device_key(key, "unused") {
                    ProvisioningUnsupportedV1::UnusedDisk
                } else {
                    ProvisioningUnsupportedV1::AdditionalDisk
                }
            } else if key == "args" {
                text(data, key)?;
                ProvisioningUnsupportedV1::Args
            } else if key == "oem" || key == "smbios0" {
                text(data, key)?;
                ProvisioningUnsupportedV1::Oem
            } else {
                ProvisioningUnsupportedV1::UnknownField
            };
            unsupported.insert(reason);
        }
        let mut config = Self {
            contract_version: 1,
            node,
            vmid,
            source,
            digest: text(data, "digest")?.to_owned(),
            name: NativeVmName::parse(text(data, "name")?)
                .map_err(|_| PveReadError::InvalidResponse)?,
            cores: u32::try_from(number(data, "cores")?)
                .map_err(|_| PveReadError::InvalidResponse)?,
            memory_mib: number(data, "memory")?,
            primary_disk,
            uuid,
            mac,
            bridge,
            system_serial,
            firmware,
            cpu,
            balloon_mib: number(data, "balloon")?,
            qga_enabled,
            qga_channel,
            boot_profile,
            deployment_iso: media(data, "ide2")?,
            driver_iso: media(data, "ide3")?,
            template: match number(data, "template")? {
                0 => false,
                1 => true,
                _ => return Err(PveReadError::InvalidResponse),
            },
            locked,
            unsupported,
            fake_clone_provenance,
            observed_at,
        };
        for (reason, present) in config.projected_unsupported() {
            if present {
                config.unsupported.insert(reason);
            }
        }
        config.validate()?;
        Ok(config)
    }
    /// Observed configuration identity, not blank-disk, freshness, ownership,
    /// publication, or dispatch authority. Concurrency/freshness remain separate.
    pub fn template_fingerprint(&self) -> Result<String, InvalidProvisioning> {
        if !self.template
            || self.locked
            || !self.unsupported.is_empty()
            || self.boot_profile != Some(ProvisioningBootProfile::InstalledDisk)
            || self.deployment_iso != ProvisioningMediaSlotV1::Absent
            || self.driver_iso != ProvisioningMediaSlotV1::Absent
        {
            return Err(InvalidProvisioning);
        }
        fingerprint(
            &json!({"contract_version":1,"node":self.node,"vmid":self.vmid,"template":true,
            "name":self.name,"cores":self.cores,"memory_mib":self.memory_mib,
            "primary_disk":{"slot":"scsi0","storage":self.primary_disk.storage,"volume":self.primary_disk.volume,
                "capacity_bytes":self.primary_disk.capacity_bytes,"serial":self.primary_disk.serial},
            "uuid":self.uuid,"mac":self.mac,"bridge":self.bridge,"system_serial":self.system_serial,
            "firmware":"seabios","cpu":"host","balloon_mib":0,"qga_enabled":self.qga_enabled,"qga_channel":"virtio",
            "boot_profile":"installed_disk","deployment_iso":null,"driver_iso":null,
            "template_device_policy":"reject_extra_devices","evidence_level":"observed_configuration"}),
        )
    }
    pub(super) fn validate(&self) -> Result<(), PveReadError> {
        if self.contract_version != 1
            || !safe_atom(&self.digest, 256)
            || !(1..=128).contains(&self.cores)
            || !(512..=1_048_576).contains(&self.memory_mib)
            || !self.memory_mib.is_multiple_of(128)
            || self.system_serial.as_deref().is_some_and(|s| !is_serial(s))
            || self.fake_clone_provenance.as_ref().is_some_and(|p| {
                self.source != NativeEvidenceSource::FakePve || p.target_vmid() != self.vmid
            })
            || self
                .projected_unsupported()
                .iter()
                .any(|(r, present)| self.unsupported.contains(r) != *present)
        {
            return Err(PveReadError::InvalidResponse);
        }
        self.primary_disk.validate()?;
        for slot in [&self.deployment_iso, &self.driver_iso] {
            if slot.volid().is_some_and(|v| media_storage(v).is_none()) {
                return Err(PveReadError::InvalidResponse);
            }
        }
        Ok(())
    }
    fn projected_unsupported(&self) -> [(ProvisioningUnsupportedV1, bool); 7] {
        use ProvisioningUnsupportedV1 as R;
        [
            (
                R::Firmware,
                self.firmware == ProvisioningFirmwareV1::Unsupported,
            ),
            (R::Cpu, self.cpu == ProvisioningCpuV1::Unsupported),
            (R::Balloon, self.balloon_mib != 0),
            (
                R::QgaChannel,
                self.qga_channel == ProvisioningQgaChannelV1::Unsupported,
            ),
            (R::BootOrder, self.boot_profile.is_none()),
            (
                R::DeploymentMedia,
                self.deployment_iso == ProvisioningMediaSlotV1::Unsupported,
            ),
            (
                R::DriverMedia,
                self.driver_iso == ProvisioningMediaSlotV1::Unsupported,
            ),
        ]
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
    pub fn digest(&self) -> &str {
        &self.digest
    }
    pub fn name(&self) -> &NativeVmName {
        &self.name
    }
    pub fn cores(&self) -> u32 {
        self.cores
    }
    pub fn memory_mib(&self) -> u64 {
        self.memory_mib
    }
    pub fn primary_disk(&self) -> &ProvisioningPrimaryDiskV1 {
        &self.primary_disk
    }
    pub fn uuid(&self) -> VmUuid {
        self.uuid
    }
    pub fn mac(&self) -> &MacAddress {
        &self.mac
    }
    pub fn bridge(&self) -> &BridgeName {
        &self.bridge
    }
    pub fn system_serial(&self) -> Option<&str> {
        self.system_serial.as_deref()
    }
    pub fn firmware(&self) -> ProvisioningFirmwareV1 {
        self.firmware
    }
    pub fn cpu(&self) -> ProvisioningCpuV1 {
        self.cpu
    }
    pub fn balloon_mib(&self) -> u64 {
        self.balloon_mib
    }
    pub fn qga_enabled(&self) -> bool {
        self.qga_enabled
    }
    pub fn qga_channel(&self) -> ProvisioningQgaChannelV1 {
        self.qga_channel
    }
    pub fn boot_profile(&self) -> Option<ProvisioningBootProfile> {
        self.boot_profile
    }
    pub fn deployment_iso(&self) -> &ProvisioningMediaSlotV1 {
        &self.deployment_iso
    }
    pub fn driver_iso(&self) -> &ProvisioningMediaSlotV1 {
        &self.driver_iso
    }
    pub fn is_template(&self) -> bool {
        self.template
    }
    pub fn locked(&self) -> bool {
        self.locked
    }
    pub fn unsupported(&self) -> &BTreeSet<ProvisioningUnsupportedV1> {
        &self.unsupported
    }
    pub fn fake_clone_provenance(&self) -> Option<&FakeCloneProvenance> {
        self.fake_clone_provenance.as_ref()
    }
    pub fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}

impl ProvisioningPrimaryDiskV1 {
    pub(super) fn validate(&self) -> Result<(), PveReadError> {
        if !safe_atom(&self.volume, 128)
            || self.capacity_bytes == 0
            || self.serial.as_deref().is_some_and(|s| !is_serial(s))
        {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(())
    }
    pub fn storage(&self) -> &StorageName {
        &self.storage
    }
    pub fn volume(&self) -> &str {
        &self.volume
    }
    pub fn capacity_bytes(&self) -> u64 {
        self.capacity_bytes
    }
    pub fn serial(&self) -> Option<&str> {
        self.serial.as_deref()
    }
}

impl ProvisioningMediaInventoryV1 {
    pub fn new(
        node: NodeName,
        storage: StorageName,
        iso_volids: Vec<String>,
        coverage: ProvisioningCoverageV1,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let mut unique = BTreeSet::new();
        if iso_volids.len() > 1024
            || iso_volids
                .iter()
                .any(|v| media_storage(v).as_ref() != Some(&storage) || !unique.insert(v))
        {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            contract_version: 1,
            node,
            storage,
            iso_volids,
            coverage,
            observed_at,
        })
    }
    pub fn node(&self) -> &NodeName {
        &self.node
    }
    pub fn storage(&self) -> &StorageName {
        &self.storage
    }
    pub fn iso_volids(&self) -> &[String] {
        &self.iso_volids
    }
    pub fn coverage(&self) -> ProvisioningCoverageV1 {
        self.coverage
    }
    pub fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}

fn safe_atom(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn text<'a>(data: &'a Map<String, Value>, key: &str) -> Result<&'a str, PveReadError> {
    data.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or(PveReadError::InvalidResponse)
}
fn number(data: &Map<String, Value>, key: &str) -> Result<u64, PveReadError> {
    data.get(key)
        .and_then(Value::as_u64)
        .ok_or(PveReadError::InvalidResponse)
}
fn properties(value: &str) -> Result<BTreeMap<&str, &str>, PveReadError> {
    let mut props = BTreeMap::new();
    for prop in value.split(',') {
        let (key, value) = prop.split_once('=').ok_or(PveReadError::InvalidResponse)?;
        if key.is_empty() || value.is_empty() || props.insert(key, value).is_some() {
            return Err(PveReadError::InvalidResponse);
        }
    }
    Ok(props)
}
fn required<'a>(props: &BTreeMap<&str, &'a str>, key: &str) -> Result<&'a str, PveReadError> {
    props.get(key).copied().ok_or(PveReadError::InvalidResponse)
}
fn serial_property(props: &BTreeMap<&str, &str>) -> Result<Option<String>, PveReadError> {
    match props.get("serial") {
        None => Ok(None),
        Some(value) if is_serial(value) => Ok(Some((*value).to_owned())),
        Some(_) => Err(PveReadError::InvalidResponse),
    }
}
fn disk_parts(value: &str) -> Result<(&str, BTreeMap<&str, &str>), PveReadError> {
    let (volume, props) = match value.split_once(',') {
        Some((volume, options)) => (volume, properties(options)?),
        None => (value, BTreeMap::new()),
    };
    if volume.is_empty() || volume.contains('=') {
        return Err(PveReadError::InvalidResponse);
    }
    Ok((volume, props))
}
fn capacity(value: &str) -> Result<u64, PveReadError> {
    let (digits, multiplier) = match value.as_bytes().last() {
        Some(b'K') => (&value[..value.len() - 1], 1024),
        Some(b'M') => (&value[..value.len() - 1], 1_048_576),
        Some(b'G') => (&value[..value.len() - 1], 1_073_741_824),
        Some(b'T') => (&value[..value.len() - 1], 1_099_511_627_776),
        _ => (value, 1),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PveReadError::InvalidResponse);
    }
    digits
        .parse::<u64>()
        .ok()
        .and_then(|n| n.checked_mul(multiplier))
        .filter(|n| *n > 0)
        .ok_or(PveReadError::InvalidResponse)
}
fn media(data: &Map<String, Value>, key: &str) -> Result<ProvisioningMediaSlotV1, PveReadError> {
    if !data.contains_key(key) {
        return Ok(ProvisioningMediaSlotV1::Absent);
    }
    let (volid, props) = disk_parts(text(data, key)?)?;
    if volid != "none" && media_storage(volid).is_none() {
        return Err(PveReadError::InvalidResponse);
    }
    if volid == "none" || props.len() != 1 || props.get("media") != Some(&"cdrom") {
        Ok(ProvisioningMediaSlotV1::Unsupported)
    } else {
        Ok(ProvisioningMediaSlotV1::Iso {
            volid: volid.to_owned(),
        })
    }
}
fn device_key(key: &str, prefix: &str) -> bool {
    key.strip_prefix(prefix)
        .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
}

impl ProvisioningQgaObservationV1 {
    pub fn new(node: NodeName, vmid: Vmid, reachable: bool, observed_at: DateTime<Utc>) -> Self {
        Self {
            contract_version: 1,
            node,
            vmid,
            reachable,
            observed_at,
        }
    }
    pub fn node(&self) -> &NodeName {
        &self.node
    }
    pub fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub fn reachable(&self) -> bool {
        self.reachable
    }
    pub fn observed_at(&self) -> DateTime<Utc> {
        self.observed_at
    }
}
