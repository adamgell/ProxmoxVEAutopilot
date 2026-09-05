//! Current-world validation and lossless, validated mutation candidates.
use super::*;

pub(super) fn validate_world(
    s: &State,
    r: &ProvisioningMutationRequestV1,
    exclude: Option<&Upid>,
) -> Result<(), PveWriteError> {
    use ProvisioningActionV1::*;
    let e = r.plan().expected();
    let p = e.vm();
    let action = r.plan().action();
    let pins = resources(p, action == Clone);
    if s.pending.iter().any(|pending| {
        Some(pending.upid()) != exclude && pending.resources().iter().any(|id| pins.contains(id))
    }) {
        return Err(PveWriteError::Conflict);
    }
    let b = r.expected_before();
    let current =
        lookup(s, b.config().node(), b.config().vmid()).map_err(|_| PveWriteError::Conflict)?;
    let config = current
        .config
        .provisioning()
        .map_err(|_| PveWriteError::Conflict)?;
    let mut observed = serde_json::to_value(config).map_err(|_| PveWriteError::Rejected)?;
    observed["observed_at"] = json!(b.config().observed_at());
    if serde_json::from_value::<ProvisioningVmConfigV1>(observed)
        .map_err(|_| PveWriteError::Rejected)?
        != *b.config()
        || current.power != b.power().power()
        || b.power().locked() != Some(config.locked())
        || config.source() != NativeEvidenceSource::FakePve
        || config.locked()
        || !config.unsupported().is_empty()
    {
        return Err(PveWriteError::Conflict);
    }
    if !s.nodes.get(p.node()).is_some_and(NodeStatus::online)
        || !s
            .bridges
            .get(p.node())
            .is_some_and(|b| b.has_active(p.bridge()))
    {
        return Err(PveWriteError::Rejected);
    }
    let free = s
        .storage
        .get(&(p.node().clone(), p.storage().clone()))
        .ok_or(PveWriteError::Rejected)?
        .available_bytes();
    if free < p.minimum_storage_bytes() {
        return Err(PveWriteError::Rejected);
    }
    for other in s.vms.values() {
        let identity = other
            .config
            .identity()
            .map_err(|_| PveWriteError::Conflict)?;
        if identity.coverage() != ProvisioningCoverageV1::Complete {
            return Err(PveWriteError::Conflict);
        }
        if action == Clone || other.config.vmid() != p.target_vmid() {
            if identity.uuid() == p.uuid() || identity.mac() == p.mac() {
                return Err(PveWriteError::Conflict);
            }
            if action != Clone
                && identity.primary_storage() == config.primary_disk().storage()
                && identity.primary_volume() == config.primary_disk().volume()
            {
                return Err(PveWriteError::Conflict);
            }
        }
    }
    if matches!(action, Clone | ConfigurePe | StartPe) {
        for volid in [e.deployment_iso_volid(), e.driver_iso_volid()] {
            let (storage, _) = volid.split_once(':').ok_or(PveWriteError::Rejected)?;
            let storage = StorageName::parse(storage).map_err(|_| PveWriteError::Rejected)?;
            let catalog = s
                .provisioning
                .media
                .get(&(p.node().clone(), storage))
                .ok_or(PveWriteError::Rejected)?;
            if catalog.coverage() != ProvisioningCoverageV1::Complete
                || !catalog.iso_volids().iter().any(|v| v == volid)
            {
                return Err(PveWriteError::Rejected);
            }
        }
    }
    match action {
        Clone => {
            if s.vms.contains_key(&p.target_vmid()) {
                return Err(PveWriteError::Conflict);
            }
            if config.node() != p.node()
                || config.vmid() != p.source_vmid()
                || !config.is_template()
                || current.power != PowerState::Stopped
                || config.fake_clone_provenance().is_some()
                || config.primary_disk().capacity_bytes() != e.template_capacity_bytes()
                || config
                    .template_fingerprint()
                    .map_err(|_| PveWriteError::Rejected)?
                    != e.template_config_sha256()
            {
                return Err(PveWriteError::Conflict);
            }
        }
        _ => {
            if config.node() != p.node() || config.vmid() != p.target_vmid() || config.is_template()
            {
                return Err(PveWriteError::Conflict);
            }
            if current.power
                != if action == EnsureStopped {
                    PowerState::Running
                } else {
                    PowerState::Stopped
                }
            {
                return Err(PveWriteError::Conflict);
            }
            if action == EnsureCapacity {
                let current = config.primary_disk().capacity_bytes();
                let delta = e
                    .effective_capacity_bytes()
                    .checked_sub(current)
                    .filter(|d| *d > 0)
                    .ok_or(PveWriteError::Conflict)?;
                if current != e.template_capacity_bytes() {
                    return Err(PveWriteError::Conflict);
                }
                if delta > free {
                    return Err(PveWriteError::Rejected);
                }
            }
        }
    }
    Ok(())
}

