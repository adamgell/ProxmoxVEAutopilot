//! Exhaustive semantic comparisons: hashes/timestamps may change, every other
//! retained field must survive unless this action explicitly changes it.
use super::*;
use crate::PowerState;
pub(in crate::provisioning) fn safe_atom(s: &str, max: usize) -> bool {
    !s.is_empty()
        && s.len() <= max
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
pub(in crate::provisioning) fn safe_volume(s: &str) -> bool {
    safe_atom(s, 128)
}
pub(in crate::provisioning) fn fresh(t: DateTime<Utc>, now: DateTime<Utc>, seconds: u16) -> bool {
    (1..=300).contains(&seconds)
        && t <= now
        && now.signed_duration_since(t) <= chrono::Duration::seconds(seconds.into())
}
pub(in crate::provisioning) fn before_fresh(
    b: &ProvisioningBeforeStateV1,
    now: DateTime<Utc>,
    seconds: u16,
) -> bool {
    fresh(b.config.observed_at(), now, seconds) && fresh(b.power.observed_at(), now, seconds)
}
pub(in crate::provisioning) fn supported(c: &ProvisioningVmConfigV1) -> bool {
    !c.locked() && c.unsupported().is_empty()
}
pub(in crate::provisioning) fn stopped(p: &VmPowerStatus) -> bool {
    p.power() == PowerState::Stopped && p.locked() == Some(false)
}
pub(in crate::provisioning) fn power_valid(p: &VmPowerStatus) -> bool {
    matches!(p.power(), PowerState::Stopped | PowerState::Running) && p.locked() == Some(false)
}
pub(in crate::provisioning) fn semantic_eq(
    a: &ProvisioningVmConfigV1,
    b: &ProvisioningVmConfigV1,
) -> bool {
    equal_except(a, b, &[])
}
pub(in crate::provisioning) fn equal_except(
    a: &ProvisioningVmConfigV1,
    b: &ProvisioningVmConfigV1,
    extra: &[&str],
) -> bool {
    fn projection(c: &ProvisioningVmConfigV1, extra: &[&str]) -> serde_json::Value {
        let mut v = serde_json::to_value(c).expect("typed snapshot");
        for key in ["digest", "observed_at"]
            .into_iter()
            .chain(extra.iter().copied())
        {
            if let Some((parent, child)) = key.split_once('.') {
                v[parent]
                    .as_object_mut()
                    .expect("typed nested snapshot")
                    .remove(child);
            } else {
                v.as_object_mut()
                    .expect("typed snapshot object")
                    .remove(key);
            }
        }
        v
    }
    projection(a, extra) == projection(b, extra)
}
pub(in crate::provisioning) fn template(
    c: &ProvisioningVmConfigV1,
    p: &VmPowerStatus,
    plan: &ProvisioningOperationPlanV1,
) -> bool {
    let e = plan.expected();
    let vm = e.vm();
    c.source() == NativeEvidenceSource::FakePve
        && c.node() == vm.node()
        && c.vmid() == vm.source_vmid()
        && c.fake_clone_provenance().is_none()
        && stopped(p)
        && c.primary_disk().capacity_bytes() == e.template_capacity_bytes()
        && c.template_fingerprint()
            .is_ok_and(|h| h == e.template_config_sha256())
}
pub(in crate::provisioning) fn owned(c: &ProvisioningVmConfigV1, clone: &CloneRequest) -> bool {
    c.source() == NativeEvidenceSource::FakePve
        && c.node() == clone.vm().node()
        && c.vmid() == clone.vm().target_vmid()
        && c.name() == clone.vm().name()
        && !c.is_template()
        && supported(c)
        && c.primary_disk().storage() == clone.vm().storage()
        && c.fake_clone_provenance().is_some_and(|p| p.matches(clone))
}
pub(in crate::provisioning) fn clone_after(
    source: &ProvisioningVmConfigV1,
    c: &ProvisioningVmConfigV1,
    clone: &CloneRequest,
) -> bool {
    owned(c, clone)
        && (source.primary_disk().storage() != c.primary_disk().storage()
            || source.primary_disk().volume() != c.primary_disk().volume())
        && source.uuid() != c.uuid()
        && source.mac() != c.mac()
        && equal_except(
            source,
            c,
            &[
                "vmid",
                "name",
                "template",
                "uuid",
                "mac",
                "fake_clone_provenance",
                "primary_disk.storage",
                "primary_disk.volume",
            ],
        )
}
pub(in crate::provisioning) fn desired(
    c: &ProvisioningVmConfigV1,
    e: &ProvisioningExpectationsV1,
    profile: ProvisioningBootProfile,
) -> bool {
    c.uuid() == e.vm().uuid()
        && c.mac() == e.vm().mac()
        && c.bridge() == e.vm().bridge()
        && c.cores() == e.vm().cores()
        && c.memory_mib() == e.vm().memory_mib()
        && c.system_serial() == Some(e.system_serial())
        && c.primary_disk().serial() == Some(e.disk_serial())
        && c.primary_disk().capacity_bytes() == e.effective_capacity_bytes()
        && c.firmware() == ProvisioningFirmwareV1::Seabios
        && c.cpu() == ProvisioningCpuV1::Host
        && c.balloon_mib() == 0
        && c.qga_enabled()
        && c.qga_channel() == ProvisioningQgaChannelV1::Virtio
        && c.boot_profile() == Some(profile)
        && match profile {
            ProvisioningBootProfile::PeMedia => {
                c.deployment_iso().volid() == Some(e.deployment_iso_volid())
                    && c.driver_iso().volid() == Some(e.driver_iso_volid())
            }
            ProvisioningBootProfile::InstalledDisk => {
                c.deployment_iso() == &ProvisioningMediaSlotV1::Absent
                    && c.driver_iso() == &ProvisioningMediaSlotV1::Absent
            }
        }
}
pub(in crate::provisioning) fn predecessor(
    action: ProvisioningActionV1,
) -> Option<ProvisioningActionV1> {
    use ProvisioningActionV1::*;
    match action {
        Clone => None,
        EnsureCapacity => Some(Clone),
        ConfigurePe => Some(EnsureCapacity),
        StartPe => Some(ConfigurePe),
        EnsureStopped => Some(StartPe),
        ConfigureDisk => Some(EnsureStopped),
        StartDisk => Some(ConfigureDisk),
    }
}
pub(in crate::provisioning) fn stage_before(
    plan: &ProvisioningOperationPlanV1,
    clone: &CloneRequest,
    original: &ProvisioningVmConfigV1,
    prior: &ProvisioningVmConfigV1,
    current: &ProvisioningVmConfigV1,
) -> bool {
    use ProvisioningActionV1::*;
    if !owned(original, clone)
        || !owned(prior, clone)
        || !owned(current, clone)
        || original.primary_disk().storage() != current.primary_disk().storage()
        || original.primary_disk().volume() != current.primary_disk().volume()
    {
        return false;
    }
    let e = plan.expected();
    match plan.action() {
        EnsureCapacity => {
            semantic_eq(original, prior)
                && equal_except(prior, current, &["primary_disk.capacity_bytes"])
                && [e.template_capacity_bytes(), e.effective_capacity_bytes()]
                    .contains(&current.primary_disk().capacity_bytes())
        }
        ConfigurePe => {
            equal_except(original, prior, &["primary_disk.capacity_bytes"])
                && prior.primary_disk().capacity_bytes() == e.effective_capacity_bytes()
                && semantic_eq(prior, current)
        }
        StartPe | EnsureStopped | ConfigureDisk => {
            desired(prior, e, ProvisioningBootProfile::PeMedia) && semantic_eq(prior, current)
        }
        StartDisk => {
            desired(prior, e, ProvisioningBootProfile::InstalledDisk) && semantic_eq(prior, current)
        }
        Clone => false,
    }
}
pub(in crate::provisioning) fn after(
    plan: &ProvisioningOperationPlanV1,
    before: &ProvisioningVmConfigV1,
    current: &ProvisioningVmConfigV1,
) -> bool {
    use ProvisioningActionV1::*;
    let e = plan.expected();
    match plan.action() {
        EnsureCapacity => {
            equal_except(before, current, &["primary_disk.capacity_bytes"])
                && current.primary_disk().capacity_bytes() == e.effective_capacity_bytes()
        }
        ConfigurePe => {
            desired(current, e, ProvisioningBootProfile::PeMedia)
                && equal_except(
                    before,
                    current,
                    &[
                        "cores",
                        "memory_mib",
                        "uuid",
                        "mac",
                        "bridge",
                        "system_serial",
                        "primary_disk.serial",
                        "firmware",
                        "cpu",
                        "balloon_mib",
                        "qga_enabled",
                        "qga_channel",
                        "deployment_iso",
                        "driver_iso",
                        "boot_profile",
                    ],
                )
        }
        ConfigureDisk => {
            desired(current, e, ProvisioningBootProfile::InstalledDisk)
                && equal_except(
                    before,
                    current,
                    &["deployment_iso", "driver_iso", "boot_profile"],
                )
        }
        StartPe | StartDisk | EnsureStopped => semantic_eq(before, current),
        Clone => false,
    }
}
