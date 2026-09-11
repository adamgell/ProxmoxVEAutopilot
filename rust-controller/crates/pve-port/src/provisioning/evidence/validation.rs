use super::*;
pub(super) fn validate(i: &ProvisioningEvidenceInputV1) -> Result<(), InvalidProvisioning> {
    let vm = i.plan.expected().vm();
    if !i.binding.validates(&i.plan) || i.media.len() > 2 {
        return Err(InvalidProvisioning);
    }
    if i.node
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .is_some_and(|c| c.node() != vm.node())
        || i.storage
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| c.node() != vm.node() || c.storage() != vm.storage())
        || i.bridges
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| c.node() != vm.node())
    {
        return Err(InvalidProvisioning);
    }
    for (read, id) in [
        (&i.source_config, vm.source_vmid()),
        (&i.target_config, vm.target_vmid()),
    ] {
        if read
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| c.node() != vm.node() || c.vmid() != id || c.source() != i.source)
        {
            return Err(InvalidProvisioning);
        }
    }
    for (read, id) in [
        (&i.source_power, vm.source_vmid()),
        (&i.target_power, vm.target_vmid()),
    ] {
        if read
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| c.node() != vm.node() || c.vmid() != id)
        {
            return Err(InvalidProvisioning);
        }
    }
    if i.qga
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .is_some_and(|c| c.node() != vm.node() || c.vmid() != vm.target_vmid())
    {
        return Err(InvalidProvisioning);
    }
    let mut keys = BTreeSet::new();
    for identity in &i.identities {
        if !keys.insert((identity.node(), identity.vmid()))
            || identity
                .read()
                .result
                .as_ref()
                .is_ok_and(|c| c.source() != i.source)
        {
            return Err(InvalidProvisioning);
        }
    }
    let media_storages: BTreeSet<_> = [
        i.plan.expected().deployment_iso_volid(),
        i.plan.expected().driver_iso_volid(),
    ]
    .into_iter()
    .filter_map(expectations::media_storage)
    .collect();
    let mut keys = BTreeSet::new();
    for m in &i.media {
        if let Ok(m) = &m.result
            && (m.node() != vm.node()
                || !media_storages.contains(m.storage())
                || !keys.insert(m.storage()))
        {
            return Err(InvalidProvisioning);
        }
    }
    if let Some(r) = &i.receipt {
        if !r
            .dispatch()
            .request()
            .binding()
            .same_operation_attempt(&i.binding)
            || r.dispatch().request().plan() != &i.plan
            || r.dispatch().source() != i.source
        {
            return Err(InvalidProvisioning);
        }
        if i.task
            .as_ref()
            .and_then(|t| t.result.as_ref().ok())
            .is_some_and(|t| r.receipt() != &MutationReceipt::Task(t.upid().clone()))
        {
            return Err(InvalidProvisioning);
        }
    }
    if i.task
        .as_ref()
        .and_then(|t| t.result.as_ref().ok())
        .is_some_and(|t| {
            !provisioning_receipt_matches(&i.plan, &MutationReceipt::Task(t.upid().clone()))
        })
    {
        return Err(InvalidProvisioning);
    }
    Ok(())
}
