//! Stage transitions require durable acceptance of the exact predecessor request.
use super::{
    FixtureStageIdentity, VmState, durable_fixture_log::FixtureLog, stage_identity::invalid,
};
use crate::{
    MutationReceipt, ProvisioningMutationRequestV1, Upid, fixture_ipc::FixtureStageRequest,
};
use std::io;

#[cfg(test)]
#[path = "../../tests/provisioning_support/mod.rs"]
mod test_support;

/// A legacy acceptance remains legacy: no owner/generation is inferred for it.
/// Only the new resize requires and consumes a v2 supervisor capability.
pub(crate) fn submit_after_legacy_clone(
    log: &mut FixtureLog,
    identity: &FixtureStageIdentity,
    request: &FixtureStageRequest,
    predecessor: &crate::fixture_ipc::FixtureCloneRequest,
    original_receipt: &[u8],
    after: VmState,
    consume: impl FnOnce() -> io::Result<()>,
) -> io::Result<Vec<u8>> {
    identity.validate_request(request)?;
    let ProvisioningMutationRequestV1::GrowDisk(grow) = request.request() else {
        return Err(invalid());
    };
    let prior = predecessor.request();
    let vm = request.request().plan().expected().vm();
    if predecessor.fixture_id() != request.fixture_id()
        || !prior
            .binding()
            .same_operation_attempt(grow.predecessor_binding())
        || !prior.binding().same_operation_attempt(grow.clone_binding())
        || prior.plan() != grow.predecessor_plan()
        || prior.plan().expected() != request.request().plan().expected()
    {
        return Err(invalid());
    }
    predecessor
        .decode_receipt(original_receipt)
        .map_err(|_| invalid())?;
    let effect = log
        .accepted_effect(
            prior.binding().operation_id().as_uuid(),
            &predecessor.request_sha256(),
        )?
        .ok_or_else(invalid)?;
    let before = VmState {
        disk_bytes: grow
            .expected_before()
            .config()
            .primary_disk()
            .capacity_bytes(),
        pe_configured: false,
    };
    if effect.receipt() != Some(original_receipt)
        || !effect.has_after(vm.target_vmid().get(), &before)
        || log.world().get(&vm.target_vmid().get()) != Some(&before)
        || after.pe_configured
        || after.disk_bytes <= before.disk_bytes
        || after.disk_bytes
            != request
                .request()
                .plan()
                .expected()
                .effective_capacity_bytes()
    {
        return Err(invalid());
    }
    consume()?;
    if log.record_stage_attempt(
        identity.operation,
        &identity.request_sha256,
        identity.ledger_binding(),
    )? {
        return Err(invalid());
    }
    let sequence = log.records().len() as u64;
    let upid = Upid::parse(format!(
        "UPID:{}:{:08X}:00000001:00000001:resize:{}:fake@pve:",
        vm.node(),
        sequence,
        vm.target_vmid()
    ))
    .map_err(|_| invalid())?;
    let receipt = request
        .encode_receipt(sequence, MutationReceipt::Task(upid))
        .map_err(|_| invalid())?;
    log.record_effect_receipt(
        sequence,
        vm.target_vmid().get(),
        Some(before),
        after,
        Some(receipt.clone()),
    )?;
    Ok(receipt)
}

