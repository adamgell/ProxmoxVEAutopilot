//! Typed controls and admission into the existing locked synthetic world.
use super::*;
use chrono::DateTime;
use controller_domain::OperationId;
use std::collections::BTreeSet;
mod controls;
mod world;
pub use controls::*;

#[derive(Default)]
pub(super) struct Controls {
    config_reads: BTreeMap<(NodeName, Vmid), VecDeque<FakeProvisioningConfigReadV1>>,
    identity_reads: BTreeMap<(NodeName, Vmid), VecDeque<FakeProvisioningIdentityReadV1>>,
    media_reads: BTreeMap<(NodeName, StorageName), VecDeque<FakeProvisioningMediaReadV1>>,
    media: BTreeMap<(NodeName, StorageName), ProvisioningMediaInventoryV1>,
    outcomes: BTreeMap<SelectorKey, VecDeque<FakeMutationOutcome>>,
    pauses: BTreeMap<SelectorKey, VecDeque<Arc<FakePause>>>,
    records: Vec<ProvisioningSubmissionRecordV1>,
    attempted: BTreeSet<uuid::Uuid>,
}
type SelectorKey = (ProvisioningActionV1, Option<uuid::Uuid>);
fn selected<T>(
    queues: &mut BTreeMap<SelectorKey, VecDeque<T>>,
    request: &ProvisioningMutationRequestV1,
) -> Option<T> {
    [Some(request.operation_id().as_uuid()), None]
        .into_iter()
        .find_map(|op| {
            queues
                .get_mut(&(request.plan().action(), op))
                .and_then(VecDeque::pop_front)
        })
}
pub(super) fn resources(vm: &NativeVmPlan, clone: bool) -> Vec<Vmid> {
    if clone {
        vec![vm.source_vmid(), vm.target_vmid()]
    } else {
        vec![vm.target_vmid()]
    }
}
#[async_trait]
impl ProvisioningFakePort for NativeFakePve {
    async fn provisioning_vm_config(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<ProvisioningVmConfigV1, PveReadError> {
        let scripted = self
            .state
            .lock()
            .unwrap()
            .provisioning
            .config_reads
            .get_mut(&(node.clone(), vmid))
            .and_then(VecDeque::pop_front);
        match scripted {
            Some(FakeProvisioningConfigReadV1::Error(e)) => return Err(e),
            Some(FakeProvisioningConfigReadV1::Snapshot(c)) => {
                return if c.node() == node
                    && c.vmid() == vmid
                    && c.source() == NativeEvidenceSource::FakePve
                {
                    Ok(*c)
                } else {
                    Err(PveReadError::InvalidResponse)
                };
            }
            Some(FakeProvisioningConfigReadV1::Pause(p)) => p.wait().await,
            None => {}
        }
        refresh_observation(
            lookup(&self.state.lock().unwrap(), node, vmid)?
                .config
                .provisioning()?,
        )
    }
    async fn provisioning_identity(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<ProvisioningIdentitySnapshotV1, PveReadError> {
        let scripted = self
            .state
            .lock()
            .unwrap()
            .provisioning
            .identity_reads
            .get_mut(&(node.clone(), vmid))
            .and_then(VecDeque::pop_front);
        match scripted {
            Some(FakeProvisioningIdentityReadV1::Error(e)) => return Err(e),
            Some(FakeProvisioningIdentityReadV1::Snapshot(c)) => {
                return if c.node() == node
                    && c.vmid() == vmid
                    && c.source() == NativeEvidenceSource::FakePve
                {
                    Ok(*c)
                } else {
                    Err(PveReadError::InvalidResponse)
                };
            }
            Some(FakeProvisioningIdentityReadV1::Pause(p)) => p.wait().await,
            None => {}
        }
        refresh_observation(
            &lookup(&self.state.lock().unwrap(), node, vmid)?
                .config
                .identity()?,
        )
    }
    async fn provisioning_media(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<ProvisioningMediaInventoryV1, PveReadError> {
        let scripted = self
            .state
            .lock()
            .unwrap()
            .provisioning
            .media_reads
            .get_mut(&(node.clone(), storage.clone()))
            .and_then(VecDeque::pop_front);
        match scripted {
            Some(FakeProvisioningMediaReadV1::Error(e)) => return Err(e),
            Some(FakeProvisioningMediaReadV1::Snapshot(c)) => {
                return if c.node() == node && c.storage() == storage {
                    Ok(*c)
                } else {
                    Err(PveReadError::InvalidResponse)
                };
            }
            Some(FakeProvisioningMediaReadV1::Pause(p)) => p.wait().await,
            None => {}
        }
        refresh_observation(
            self.state
                .lock()
                .unwrap()
                .provisioning
                .media
                .get(&(node.clone(), storage.clone()))
                .ok_or(PveReadError::NotFound)?,
        )
    }
    async fn submit_provisioning(
        &self,
        request: &ProvisioningMutationRequestV1,
    ) -> Result<MutationReceipt, PveWriteError> {
        let gate = selected(&mut self.state.lock().unwrap().provisioning.pauses, request);
        if let Some(p) = gate {
            p.wait().await;
        }
        let mut s = self.state.lock().unwrap();
        let submitted_at = Utc::now();
        let number = u64::try_from(s.provisioning.records.len())
            .ok()
            .and_then(|n| n.checked_add(1))
            .ok_or(PveWriteError::Rejected)?;
        let mut acceptance = None;
        let returned = if !s
            .provisioning
            .attempted
            .insert(request.operation_id().as_uuid())
        {
            Err(PveWriteError::Conflict)
        } else {
            admit(&mut s, request, &mut acceptance)
        };
        s.provisioning.records.push(ProvisioningSubmissionRecordV1 {
            submission_number: number,
            request: request.clone(),
            request_sha256: request
                .request_digest()
                .map_err(|_| PveWriteError::Rejected)?,
            submitted_at,
            acceptance,
            returned: returned.clone(),
        });
        returned
    }
}
fn admit(
    s: &mut State,
    r: &ProvisioningMutationRequestV1,
    acceptance: &mut Option<ProvisioningAcceptanceV1>,
) -> Result<MutationReceipt, PveWriteError> {
    world::validate_world(s, r, None)?;
    let candidate = world::candidate(s, r)?;
    let outcome =
        selected(&mut s.provisioning.outcomes, r).unwrap_or(FakeMutationOutcome::Accepted);
    if let FakeMutationOutcome::Rejected(e) = outcome {
        return Err(e);
    }
    let receipt = if let Some((worker, id)) = task_identity(r) {
        let sequence = s.sequence.checked_add(1).ok_or(PveWriteError::Rejected)?;
        let upid = Upid::parse(format!(
            "UPID:{}:{sequence:08X}:00000001:00000001:{worker}:{id}:fake@pve:",
            r.plan().expected().vm().node()
        ))
        .map_err(|_| PveWriteError::Rejected)?;
        s.sequence = sequence;
        MutationReceipt::Task(upid)
    } else {
        MutationReceipt::SynchronousAccepted
    };
    if !crate::provisioning::provisioning_receipt_matches(r.plan(), &receipt) {
        return Err(PveWriteError::Rejected);
    }
    match (&receipt, outcome) {
        (MutationReceipt::Task(upid), FakeMutationOutcome::AcceptedTaskDelayed) => {
            let vm = r.plan().expected().vm();
            let source_incarnation = if r.plan().action() == ProvisioningActionV1::Clone {
                s.vms.get(&vm.source_vmid()).map(|v| v.incarnation)
            } else {
                None
            };
            let target_incarnation = s.vms.get(&vm.target_vmid()).map(|v| v.incarnation);
            s.pending.push_back(PendingMutation::ProvisioningV1 {
                upid: upid.clone(),
                request: Box::new(r.clone()),
                source_incarnation,
                target_incarnation,
            });
            s.tasks.insert(upid.clone(), TaskState::Running);
        }
        (MutationReceipt::Task(upid), FakeMutationOutcome::AcceptedTaskFails) => {
            s.tasks.insert(upid.clone(), TaskState::CompleteFailure);
        }
        _ => {
            world::apply_candidate(s, candidate);
            if let MutationReceipt::Task(upid) = &receipt {
                s.tasks.insert(upid.clone(), TaskState::CompleteSuccess);
            }
        }
    }
    *acceptance = Some(ProvisioningAcceptanceV1 {
        accepted_at: Utc::now(),
        receipt: receipt.clone(),
    });
    if outcome == FakeMutationOutcome::AppliedResponseLost {
        Err(PveWriteError::OutcomeUnknown)
    } else {
        Ok(receipt)
    }
}
fn task_identity(r: &ProvisioningMutationRequestV1) -> Option<(&'static str, Vmid)> {
    use ProvisioningActionV1::*;
    let p = r.plan().expected().vm();
    match r.plan().action() {
        Clone => Some(("qmclone", p.source_vmid())),
        EnsureCapacity => Some(("resize", p.target_vmid())),
        StartPe | StartDisk => Some(("qmstart", p.target_vmid())),
        EnsureStopped => Some(("qmstop", p.target_vmid())),
        ConfigurePe | ConfigureDisk => None,
    }
}
impl NativeFakePve {
    pub fn complete_provisioning_task(&self, upid: &Upid) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        let index = s
            .pending
            .iter()
            .position(|p| matches!(p, PendingMutation::ProvisioningV1 { .. }) && p.upid() == upid)
            .ok_or(PveWriteError::Rejected)?;
        let Some(PendingMutation::ProvisioningV1 {
            request,
            source_incarnation,
            target_incarnation,
            ..
        }) = s.pending.remove(index)
        else {
            return Err(PveWriteError::Rejected);
        };
        let p = request.plan().expected().vm();
        let same = (request.plan().action() != ProvisioningActionV1::Clone
            || s.vms.get(&p.source_vmid()).map(|v| v.incarnation) == source_incarnation)
            && s.vms.get(&p.target_vmid()).map(|v| v.incarnation) == target_incarnation;
        let result = if same {
            world::validate_world(&s, &request, Some(upid))
                .and_then(|()| world::candidate(&s, &request))
                .map(|candidate| world::apply_candidate(&mut s, candidate))
        } else {
            Err(PveWriteError::Conflict)
        };
        s.tasks.insert(
            upid.clone(),
            if result.is_ok() {
                TaskState::CompleteSuccess
            } else {
                TaskState::CompleteFailure
            },
        );
        result
    }
}
