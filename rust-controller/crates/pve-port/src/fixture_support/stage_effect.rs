//! Stage transitions require durable acceptance of the exact predecessor request.
use super::{
    FixtureStageIdentity, VmState, durable_fixture_log::FixtureLog, stage_identity::invalid,
};
use crate::{
    MutationReceipt, ProvisioningMutationRequestV1, Upid, fixture_ipc::FixtureStageRequest,
};
use std::io;

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
    let (kind, worker) = match request.request() {
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
            ("qmclone", vm.source_vmid())
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
            ("resize", vm.target_vmid())
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
    let upid = Upid::parse(format!(
        "UPID:{}:{:08X}:00000001:00000001:{}:{}:fake@pve:",
        vm.node(),
        sequence,
        kind,
        worker
    ))
    .map_err(|_| invalid())?;
    let receipt = request
        .encode_receipt(sequence, MutationReceipt::Task(upid))
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
