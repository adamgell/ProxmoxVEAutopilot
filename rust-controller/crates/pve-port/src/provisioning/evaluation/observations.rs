use super::*;
use crate::{BridgeInventory, ClusterVmInventory, NodeStatus, StorageStatus};
pub(super) fn unauthorized(e: &ProvisioningEvidenceInputV1) -> bool {
    fn bad<T>(r: &Option<NativeRead<T>>) -> bool {
        r.as_ref()
            .is_some_and(|r| matches!(r.result, Err(PveReadError::Unauthorized)))
    }
    bad(&e.node)
        || bad(&e.storage)
        || bad(&e.bridges)
        || bad(&e.inventory)
        || bad(&e.source_config)
        || bad(&e.source_power)
        || bad(&e.target_config)
        || bad(&e.target_power)
        || bad(&e.task)
        || bad(&e.qga)
        || e.identities
            .iter()
            .any(|i| i.read().result == Err(PveReadError::Unauthorized))
        || e.media
            .iter()
            .any(|i| i.result == Err(PveReadError::Unauthorized))
}
pub(super) fn clock(
    snapshot: DateTime<Utc>,
    wrapper: DateTime<Utc>,
    c: &ProvisioningEvaluationContextInputV1,
    e: &ProvisioningEvidenceInputV1,
    now: DateTime<Utc>,
    minimum: Option<DateTime<Utc>>,
) -> Check<()> {
    if !physical::fresh(wrapper, now, c.freshness_seconds)
        || !physical::fresh(snapshot, now, c.freshness_seconds)
        || snapshot > wrapper
        || wrapper > e.collected_at
        || minimum.is_some_and(|m| snapshot < m || wrapper < m)
    {
        return Err(unknown(ProvisioningReasonV1::ObservationNotFresh));
    }
    Ok(())
}
pub(super) fn read<'a, T>(
    r: &'a Option<NativeRead<T>>,
    c: &ProvisioningEvaluationContextInputV1,
    e: &ProvisioningEvidenceInputV1,
    now: DateTime<Utc>,
    minimum: Option<DateTime<Utc>>,
    timestamp: impl Fn(&T) -> DateTime<Utc>,
) -> Check<&'a T> {
    let r = r
        .as_ref()
        .ok_or_else(|| unknown(ProvisioningReasonV1::ObservationMissing))?;
    clock(r.observed_at, r.observed_at, c, e, now, minimum)?;
    let t = r
        .result
        .as_ref()
        .map_err(|_| unknown(ProvisioningReasonV1::ObservationUnavailable))?;
    clock(timestamp(t), r.observed_at, c, e, now, minimum)?;
    Ok(t)
}
pub(super) fn infrastructure(
    c: &ProvisioningEvaluationContextInputV1,
    e: &ProvisioningEvidenceInputV1,
    now: DateTime<Utc>,
) -> Check<()> {
    let n = read(&e.node, c, e, now, None, NodeStatus::observed_at)?;
    let s = read(&e.storage, c, e, now, None, StorageStatus::observed_at)?;
    let b = read(&e.bridges, c, e, now, None, BridgeInventory::observed_at)?;
    if !n.online() || !b.has_active(c.plan.expected().vm().bridge()) {
        return Err(blocked(ProvisioningReasonV1::UnsupportedInfrastructure));
    }
    if s.available_bytes() < c.plan.expected().vm().minimum_storage_bytes() {
        return Err(blocked(ProvisioningReasonV1::InsufficientCapacity));
    }
    Ok(())
}
pub(super) fn media(
    c: &ProvisioningEvaluationContextInputV1,
    e: &ProvisioningEvidenceInputV1,
    now: DateTime<Utc>,
) -> Check<()> {
    for iso in [
        c.plan.expected().deployment_iso_volid(),
        c.plan.expected().driver_iso_volid(),
    ] {
        let storage = expectations::media_storage(iso).expect("validated ISO");
        let read = e
            .media
            .iter()
            .find(|r| r.result.as_ref().is_ok_and(|m| m.storage() == &storage))
            .ok_or_else(|| unknown(ProvisioningReasonV1::MediaCoverageIncomplete))?;
        let m = read.result.as_ref().expect("selected success");
        clock(m.observed_at(), read.observed_at, c, e, now, None)?;
        if m.coverage() != ProvisioningCoverageV1::Complete {
            return Err(unknown(ProvisioningReasonV1::MediaCoverageIncomplete));
        }
        if !m.iso_volids().iter().any(|v| v == iso) {
            return Err(blocked(ProvisioningReasonV1::MediaMissing));
        }
    }
    Ok(())
}
pub(super) fn identities(
    c: &ProvisioningEvaluationContextInputV1,
    e: &ProvisioningEvidenceInputV1,
    now: DateTime<Utc>,
    minimum: Option<DateTime<Utc>>,
    target_present: bool,
) -> Check<()> {
    use ProvisioningReasonV1::*;
    let inventory = read(
        &e.inventory,
        c,
        e,
        now,
        minimum,
        ClusterVmInventory::observed_at,
    )?;
    let vm = c.plan.expected().vm();
    if !target_present && inventory.find(vm.target_vmid()).is_some() {
        return Err(conflict(TargetOccupied));
    }
    if !target_present && e.target_power.as_ref().is_some_and(|r| r.result.is_ok()) {
        return Err(conflict(InventoryContradiction));
    }
    if e.inventory_coverage != ProvisioningCoverageV1::Complete
        || inventory.vms().len() != e.identities.len()
    {
        return Err(unknown(IncompleteIdentityCoverage));
    }
    let mut ids = BTreeSet::new();
    let mut uuids = BTreeSet::new();
    let mut macs = BTreeSet::new();
    let mut disks = BTreeSet::new();
    for i in &e.identities {
        if !ids.insert(i.vmid()) {
            return Err(conflict(InventoryContradiction));
        }
        let entry = inventory
            .find(i.vmid())
            .ok_or_else(|| conflict(InventoryContradiction))?;
        if entry.node() != i.node() {
            return Err(conflict(InventoryContradiction));
        }
        clock(
            i.read().observed_at,
            i.read().observed_at,
            c,
            e,
            now,
            minimum,
        )?;
        let s = i
            .read()
            .result
            .as_ref()
            .map_err(|_| unknown(IncompleteIdentityCoverage))?;
        clock(s.observed_at(), i.read().observed_at, c, e, now, minimum)?;
        if s.coverage() != ProvisioningCoverageV1::Complete {
            return Err(unknown(IncompleteIdentityCoverage));
        }
        if s.name() != entry.name() || s.is_template() != entry.is_template() {
            return Err(conflict(InventoryContradiction));
        }
        if !uuids.insert(s.uuid())
            || !macs.insert(s.mac())
            || !disks.insert((s.node(), s.primary_storage(), s.primary_volume()))
        {
            return Err(conflict(IdentityCollision));
        }
        if (s.uuid() == vm.uuid() || s.mac() == vm.mac())
            && (!target_present || s.vmid() != vm.target_vmid() || s.node() != vm.node())
        {
            return Err(conflict(IdentityCollision));
        }
        for (config, power, id) in [
            (&e.source_config, &e.source_power, vm.source_vmid()),
            (&e.target_config, &e.target_power, vm.target_vmid()),
        ] {
            if s.vmid() != id {
                continue;
            }
            if let Some(Ok(config)) = config.as_ref().map(|r| &r.result) {
                let wrapper = if id == vm.source_vmid() {
                    e.source_config.as_ref()
                } else {
                    e.target_config.as_ref()
                }
                .expect("available read")
                .observed_at;
                clock(config.observed_at(), wrapper, c, e, now, minimum)?;
                let projection = ProvisioningIdentitySnapshotV1::from_provisioning(config);
                if projection.node() != s.node()
                    || projection.vmid() != s.vmid()
                    || projection.name() != s.name()
                    || projection.is_template() != s.is_template()
                    || projection.uuid() != s.uuid()
                    || projection.mac() != s.mac()
                    || projection.primary_storage() != s.primary_storage()
                    || projection.primary_volume() != s.primary_volume()
                    || projection.config_digest() != s.config_digest()
                    || projection.coverage() != s.coverage()
                {
                    return Err(conflict(InventoryContradiction));
                }
            }
            if let Some(Ok(power)) = power.as_ref().map(|r| &r.result) {
                let wrapper = if id == vm.source_vmid() {
                    e.source_power.as_ref()
                } else {
                    e.target_power.as_ref()
                }
                .expect("available read")
                .observed_at;
                clock(power.observed_at(), wrapper, c, e, now, minimum)?;
                if power.power() != entry.power() || power.node() != entry.node() {
                    return Err(conflict(InventoryContradiction));
                }
            }
        }
    }
    if target_present && inventory.find(vm.target_vmid()).is_none() {
        return Err(conflict(InventoryContradiction));
    }
    if inventory.find(vm.source_vmid()).is_none() {
        return Err(conflict(InventoryContradiction));
    }
    Ok(())
}
