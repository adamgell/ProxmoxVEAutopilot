//! Private persisted wire shapes reconstruct validated values and erase error
//! payloads. Raw config parsing is a distinct boundary.
use super::*;
use serde::{Deserializer, de};

macro_rules! fixed_enum {
    ($ty:ty, {$($wire:literal => $variant:ident),+ $(,)?}) => {
        impl<'de> Deserialize<'de> for $ty {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self,D::Error> {
                let value = String::deserialize(d).map_err(|_| de::Error::custom(InvalidProvisioning))?;
                match value.as_str() {
                    $($wire => Ok(Self::$variant),)+
                    _ => Err(de::Error::custom(InvalidProvisioning)),
                }
            }
        }
    };
}
fixed_enum!(ProvisioningActionV1, {"clone"=>Clone,"ensure_capacity"=>EnsureCapacity,
    "configure_pe"=>ConfigurePe,"start_pe"=>StartPe,"ensure_stopped"=>EnsureStopped,
    "configure_disk"=>ConfigureDisk,"start_disk"=>StartDisk});
fixed_enum!(ProvisioningBootProfile, {"pe_media"=>PeMedia,"installed_disk"=>InstalledDisk});
fixed_enum!(ProvisioningFirmwareV1, {"seabios"=>Seabios,"unsupported"=>Unsupported});
fixed_enum!(ProvisioningCpuV1, {"host"=>Host,"unsupported"=>Unsupported});
fixed_enum!(ProvisioningQgaChannelV1, {"virtio"=>Virtio,"unsupported"=>Unsupported});
fixed_enum!(ProvisioningCoverageV1, {"complete"=>Complete,"partial"=>Partial});
fixed_enum!(ProvisioningUnsupportedV1, {"firmware"=>Firmware,"cpu"=>Cpu,"balloon"=>Balloon,
    "qga_channel"=>QgaChannel,"agent_properties"=>AgentProperties,"boot_order"=>BootOrder,
    "deployment_media"=>DeploymentMedia,"driver_media"=>DriverMedia,"disk_properties"=>DiskProperties,
    "smbios_properties"=>SmbiosProperties,"nic_properties"=>NicProperties,"additional_nic"=>AdditionalNic,
    "additional_disk"=>AdditionalDisk,"efi_disk"=>EfiDisk,"tpm_state"=>TpmState,"unused_disk"=>UnusedDisk,
    "oem"=>Oem,"args"=>Args,"unknown_field"=>UnknownField});

// Derived structs also admit positional arrays. This contract requires JSON
// objects, preserving map access so duplicate keys still reach the strict wire.
fn object<'de, D, T>(d: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    struct Object<T>(std::marker::PhantomData<T>);
    impl<'de, T: Deserialize<'de>> de::Visitor<'de> for Object<T> {
        type Value = T;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("provisioning object")
        }
        fn visit_map<A: de::MapAccess<'de>>(self, access: A) -> Result<T, A::Error> {
            T::deserialize(de::value::MapAccessDeserializer::new(access))
        }
    }
    d.deserialize_map(Object(std::marker::PhantomData))
}

// `deserialize_with` without `default` makes these nullable fields required.
// An ordinary Option field would silently admit a missing observation key.
fn required_nullable<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

fn required_nullable_object<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    struct ObjectOnly<T>(T);
    impl<'de, T: Deserialize<'de>> Deserialize<'de> for ObjectOnly<T> {
        fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
            object(d).map(Self)
        }
    }
    Option::<ObjectOnly<T>>::deserialize(d).map(|v| v.map(|v| v.0))
}

impl<'de> Deserialize<'de> for ProvisioningMediaSlotV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            // A derived unit enum also accepts externally tagged objects.
            // The persisted media discriminator is strictly a JSON string.
            state: String,
            #[serde(default, deserialize_with = "present_string")]
            volid: Option<String>,
        }
        // An explicit null is not an absent field, and unit variants must not
        // ignore extra fields (internally tagged serde unit variants can do so).
        fn present_string<'de, D: Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
            String::deserialize(d).map(Some)
        }
        let wire =
            object::<D, Wire>(d).map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
        match (wire.state.as_str(), wire.volid) {
            ("absent", None) => Ok(Self::Absent),
            ("unsupported", None) => Ok(Self::Unsupported),
            ("iso", Some(volid)) if expectations::media_storage(&volid).is_some() => {
                Ok(Self::Iso { volid })
            }
            _ => Err(de::Error::custom(PveReadError::InvalidResponse)),
        }
    }
}

impl<'de> Deserialize<'de> for ProvisioningPrimaryDiskV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            storage: StorageName,
            volume: String,
            capacity_bytes: u64,
            #[serde(deserialize_with = "required_nullable")]
            serial: Option<String>,
        }
        let w =
            object::<D, Wire>(d).map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
        let disk = Self {
            storage: w.storage,
            volume: w.volume,
            capacity_bytes: w.capacity_bytes,
            serial: w.serial,
        };
        disk.validate().map_err(de::Error::custom)?;
        Ok(disk)
    }
}