pub(crate) fn submit(
    log: &mut FixtureLog,
    identity: &FixtureStageIdentity,
    request: &FixtureStageRequest,
    predecessor: Option<(&FixtureStageIdentity, &FixtureStageRequest)>,
    after: VmState,
    consume: impl FnOnce() -> io::Result<()>,
) -> io::Result<Vec<u8>> {
    identity.validate_request(request)?;
    let vm = request.request().plan().expected().vm();
    let before = log.world().get(&vm.target_vmid().get()).cloned();
    let task = match request.request() {
        ProvisioningMutationRequestV1::Clone(_) => {
            if predecessor.is_some()
                || before.is_some()
                || after.pe_configured
                || after.disk_bytes
                    != request
                        .request()
                        .plan()
                        .expected()
                        .template_capacity_bytes()
            {
                return Err(invalid());
            }
            Some(("qmclone", vm.source_vmid()))
        }
        ProvisioningMutationRequestV1::GrowDisk(grow) => {
            let (prior_identity, prior) = predecessor.ok_or_else(invalid)?;
            prior_identity.validate_request(prior)?;
            if prior.fixture_id() != request.fixture_id()
                || !matches!(prior.request(), ProvisioningMutationRequestV1::Clone(_))
                || !prior
                    .request()
                    .binding()
                    .same_operation_attempt(grow.predecessor_binding())
                || prior.request().plan() != grow.predecessor_plan()
                || !prior
                    .request()
                    .binding()
                    .same_operation_attempt(grow.clone_binding())
                || prior.request().plan().expected().vm() != vm
            {
                return Err(invalid());
            }
            let effect = log
                .accepted_stage_effect(
                    prior_identity.operation,
                    &prior_identity.request_sha256,
                    prior_identity.ledger_binding(),
                )?
                .ok_or_else(invalid)?;
            prior
                .decode_receipt(effect.receipt().ok_or_else(invalid)?)
                .map_err(|_| invalid())?;
            let expected = VmState {
                disk_bytes: grow
                    .expected_before()
                    .config()
                    .primary_disk()
                    .capacity_bytes(),
                pe_configured: false,
            };
            if before.as_ref() != Some(&expected)
                || !effect.has_after(vm.target_vmid().get(), &expected)
                || after.pe_configured
                || after.disk_bytes <= expected.disk_bytes
                || after.disk_bytes
                    != request
                        .request()
                        .plan()
                        .expected()
                        .effective_capacity_bytes()
            {
                return Err(invalid());
            }
            Some(("resize", vm.target_vmid()))
        }
        ProvisioningMutationRequestV1::Configure(configure)
            if request.request().plan().action() == crate::ProvisioningActionV1::ConfigurePe =>
        {
            let (prior_identity, prior) = predecessor.ok_or_else(invalid)?;
            prior_identity.validate_request(prior)?;
            let ProvisioningMutationRequestV1::GrowDisk(grow) = prior.request() else {
                return Err(invalid());
            };
            if prior.fixture_id() != request.fixture_id()
                || !prior
                    .request()
                    .binding()
                    .same_operation_attempt(configure.predecessor_binding())
                || prior.request().plan() != configure.predecessor_plan()
                || !grow
                    .clone_binding()
                    .same_operation_attempt(configure.clone_binding())
                || prior.request().plan().expected() != request.request().plan().expected()
            {
                return Err(invalid());
            }
            // Accepted resize is possible only after an exact accepted Clone.
            // Its persisted request digest binds that predecessor transitively.
            let effect = log
                .accepted_stage_effect(
                    prior_identity.operation,
                    &prior_identity.request_sha256,
                    prior_identity.ledger_binding(),
                )?
                .ok_or_else(invalid)?;
            prior
                .decode_receipt(effect.receipt().ok_or_else(invalid)?)
                .map_err(|_| invalid())?;
            let expected = VmState {
                disk_bytes: request
                    .request()
                    .plan()
                    .expected()
                    .effective_capacity_bytes(),
                pe_configured: false,
            };
            if before.as_ref() != Some(&expected)
                || !effect.has_after(vm.target_vmid().get(), &expected)
                || after.disk_bytes != expected.disk_bytes
                || !after.pe_configured
                || configure
                    .expected_before()
                    .config()
                    .primary_disk()
                    .capacity_bytes()
                    != expected.disk_bytes
            {
                return Err(invalid());
            }
            None
        }
        ProvisioningMutationRequestV1::Start(_)
            if request.request().plan().action() == crate::ProvisioningActionV1::StartPe =>
        {
            let (prior_identity, prior) = predecessor.ok_or_else(invalid)?;
            prior_identity.validate_request(prior)?;
            let effect = log
                .accepted_stage_effect(
                    prior_identity.operation,
                    &prior_identity.request_sha256,
                    prior_identity.ledger_binding(),
                )?
                .ok_or_else(invalid)?;
            request
                .validate_start_pe_predecessor(prior, effect.receipt().ok_or_else(invalid)?)
                .map_err(|_| invalid())?;
            // Disk/PE flags cannot establish a durable running power transition.
            // Preserve authorization and the existing world until that contract exists.
            return Err(invalid());
        }
        _ => return Err(invalid()),
    };
    consume()?;
    if log.record_stage_attempt(
        identity.operation,
        &identity.request_sha256,
        identity.ledger_binding(),
    )? {
        return Err(invalid());
    }
    let sequence = log.records().len() as u64;
    let receipt = if let Some((kind, worker)) = task {
        let upid = Upid::parse(format!(
            "UPID:{}:{:08X}:00000001:00000001:{}:{}:fake@pve:",
            vm.node(),
            sequence,
            kind,
            worker
        ))
        .map_err(|_| invalid())?;
        MutationReceipt::Task(upid)
    } else {
        MutationReceipt::SynchronousAccepted
    };
    let receipt = request
        .encode_receipt(sequence, receipt)
        .map_err(|_| invalid())?;
    log.record_effect_receipt(
        sequence,
        vm.target_vmid().get(),
        before,
        after,
        Some(receipt.clone()),
    )?;
    Ok(receipt)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixture_support::{FixtureLedgerStage, StageBinding};
    use uuid::Uuid;

    #[test]
    fn legacy_clone_authorizes_only_exact_new_resize_without_reassignment() {
        let path = std::env::temp_dir().join(format!("legacy-resize-{}", Uuid::now_v7()));
        let episodes = test_support::chain();
        let fixture = Uuid::now_v7();
        let ProvisioningMutationRequestV1::Clone(clone) = &episodes[0].request else {
            panic!()
        };
        let prior = crate::fixture_ipc::FixtureCloneRequest::new(fixture, clone.clone()).unwrap();
        let request = FixtureStageRequest::new(fixture, episodes[1].request.clone()).unwrap();
        let identity = FixtureStageIdentity {
            operation: request.request().binding().operation_id().as_uuid(),
            stage: FixtureLedgerStage::DiskCapacity,
            attempt: request.request().binding().attempt_id().as_uuid(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
            request_sha256: request.request_sha256(),
        };
        let mut log = FixtureLog::create(&path).unwrap();
        let before = VmState {
            disk_bytes: 80 * test_support::GIB,
            pe_configured: false,
        };
        let after = VmState {
            disk_bytes: 120 * test_support::GIB,
            pe_configured: false,
        };
        let receipt = prior
            .encode_receipt(
                1,
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:")
                    .unwrap(),
            )
            .unwrap();
        assert!(
            submit_after_legacy_clone(
                &mut log,
                &identity,
                &request,
                &prior,
                &receipt,
                after.clone(),
                || panic!("unaccepted predecessor consumed authority")
            )
            .is_err()
        );
        log.record_attempt(
            clone.binding().operation_id().as_uuid(),
            &prior.request_sha256(),
        )
        .unwrap();
        log.record_effect_receipt(1, 101, None, before, Some(receipt.clone()))
            .unwrap();
        assert!(
            log.record_stage_attempt(
                clone.binding().operation_id().as_uuid(),
                &prior.request_sha256(),
                StageBinding {
                    stage: FixtureLedgerStage::Clone,
                    attempt: clone.binding().attempt_id().as_uuid(),
                    generation: identity.generation,
                    owner: identity.owner
                }
            )
            .is_err()
        );
        let bad_receipt = prior
            .encode_receipt(
                2,
                Upid::parse("UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:")
                    .unwrap(),
            )
            .unwrap();
        assert!(
            submit_after_legacy_clone(
                &mut log,
                &identity,
                &request,
                &prior,
                &bad_receipt,
                after.clone(),
                || panic!("changed receipt consumed authority")
            )
            .is_err()
        );
        let mut missing_owner = identity.clone();
        missing_owner.owner = Uuid::nil();
        assert!(
            submit_after_legacy_clone(
                &mut log,
                &missing_owner,
                &request,
                &prior,
                &receipt,
                after.clone(),
                || panic!("missing owner consumed authority")
            )
            .is_err()
        );
        let accepted = submit_after_legacy_clone(
            &mut log,
            &identity,
            &request,
            &prior,
            &receipt,
            after.clone(),
            || Ok(()),
        )
        .unwrap();
        request.decode_receipt(&accepted).unwrap();
        assert_eq!(log.effects().len(), 2);
        assert!(
            submit_after_legacy_clone(
                &mut log,
                &identity,
                &request,
                &prior,
                &receipt,
                after,
                || panic!("duplicate consumed authority")
            )
            .is_err()
        );
        assert_eq!(log.effects().len(), 2);
        drop(log);
        let recovered = FixtureLog::recover(&path).unwrap();
        assert_eq!(recovered.effects().len(), 2);
        assert_eq!(
            recovered
                .accepted_effect(
                    clone.binding().operation_id().as_uuid(),
                    &prior.request_sha256()
                )
                .unwrap()
                .unwrap()
                .receipt(),
            Some(receipt.as_slice())
        );
        assert_eq!(
            recovered
                .accepted_stage_effect(
                    identity.operation,
                    &identity.request_sha256,
                    identity.ledger_binding()
                )
                .unwrap()
                .unwrap()
                .receipt(),
            Some(accepted.as_slice())
        );
        drop(recovered);
        std::fs::remove_file(path).unwrap();
    }
}
