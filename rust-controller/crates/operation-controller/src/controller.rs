//! Bounded orchestration over a concrete in-memory fake. Dispatch authority is
//! recomputed by the store; no observation or advisory context is a capability.
use chrono::Utc;
use controller_domain::{EventId, ExecutionState, OperationId};
use postgres_store::{LeaseGrant, NativeOperationSnapshot, NativeStoreError, PgStore, Scheduler};
use pve_port::*;
use std::{collections::HashMap, future::Future, sync::Arc, time::Duration};

const READ_BOUND: Duration = Duration::from_secs(2);
const COLLECTION_BOUND: Duration = Duration::from_secs(6);
const CALL_BOUND: Duration = Duration::from_secs(24);
const MAX_IDENTITIES: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeProgress {
    Idle,
    Waiting,
    Decided(ExecutionState),
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum NativeControllerError {
    #[error("native_storage_unavailable")]
    Storage,
    #[error("native_validation_failed")]
    Validation,
    #[error("native_fence_lost")]
    FenceLost,
    #[error("native_call_timed_out")]
    TimedOut,
}
impl From<NativeStoreError> for NativeControllerError {
    fn from(e: NativeStoreError) -> Self {
        match e {
            NativeStoreError::StorageUnavailable => Self::Storage,
            NativeStoreError::Validation => Self::Validation,
            _ => Self::FenceLost,
        }
    }
}

pub struct NativeController {
    store: PgStore,
    scheduler: Scheduler,
    fake: Arc<NativeFakePve>,
    grants: HashMap<OperationId, LeaseGrant>,
}
impl NativeController {
    pub fn new(store: PgStore, scheduler: Scheduler, fake: Arc<NativeFakePve>) -> Self {
        Self {
            store,
            scheduler,
            fake,
            grants: HashMap::new(),
        }
    }
    pub async fn run_once(
        &mut self,
        operation_id: OperationId,
    ) -> Result<NativeProgress, NativeControllerError> {
        let result = tokio::time::timeout(CALL_BOUND, async {
            if self.scheduler.reap_expired().await.is_err() {
                self.grants.clear();
                return Err(NativeControllerError::FenceLost);
            }
            self.execute(operation_id).await
        })
        .await
        .unwrap_or(Err(NativeControllerError::TimedOut));
        if result.is_err() || matches!(result, Ok(NativeProgress::Decided(_))) {
            self.grants.remove(&operation_id);
        }
        result
    }
    async fn execute(&mut self, id: OperationId) -> Result<NativeProgress, NativeControllerError> {
        let snapshot = self.store.load_native_operation(id).await?;
        match snapshot.state() {
            ExecutionState::Unknown => return self.reconcile_once(id).await,
            ExecutionState::Satisfied
            | ExecutionState::Failed
            | ExecutionState::Blocked
            | ExecutionState::Conflicted => return Ok(NativeProgress::Decided(snapshot.state())),
            ExecutionState::Pending => {
                if snapshot.cancelled()
                    || snapshot
                        .predecessor_state()
                        .is_some_and(|s| s != ExecutionState::Satisfied)
                {
                    return Ok(NativeProgress::Idle);
                }
                let hash = digest(snapshot.plan())?;
                let Some(grant) = self.scheduler.claim_native_bound(id, &hash, 1).await? else {
                    return Ok(NativeProgress::Idle);
                };
                self.scheduler.start_native_bound(&grant, &hash).await?;
                self.grants.insert(id, grant);
            }
            _ => {
                if !self.grants.contains_key(&id) {
                    return Ok(NativeProgress::Waiting);
                }
                if snapshot.cancelled() {
                    return self.observe_and_decide(id, false).await;
                }
                self.heartbeat(id).await?;
                return self.observe_and_decide(id, false).await;
            }
        }
        let snapshot = self.store.load_native_operation(id).await?;
        let ownership = self.ownership(&snapshot).await?;
        let mut facts = self.collect(&snapshot, ownership.as_ref(), true).await?;
        let context = context(
            &snapshot,
            &facts,
            ownership.clone(),
            NativeEvaluationMode::Preflight,
        )?;
        let mut evaluation = evaluate_native_preflight(&context, &facts, Utc::now());
        // Retry observations only; each collection is bounded and heartbeats
        // before reads. A hard execution deadline is never extended here.
        for delay in [100, 250] {
            if !matches!(
                evaluation.reason,
                NativeReason::ObservationNotFresh | NativeReason::ObservationUnavailable
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(delay)).await;
            self.heartbeat(id).await?;
            facts = self.collect(&snapshot, ownership.as_ref(), true).await?;
            evaluation = evaluate_native_preflight(&context, &facts, Utc::now());
        }
        let event = self
            .store
            .record_native_evidence(
                id,
                snapshot
                    .attempt_id()
                    .ok_or(NativeControllerError::Validation)?,
                snapshot.revision(),
                &facts,
            )
            .await?;
        if evaluation.decision != NativeDecision::Ready {
            let grant = self
                .grants
                .get(&id)
                .ok_or(NativeControllerError::FenceLost)?;
            return self
                .scheduler
                .decide_native(grant, event, snapshot.revision() + 1)
                .await
                .map(progress)
                .map_err(Into::into);
        }
        let request = request(&snapshot, &facts, ownership.as_ref())?;
        let (hash, marker) = match &request {
            NativeMutationRequest::Clone(r) => (r.request_digest(), r.request_marker()),
            NativeMutationRequest::Configure(r) => (digest(r)?, id.as_uuid()),
            NativeMutationRequest::Start(r) => (digest(r)?, id.as_uuid()),
        };
        let grant = self
            .grants
            .get(&id)
            .ok_or(NativeControllerError::FenceLost)?;
        let permit = self
            .scheduler
            .begin_native_dispatch(grant, snapshot.revision() + 1, event, &hash, marker)
            .await?;
        let remaining = (*grant.deadline_at() - Utc::now())
            .to_std()
            .unwrap_or(Duration::ZERO);
        if tokio::time::timeout(
            READ_BOUND.min(remaining),
            self.fake
                .controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted),
        )
        .await
        .is_err()
        {
            return self.observe_and_decide(id, false).await;
        }
        // This final check covers authority, cancellation, lease and deadline.
        // It cannot atomically fence the subsequent external acceptance window.
        if self.scheduler.continuation(grant).await.is_err() {
            return self.observe_and_decide(id, false).await;
        }
        let remaining = (*grant.deadline_at() - Utc::now())
            .to_std()
            .unwrap_or(Duration::ZERO);
        let sent = tokio::time::timeout(READ_BOUND.min(remaining), async {
            match &request {
                NativeMutationRequest::Clone(r) => self.fake.clone_vm(r).await,
                NativeMutationRequest::Configure(r) => self.fake.configure_vm(r).await,
                NativeMutationRequest::Start(r) => self.fake.start_vm(r).await,
            }
        })
        .await;
        match sent {
            Ok(Ok(receipt)) => {
                self.scheduler
                    .record_native_receipt(&permit, &receipt)
                    .await?
            }
            other => {
                let label = match other {
                    Err(_) => "timed_out",
                    Ok(Err(PveWriteError::Unauthorized)) => "unauthorized",
                    Ok(Err(PveWriteError::Conflict)) => "conflict",
                    Ok(Err(PveWriteError::Rejected)) => "rejected",
                    Ok(Err(PveWriteError::OutcomeUnknown)) => "outcome_unknown",
                    Ok(Ok(_)) => unreachable!(),
                };
                self.record_write_error(id, label).await?;
            }
        }
        // Always reload: stored acceptance time, not our collection clock, is
        // the independently persisted receipt used by the locked evaluator.
        self.observe_and_decide(id, false).await
    }
    pub async fn reconcile_once(
        &mut self,
        id: OperationId,
    ) -> Result<NativeProgress, NativeControllerError> {
        self.grants.remove(&id);
        tokio::time::timeout(CALL_BOUND, self.reconcile(id))
            .await
            .unwrap_or(Err(NativeControllerError::TimedOut))
    }
    async fn reconcile(
        &mut self,
        id: OperationId,
    ) -> Result<NativeProgress, NativeControllerError> {
        let snapshot = self.store.load_native_operation(id).await?;
        if snapshot.state() != ExecutionState::Unknown {
            // Late evidence remains append-only even when a conflict already
            // fixed the decision. No current decision is reconsidered here.
            if matches!(
                snapshot.state(),
                ExecutionState::Conflicted | ExecutionState::Failed
            ) && snapshot.dispatch().is_some()
            {
                let ownership = self.ownership(&snapshot).await?;
                let facts = self.collect(&snapshot, ownership.as_ref(), false).await?;
                self.store
                    .record_native_evidence(
                        id,
                        snapshot
                            .attempt_id()
                            .ok_or(NativeControllerError::Validation)?,
                        snapshot.revision(),
                        &facts,
                    )
                    .await?;
            }
            return Ok(progress(snapshot.state()));
        }
        if snapshot.dispatch().is_none() {
            return Ok(NativeProgress::Decided(ExecutionState::Unknown));
        }
        self.observe_and_decide(id, true).await
    }
    async fn ownership(
        &self,
        snapshot: &NativeOperationSnapshot,
    ) -> Result<Option<NativeCloneOwnership>, NativeControllerError> {
        if snapshot.plan().step() == NativeStep::Clone {
            Ok(None)
        } else {
            Ok(Some(
                self.store
                    .load_native_clone_ownership(snapshot.operation_id())
                    .await?,
            ))
        }
    }
    async fn record_write_error(
        &self,
        id: OperationId,
        label: &'static str,
    ) -> Result<(), NativeControllerError> {
        let snapshot = self.store.load_native_operation(id).await?;
        let payload = serde_json::json!({"contract_version":1,"mutation_error":label});
        let event = event_journal::JournalEvent::new(
            EventId::new(),
            id,
            snapshot.attempt_id(),
            snapshot.revision() + 1,
            format!("native:mutation-error:{label}"),
            digest(&payload)?,
            event_journal::EventKind::EvidenceRecorded,
            payload,
            Utc::now(),
        )
        .map_err(|_| NativeControllerError::Validation)?;
        self.store
            .append_event(snapshot.revision(), &event)
            .await
            .map_err(|_| NativeControllerError::Storage)?;
        Ok(())
    }
    async fn heartbeat(&mut self, id: OperationId) -> Result<(), NativeControllerError> {
        let grant = self
            .grants
            .get(&id)
            .ok_or(NativeControllerError::FenceLost)?;
        let updated = self
            .scheduler
            .heartbeat(grant)
            .await
            .map_err(|_| NativeControllerError::FenceLost)?;
        self.grants.insert(id, updated);
        Ok(())
    }
    async fn observe_and_decide(
        &mut self,
        id: OperationId,
        reconcile: bool,
    ) -> Result<NativeProgress, NativeControllerError> {
        let snapshot = self.store.load_native_operation(id).await?;
        let ownership = self.ownership(&snapshot).await?;
        let active = !reconcile && !snapshot.cancelled();
        let (facts, fence_lost) = match self.collect(&snapshot, ownership.as_ref(), active).await {
            Ok(facts) => (facts, false),
            Err(NativeControllerError::FenceLost) => {
                // Preserve already-durable receipt evidence without another
                // read or heartbeat after loss of execution authority.
                (
                    NativeEvidence::new(empty(&snapshot, ownership.as_ref())?)
                        .map_err(|_| NativeControllerError::Validation)?,
                    true,
                )
            }
            Err(error) => return Err(error),
        };
        let attempt = snapshot
            .attempt_id()
            .ok_or(NativeControllerError::Validation)?;
        let event = self
            .store
            .record_native_evidence(id, attempt, snapshot.revision(), &facts)
            .await?;
        if fence_lost {
            return Err(NativeControllerError::FenceLost);
        }
        let state = if reconcile {
            self.scheduler
                .reconcile_native_unknown(
                    id,
                    attempt,
                    event,
                    snapshot.revision() + 1,
                    &digest(snapshot.plan())?,
                )
                .await?
        } else {
            let grant = self
                .grants
                .get(&id)
                .ok_or(NativeControllerError::FenceLost)?;
            self.scheduler
                .decide_native(grant, event, snapshot.revision() + 1)
                .await?
        };
        Ok(progress(state))
    }
    async fn read<T>(
        &mut self,
        id: OperationId,
        active: bool,
        read: impl Future<Output = Result<T, PveReadError>>,
    ) -> Result<NativeRead<T>, NativeControllerError> {
        if active {
            self.heartbeat(id).await?;
        }
        let result = tokio::time::timeout(READ_BOUND, read)
            .await
            .unwrap_or(Err(PveReadError::TimedOut));
        Ok(NativeRead::new(Utc::now(), result))
    }
    async fn collect(
        &mut self,
        snapshot: &NativeOperationSnapshot,
        ownership: Option<&NativeCloneOwnership>,
        active: bool,
    ) -> Result<NativeEvidence, NativeControllerError> {
        let id = snapshot.operation_id();
        let mut input = empty(snapshot, ownership)?;
        let budget = if active {
            let remaining = snapshot
                .deadline()
                .ok_or(NativeControllerError::Validation)?
                - Utc::now();
            COLLECTION_BOUND.min(
                remaining
                    .to_std()
                    .map_err(|_| NativeControllerError::FenceLost)?,
            )
        } else {
            COLLECTION_BOUND
        };
        let fake = self.fake.clone();
        let p = snapshot.plan().vm();
        let reads = async {
            input.node = Some(self.read(id, active, fake.node_status(p.node())).await?);
            input.storage = Some(
                self.read(id, active, fake.storage_status(p.node(), p.storage()))
                    .await?,
            );
            input.bridges = Some(self.read(id, active, fake.bridges(p.node())).await?);
            input.inventory = Some(self.read(id, active, fake.cluster_vms()).await?);
            input.source_config = Some(
                self.read(id, active, fake.native_vm_config(p.node(), p.source_vmid()))
                    .await?,
            );
            input.source_power = Some(
                self.read(id, active, fake.vm_status(p.node(), p.source_vmid()))
                    .await?,
            );
            input.target_config = Some(
                self.read(id, active, fake.native_vm_config(p.node(), p.target_vmid()))
                    .await?,
            );
            input.target_power = Some(
                self.read(id, active, fake.vm_status(p.node(), p.target_vmid()))
                    .await?,
            );
            if let Some(Ok(inventory)) = input.inventory.as_ref().map(|r| &r.result) {
                // No truncation is represented as complete coverage. The pure
                // evaluator sees missing identities and fails closed.
                if inventory.vms().len() <= MAX_IDENTITIES {
                    for (vmid, vm) in inventory.vms() {
                        // Reuse this collection's exact source/target read so
                        // each VM is read once, including the retry budget.
                        let read = if vm.node() == p.node() && *vmid == p.source_vmid() {
                            input
                                .source_config
                                .clone()
                                .ok_or(NativeControllerError::Validation)?
                        } else if vm.node() == p.node() && *vmid == p.target_vmid() {
                            input
                                .target_config
                                .clone()
                                .ok_or(NativeControllerError::Validation)?
                        } else {
                            self.read(id, active, fake.native_vm_config(vm.node(), *vmid))
                                .await?
                        };
                        input.identities.push(NativeIdentityRead {
                            node: vm.node().clone(),
                            vmid: *vmid,
                            read,
                        });
                    }
                }
            }
            if let Some(NativeReceipt {
                receipt: MutationReceipt::Task(upid),
                ..
            }) = &input.receipt
            {
                input.task = Some(
                    self.read(id, active, fake.task_status(p.node(), upid))
                        .await?,
                );
            }
            Ok::<(), NativeControllerError>(())
        };
        match tokio::time::timeout(budget, reads).await {
            Ok(Ok(())) | Err(_) => {}
            Ok(Err(e)) => return Err(e),
        }
        input.collected_at = Utc::now();
        input.evaluated_at = input.collected_at;
        NativeEvidence::new(input).map_err(|_| NativeControllerError::Validation)
    }
}
fn digest(value: &impl serde::Serialize) -> Result<String, NativeControllerError> {
    event_journal::payload_digest(
        &serde_json::to_value(value).map_err(|_| NativeControllerError::Validation)?,
    )
    .map_err(|_| NativeControllerError::Validation)
}
fn progress(state: ExecutionState) -> NativeProgress {
    match state {
        ExecutionState::Pending => NativeProgress::Idle,
        ExecutionState::Leased
        | ExecutionState::Running
        | ExecutionState::Waiting
        | ExecutionState::Cancelling => NativeProgress::Waiting,
        _ => NativeProgress::Decided(state),
    }
}
fn empty(
    s: &NativeOperationSnapshot,
    ownership: Option<&NativeCloneOwnership>,
) -> Result<NativeEvidenceInput, NativeControllerError> {
    let t = Utc::now();
    Ok(NativeEvidenceInput {
        binding: NativeBinding::new(
            s.run_id(),
            s.operation_id(),
            s.attempt_id().ok_or(NativeControllerError::Validation)?,
            s.plan(),
            s.revision()
                .try_into()
                .map_err(|_| NativeControllerError::Validation)?,
        ),
        plan: s.plan().clone(),
        source: NativeEvidenceSource::FakePve,
        collected_at: t,
        evaluated_at: t,
        node: None,
        storage: None,
        bridges: None,
        inventory: None,
        identities: vec![],
        source_config: None,
        source_power: None,
        target_config: None,
        target_power: None,
        task: None,
        receipt: s.receipt().cloned(),
        bound_intermediate: ownership.map(|o| o.config().clone()),
    })
}
fn context(
    s: &NativeOperationSnapshot,
    f: &NativeEvidence,
    ownership: Option<NativeCloneOwnership>,
    mode: NativeEvaluationMode,
) -> Result<NativeEvaluationContext, NativeControllerError> {
    let clone_request = if let Some(o) = &ownership {
        o.request().clone()
    } else if let Some(d) = s.dispatch() {
        serde_json::from_value(serde_json::json!({"vm":s.plan().vm(),"operation_id":s.operation_id(),"request_marker":d.request_marker()})).map_err(|_|NativeControllerError::Validation)?
    } else {
        CloneRequest::new(s.plan().vm().clone(), s.operation_id())
    };
    Ok(NativeEvaluationContext {
        binding: f.facts().binding.clone(),
        source: NativeEvidenceSource::FakePve,
        state: s.state(),
        mode,
        cancelled: s.cancelled(),
        possible_dispatch: s.dispatch().is_some(),
        mutation_deadline: s.deadline(),
        transport_lost: s.dispatch().is_some()
            && s.receipt().is_none()
            && s.state() == ExecutionState::Unknown,
        receipt: s.receipt().cloned(),
        dispatched_at: s.dispatch().map(|d| d.dispatched_at()),
        configure_satisfied: s.plan().step() == NativeStep::Start
            && s.predecessor_state() == Some(ExecutionState::Satisfied),
        uninterrupted_workflow: !s.cancelled(),
        clone_request,
        clone_ownership: ownership,
    })
}
fn request(
    s: &NativeOperationSnapshot,
    f: &NativeEvidence,
    ownership: Option<&NativeCloneOwnership>,
) -> Result<NativeMutationRequest, NativeControllerError> {
    if s.plan().step() == NativeStep::Clone {
        return Ok(NativeMutationRequest::Clone(CloneRequest::new(
            s.plan().vm().clone(),
            s.operation_id(),
        )));
    }
    let owned = ownership.ok_or(NativeControllerError::Validation)?;
    let current = f
        .facts()
        .target_config
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .ok_or(NativeControllerError::Validation)?;
    match s.plan().step() {
        NativeStep::Configure => ConfigureRequest::new(
            s.plan().vm().clone(),
            owned.request(),
            owned.config(),
            current,
            Utc::now(),
        )
        .map(NativeMutationRequest::Configure),
        NativeStep::Start => StartRequest::new(
            s.plan().vm().clone(),
            owned.request(),
            owned.config(),
            current,
            Utc::now(),
        )
        .map(NativeMutationRequest::Start),
        NativeStep::Clone => unreachable!(),
    }
    .map_err(|_| NativeControllerError::Validation)
}