/// Reconstruct through the rich sanitized schema. Capacity never comes from
/// ConfigurePe's size-less external disk option, and no old wire path is used.
pub(super) fn candidate(
    s: &State,
    r: &ProvisioningMutationRequestV1,
) -> Result<FakeVm, PveWriteError> {
    use ProvisioningActionV1::*;
    let e = r.plan().expected();
    let p = e.vm();
    let action = r.plan().action();
    let original = lookup(
        s,
        p.node(),
        if action == Clone {
            p.source_vmid()
        } else {
            p.target_vmid()
        },
    )
    .map_err(|_| PveWriteError::Conflict)?;
    let mut result = original.clone();
    if matches!(action, StartPe | StartDisk | EnsureStopped) {
        result.power = if action == EnsureStopped {
            PowerState::Stopped
        } else {
            PowerState::Running
        };
        return Ok(result);
    }
    let mut v = serde_json::to_value(
        original
            .config
            .provisioning()
            .map_err(|_| PveWriteError::Conflict)?,
    )
    .map_err(|_| PveWriteError::Rejected)?;
    v["digest"] = json!(format!("provisioning-{}", uuid::Uuid::now_v7()));
    v["observed_at"] = json!(Utc::now());
    match r {
        ProvisioningMutationRequestV1::Clone(clone) => {
            let mut identity = None;
            for _ in 0..64 {
                let uuid = uuid::Uuid::now_v7();
                let b = uuid.as_bytes();
                let mac = format!(
                    "02:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}",
                    b[11], b[12], b[13], b[14], b[15]
                );
                let parsed_uuid =
                    VmUuid::parse(&uuid.to_string()).map_err(|_| PveWriteError::Rejected)?;
                let parsed_mac = MacAddress::parse(&mac).map_err(|_| PveWriteError::Rejected)?;
                let collision = s.vms.values().any(|vm| {
                    vm.config
                        .identity()
                        .map_or(true, |i| i.uuid() == parsed_uuid || i.mac() == &parsed_mac)
                });
                if !collision && parsed_uuid != p.uuid() && &parsed_mac != p.mac() {
                    identity = Some((uuid, mac));
                    break;
                }
            }
            let (uuid, mac) = identity.ok_or(PveWriteError::Conflict)?;
            let volume = (0..=s.vms.len())
                .map(|n| format!("vm-{}-disk-{n}", p.target_vmid()))
                .find(|volume| {
                    !s.vms.values().any(|vm| {
                        vm.config.storage() == p.storage() && vm.config.volume() == volume
                    })
                })
                .ok_or(PveWriteError::Conflict)?;
            v["vmid"] = json!(p.target_vmid());
            v["name"] = json!(p.name());
            v["template"] = json!(false);
            v["primary_disk"]["storage"] = json!(p.storage());
            v["primary_disk"]["volume"] = json!(volume);
            v["uuid"] = json!(uuid);
            v["mac"] = json!(mac);
            v["fake_clone_provenance"] =
                json!(FakeCloneProvenance::from_request(clone.clone_request()));
            result.incarnation = s
                .incarnation
                .checked_add(1)
                .ok_or(PveWriteError::Rejected)?;
            result.power = PowerState::Stopped;
            result.qga_reachable = false;
        }
        ProvisioningMutationRequestV1::GrowDisk(_) => {
            v["primary_disk"]["capacity_bytes"] = json!(e.effective_capacity_bytes())
        }
        ProvisioningMutationRequestV1::Configure(_) if action == ConfigurePe => {
            v["cores"] = json!(p.cores());
            v["memory_mib"] = json!(p.memory_mib());
            v["cpu"] = json!("host");
            v["balloon_mib"] = json!(0);
            v["firmware"] = json!("seabios");
            v["qga_enabled"] = json!(true);
            v["qga_channel"] = json!("virtio");
            v["uuid"] = json!(p.uuid());
            v["system_serial"] = json!(e.system_serial());
            v["mac"] = json!(p.mac());
            v["bridge"] = json!(p.bridge());
            v["primary_disk"]["serial"] = json!(e.disk_serial());
            v["deployment_iso"] = json!({"state":"iso","volid":e.deployment_iso_volid()});
            v["driver_iso"] = json!({"state":"iso","volid":e.driver_iso_volid()});
            v["boot_profile"] = json!("pe_media");
        }
        ProvisioningMutationRequestV1::Configure(_) => {
            v["deployment_iso"] = json!({"state":"absent"});
            v["driver_iso"] = json!({"state":"absent"});
            v["boot_profile"] = json!("installed_disk");
        }
        _ => return Err(PveWriteError::Rejected),
    }
    result.config =
        FakeConfig::ProvisioningV1(serde_json::from_value(v).map_err(|_| PveWriteError::Rejected)?);
    Ok(result)
}
pub(super) fn apply_candidate(s: &mut State, vm: FakeVm) {
    s.incarnation = s.incarnation.max(vm.incarnation);
    s.vms.insert(vm.config.vmid(), vm);
}