// The new snapshot requires a string discriminator while the legacy source
// enum retains its historical derived decoding behavior.
fn provisioning_source<'de, D: Deserializer<'de>>(d: D) -> Result<NativeEvidenceSource, D::Error> {
    let source =
        String::deserialize(d).map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
    match source.as_str() {
        "pve_api" => Ok(NativeEvidenceSource::PveApi),
        "fake_pve" => Ok(NativeEvidenceSource::FakePve),
        _ => Err(de::Error::custom(PveReadError::InvalidResponse)),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigWire {
    contract_version: u16,
    node: NodeName,
    vmid: Vmid,
    #[serde(deserialize_with = "provisioning_source")]
    source: NativeEvidenceSource,
    digest: String,
    name: NativeVmName,
    cores: u32,
    memory_mib: u64,
    primary_disk: ProvisioningPrimaryDiskV1,
    uuid: VmUuid,
    mac: MacAddress,
    bridge: BridgeName,
    #[serde(deserialize_with = "required_nullable")]
    system_serial: Option<String>,
    firmware: ProvisioningFirmwareV1,
    cpu: ProvisioningCpuV1,
    balloon_mib: u64,
    qga_enabled: bool,
    qga_channel: ProvisioningQgaChannelV1,
    #[serde(deserialize_with = "required_nullable")]
    boot_profile: Option<ProvisioningBootProfile>,
    deployment_iso: ProvisioningMediaSlotV1,
    driver_iso: ProvisioningMediaSlotV1,
    template: bool,
    locked: bool,
    unsupported: Vec<ProvisioningUnsupportedV1>,
    #[serde(deserialize_with = "required_nullable_object")]
    fake_clone_provenance: Option<FakeCloneProvenance>,
    observed_at: DateTime<Utc>,
}
impl<'de> Deserialize<'de> for ProvisioningVmConfigV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let w = object::<D, ConfigWire>(d)
            .map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
        let unsupported: BTreeSet<_> = w.unsupported.iter().copied().collect();
        if unsupported.len() != w.unsupported.len() {
            return Err(de::Error::custom(PveReadError::InvalidResponse));
        }
        let config = Self {
            contract_version: w.contract_version,
            node: w.node,
            vmid: w.vmid,
            source: w.source,
            digest: w.digest,
            name: w.name,
            cores: w.cores,
            memory_mib: w.memory_mib,
            primary_disk: w.primary_disk,
            uuid: w.uuid,
            mac: w.mac,
            bridge: w.bridge,
            system_serial: w.system_serial,
            firmware: w.firmware,
            cpu: w.cpu,
            balloon_mib: w.balloon_mib,
            qga_enabled: w.qga_enabled,
            qga_channel: w.qga_channel,
            boot_profile: w.boot_profile,
            deployment_iso: w.deployment_iso,
            driver_iso: w.driver_iso,
            template: w.template,
            locked: w.locked,
            unsupported,
            fake_clone_provenance: w.fake_clone_provenance,
            observed_at: w.observed_at,
        };
        config.validate().map_err(de::Error::custom)?;
        Ok(config)
    }
}

impl<'de> Deserialize<'de> for ProvisioningMediaInventoryV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            contract_version: u16,
            node: NodeName,
            storage: StorageName,
            iso_volids: Vec<String>,
            coverage: ProvisioningCoverageV1,
            observed_at: DateTime<Utc>,
        }
        let w =
            object::<D, Wire>(d).map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
        if w.contract_version != 1 {
            return Err(de::Error::custom(PveReadError::InvalidResponse));
        }
        Self::new(w.node, w.storage, w.iso_volids, w.coverage, w.observed_at)
            .map_err(de::Error::custom)
    }
}

impl<'de> Deserialize<'de> for ProvisioningQgaObservationV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            contract_version: u16,
            node: NodeName,
            vmid: Vmid,
            reachable: bool,
            observed_at: DateTime<Utc>,
        }
        let w =
            object::<D, Wire>(d).map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
        if w.contract_version != 1 {
            return Err(de::Error::custom(PveReadError::InvalidResponse));
        }
        Ok(Self::new(w.node, w.vmid, w.reachable, w.observed_at))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpectationsWire {
    contract_version: u16,
    #[serde(deserialize_with = "object")]
    vm: NativeVmPlan,
    template_config_sha256: String,
    template_capacity_bytes: u64,
    effective_capacity_bytes: u64,
    system_serial: String,
    disk_serial: String,
    deployment_iso_volid: String,
    driver_iso_volid: String,
}

impl<'de> Deserialize<'de> for ProvisioningExpectationsV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let w =
            object::<D, ExpectationsWire>(d).map_err(|_| de::Error::custom(InvalidProvisioning))?;
        if w.contract_version != 1 {
            return Err(de::Error::custom(InvalidProvisioning));
        }
        Self::new(ProvisioningExpectationsInputV1 {
            vm: w.vm,
            template_config_sha256: &w.template_config_sha256,
            template_capacity_bytes: w.template_capacity_bytes,
            effective_capacity_bytes: w.effective_capacity_bytes,
            system_serial: &w.system_serial,
            disk_serial: &w.disk_serial,
            deployment_iso_volid: &w.deployment_iso_volid,
            driver_iso_volid: &w.driver_iso_volid,
        })
        .map_err(de::Error::custom)
    }
}

impl<'de> Deserialize<'de> for ProvisioningOperationPlanV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            contract_version: u16,
            action: ProvisioningActionV1,
            expected: ProvisioningExpectationsV1,
        }
        let w = object::<D, Wire>(d).map_err(|_| de::Error::custom(InvalidProvisioning))?;
        if w.contract_version != 1 {
            return Err(de::Error::custom(InvalidProvisioning));
        }
        Ok(Self::new(w.action, w.expected))
    }
}
