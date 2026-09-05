//! Durable fake-native contracts. Observations are not execution authority.
pub(crate) mod records;
use crate::{CommandAppend, EventAppend, PgStore, StoreError};
use chrono::{DateTime, Utc};
use controller_domain::{
    AttemptId, CommandEnvelope, EventId, ExecutionState, OperationId, RunId, SemanticOperationKey,
    WorkflowKind,
};
use event_journal::{EventKind, JournalEvent};
use pve_port::*;
use serde::Serialize;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum NativeStoreError {
    #[error("native validation failed")]
    Validation,
    #[error("native conflict")]
    Conflict,
    #[error("native evidence is stale")]
    StaleEvidence,
    #[error("native fence mismatch")]
    FenceMismatch,
    #[error("native run cancelled")]
    Cancelled,
    #[error("native operation already dispatched")]
    AlreadyDispatched,
    #[error("native storage unavailable")]
    StorageUnavailable,
}
impl From<sqlx::Error> for NativeStoreError {
    fn from(e: sqlx::Error) -> Self {
        if e.as_database_error()
            .is_some_and(|e| e.is_unique_violation())
        {
            Self::Conflict
        } else {
            Self::StorageUnavailable
        }
    }
}
impl From<serde_json::Error> for NativeStoreError {
    fn from(_: serde_json::Error) -> Self {
        Self::Validation
    }
}
impl From<StoreError> for NativeStoreError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::CommandDigestConflict { .. } | StoreError::EventDigestConflict { .. } => {
                Self::Conflict
            }
            StoreError::RevisionConflict { .. } => Self::FenceMismatch,
            StoreError::Database(_) => Self::StorageUnavailable,
            _ => Self::Validation,
        }
    }
}
impl From<crate::SchedulerError> for NativeStoreError {
    fn from(e: crate::SchedulerError) -> Self {
        match e {
            crate::SchedulerError::Database(_) => Self::StorageUnavailable,
            _ => Self::FenceMismatch,
        }
    }
}
pub(crate) fn digest(value: &impl Serialize) -> Result<String, NativeStoreError> {
    event_journal::payload_digest(&serde_json::to_value(value)?)
        .map_err(|_| NativeStoreError::Validation)
}
pub(crate) fn id<T: serde::de::DeserializeOwned>(value: Uuid) -> Result<T, NativeStoreError> {
    Ok(serde_json::from_value(serde_json::Value::String(
        value.to_string(),
    ))?)
}
pub(crate) const fn step_name(step: NativeStep) -> &'static str {
    match step {
        NativeStep::Clone => "clone",
        NativeStep::Configure => "configure",
        NativeStep::Start => "start",
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeWorkflowIds {
    run_id: RunId,
    clone_id: OperationId,
    configure_id: OperationId,
    start_id: OperationId,
}
impl NativeWorkflowIds {
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }
    pub const fn clone_id(&self) -> OperationId {
        self.clone_id
    }
    pub const fn configure_id(&self) -> OperationId {
        self.configure_id
    }
    pub const fn start_id(&self) -> OperationId {
        self.start_id
    }
    pub(crate) fn operations(&self) -> [OperationId; 3] {
        [self.clone_id, self.configure_id, self.start_id]
    }
}
#[derive(Clone, Debug)]
pub struct NativeDispatch {
    pub(crate) operation_id: OperationId,
    pub(crate) attempt_id: AttemptId,
    pub(crate) plan_digest: String,
    pub(crate) generation: i64,
    pub(crate) revision: i64,
    pub(crate) request_digest: String,
    pub(crate) request_marker: Uuid,
    pub(crate) preflight_event_id: EventId,
    pub(crate) dispatched_at: DateTime<Utc>,
}
impl NativeDispatch {
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }
    pub const fn attempt_id(&self) -> AttemptId {
        self.attempt_id
    }
    pub fn plan_digest(&self) -> &str {
        &self.plan_digest
    }
    pub const fn generation(&self) -> i64 {
        self.generation
    }
    pub const fn revision(&self) -> i64 {
        self.revision
    }
    pub fn request_digest(&self) -> &str {
        &self.request_digest
    }
    pub const fn request_marker(&self) -> Uuid {
        self.request_marker
    }
    pub const fn preflight_event_id(&self) -> EventId {
        self.preflight_event_id
    }
    pub const fn dispatched_at(&self) -> DateTime<Utc> {
        self.dispatched_at
    }
}
/// Constructed only by the committed pre-send transaction. Not Clone or serde.
/// No lease token is retained or exposed; the immutable dispatch is its fence.
#[derive(Debug)]
pub struct NativeDispatchPermit {
    pub(crate) dispatch: NativeDispatch,
}
impl NativeDispatchPermit {
    pub const fn dispatch(&self) -> &NativeDispatch {
        &self.dispatch
    }
}

#[derive(Clone, Debug)]
pub struct NativeDecisionRecord {
    pub(crate) revision: i64,
    pub(crate) attempt_id: AttemptId,
    pub(crate) evidence_event_id: EventId,
    pub(crate) generation: i64,
    pub(crate) evaluation: NativeEvaluation,
    pub(crate) evaluated_at: DateTime<Utc>,
}
impl NativeDecisionRecord {
    pub const fn revision(&self) -> i64 {
        self.revision
    }
    pub const fn attempt_id(&self) -> AttemptId {
        self.attempt_id
    }
    pub const fn evidence_event_id(&self) -> EventId {
        self.evidence_event_id
    }
    pub const fn generation(&self) -> i64 {
        self.generation
    }
    pub const fn evaluation(&self) -> NativeEvaluation {
        self.evaluation
    }
    pub const fn evaluated_at(&self) -> DateTime<Utc> {
        self.evaluated_at
    }
}
#[derive(Clone, Debug)]
pub struct NativeOperationSnapshot {
    pub(crate) operation_id: OperationId,
    pub(crate) run_id: RunId,
    pub(crate) plan: NativeOperationPlan,
    pub(crate) state: ExecutionState,
    pub(crate) revision: i64,
    pub(crate) predecessor_id: Option<OperationId>,
    pub(crate) predecessor_state: Option<ExecutionState>,
    pub(crate) attempt_id: Option<AttemptId>,
    pub(crate) attempt_state: Option<ExecutionState>,
    pub(crate) deadline: Option<DateTime<Utc>>,
    pub(crate) dispatch: Option<NativeDispatch>,
    pub(crate) receipt: Option<NativeReceipt>,
    pub(crate) evidence: Option<(EventId, NativeEvidence)>,
    pub(crate) decision: Option<NativeDecisionRecord>,
    pub(crate) cancelled: bool,
}
impl NativeOperationSnapshot {
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }
    pub const fn plan(&self) -> &NativeOperationPlan {
        &self.plan
    }
    pub const fn state(&self) -> ExecutionState {
        self.state
    }
    pub const fn revision(&self) -> i64 {
        self.revision
    }
    pub const fn predecessor_id(&self) -> Option<OperationId> {
        self.predecessor_id
    }
    pub const fn predecessor_state(&self) -> Option<ExecutionState> {
        self.predecessor_state
    }
    pub const fn attempt_id(&self) -> Option<AttemptId> {
        self.attempt_id
    }
    pub const fn attempt_state(&self) -> Option<ExecutionState> {
        self.attempt_state
    }
    pub const fn deadline(&self) -> Option<DateTime<Utc>> {
        self.deadline
    }
    pub const fn dispatch(&self) -> Option<&NativeDispatch> {
        self.dispatch.as_ref()
    }
    pub const fn receipt(&self) -> Option<&NativeReceipt> {
        self.receipt.as_ref()
    }
    pub const fn evidence(&self) -> Option<&(EventId, NativeEvidence)> {
        self.evidence.as_ref()
    }
    pub const fn decision(&self) -> Option<&NativeDecisionRecord> {
        self.decision.as_ref()
    }
    pub const fn cancelled(&self) -> bool {
        self.cancelled
    }
}

pub(crate) async fn lock_run(
    tx: &mut Transaction<'_, Postgres>,
    run: RunId,
) -> Result<(), NativeStoreError> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!("native:run:{}", run.as_uuid()))
        .execute(&mut **tx)
        .await?;
    Ok(())
}
pub(crate) async fn operation_run(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<RunId, NativeStoreError> {
    let run:Option<Uuid>=sqlx::query_scalar("SELECT run_id FROM rust_controller.operations WHERE operation_id=$1 AND workflow_kind='native_pve_vm_boot'").bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
    id(run.ok_or(NativeStoreError::Validation)?)
}
pub(crate) async fn workflow_ids(
    tx: &mut Transaction<'_, Postgres>,
    run: RunId,
) -> Result<NativeWorkflowIds, NativeStoreError> {
    let rows:Vec<(Uuid,String)>=sqlx::query_as("SELECT o.operation_id,p.step FROM rust_controller.operations o JOIN rust_controller.native_operation_plans p USING(operation_id) WHERE o.run_id=$1 AND o.workflow_kind='native_pve_vm_boot'").bind(run.as_uuid()).fetch_all(&mut **tx).await?;
    if rows.len() != 3 {
        return Err(NativeStoreError::Validation);
    }
    let find = |step: &str| {
        rows.iter()
            .find(|r| r.1 == step)
            .ok_or(NativeStoreError::Validation)
            .and_then(|r| id(r.0))
    };
    Ok(NativeWorkflowIds {
        run_id: run,
        clone_id: find("clone")?,
        configure_id: find("configure")?,
        start_id: find("start")?,
    })
}
impl PgStore {
    pub async fn enqueue_native_vm(
        &self,
        run_id: RunId,
        plan: &NativeVmPlan,
    ) -> Result<NativeWorkflowIds, NativeStoreError> {
        let plan: NativeVmPlan = serde_json::from_value(serde_json::to_value(plan)?)?;
        let plan_digest = digest(&plan)?;
        let mut tx = self.pool().begin().await?;
        lock_run(&mut tx, run_id).await?;
        let existing: Option<String> = sqlx::query_scalar(
            "SELECT plan_digest FROM rust_controller.native_vm_reservations WHERE run_id=$1",
        )
        .bind(run_id.as_uuid())
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(existing) = existing {
            if existing != plan_digest {
                return Err(NativeStoreError::Conflict);
            }
            let ids = workflow_ids(&mut tx, run_id).await?;
            for operation in ids.operations() {
                records::load(&mut tx, operation).await?;
            }
            tx.commit().await?;
            return Ok(ids);
        }
        let mut keys = [
            format!(
                "native:identity:{}:vmid:{}",
                plan.cluster_key(),
                plan.target_vmid()
            ),
            format!(
                "native:identity:{}:uuid:{}",
                plan.cluster_key(),
                plan.uuid()
            ),
            format!("native:identity:{}:mac:{}", plan.cluster_key(), plan.mac()),
        ];
        keys.sort();
        for key in keys {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
                .bind(key)
                .execute(&mut *tx)
                .await?;
        }
        sqlx::query("INSERT INTO rust_controller.native_vm_reservations(cluster_key,vmid,vm_uuid,mac,run_id,plan_digest) VALUES($1,$2,$3,$4,$5,$6)")
            .bind(plan.cluster_key().to_string()).bind(plan.target_vmid().to_string().parse::<i32>().map_err(|_|NativeStoreError::Validation)?)
            .bind(plan.uuid().to_string().parse::<Uuid>().map_err(|_|NativeStoreError::Validation)?).bind(plan.mac().to_string()).bind(run_id.as_uuid()).bind(plan_digest).execute(&mut *tx).await?;
        let ids = NativeWorkflowIds {
            run_id,
            clone_id: OperationId::new(),
            configure_id: OperationId::new(),
            start_id: OperationId::new(),
        };
        let mut predecessor: Option<OperationId> = None;
        for (operation_id, step) in ids.operations().into_iter().zip([
            NativeStep::Clone,
            NativeStep::Configure,
            NativeStep::Start,
        ]) {
            let plan = NativeOperationPlan::new(step, plan.clone());
            let payload_digest = digest(&plan)?;
            let semantic = SemanticOperationKey::new(
                WorkflowKind::NativePveVmBoot,
                run_id,
                step.operation_key(),
                1,
            )
            .map_err(|_| NativeStoreError::Validation)?;
            let command = CommandEnvelope::new(
                format!("native:{}:{}", run_id.as_uuid(), step.operation_key()),
                semantic,
                payload_digest.clone(),
            )
            .map_err(|_| NativeStoreError::Validation)?;
            if Self::append_command_tx(&mut tx, operation_id, &command).await?
                != CommandAppend::Appended(operation_id)
            {
                return Err(NativeStoreError::Conflict);
            }
            sqlx::query("INSERT INTO rust_controller.native_operation_plans(operation_id,predecessor_id,step,payload_digest,plan) VALUES($1,$2,$3,$4,$5)").bind(operation_id.as_uuid()).bind(predecessor.map(|p|p.as_uuid())).bind(step_name(step)).bind(payload_digest).bind(serde_json::to_value(&plan)?).execute(&mut *tx).await?;
            predecessor = Some(operation_id);
        }
        tx.commit().await?;
        Ok(ids)
    }
    pub async fn load_native_operation(
        &self,
        operation_id: OperationId,
    ) -> Result<NativeOperationSnapshot, NativeStoreError> {
        let mut tx = self.pool().begin().await?;
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .execute(&mut *tx)
            .await?;
        let snapshot = records::load(&mut tx, operation_id).await?;
        tx.commit().await?;
        Ok(snapshot)
    }
    pub async fn record_native_evidence(
        &self,
        operation_id: OperationId,
        attempt_id: AttemptId,
        expected_revision: i64,
        facts: &NativeEvidence,
    ) -> Result<EventId, NativeStoreError> {
        let snapshot = self.load_native_operation(operation_id).await?;
        let b = &facts.facts().binding;
        if b.operation_id() != operation_id
            || b.attempt_id() != attempt_id
            || b.run_id() != snapshot.run_id
            || facts.facts().plan != snapshot.plan
            || facts.facts().source != NativeEvidenceSource::FakePve
            || b.evidence_fence()
                != u64::try_from(expected_revision).map_err(|_| NativeStoreError::Validation)?
        {
            return Err(NativeStoreError::Validation);
        }
        // Evidence is intentionally admissible after a newer terminal decision.
        // The scheduler separately fences consumption under its operation lock.
        let payload = serde_json::to_value(facts)?;
        let hash = digest(facts)?;
        let event = JournalEvent::new(
            EventId::new(),
            operation_id,
            Some(attempt_id),
            expected_revision
                .checked_add(1)
                .ok_or(NativeStoreError::Validation)?,
            format!("native:evidence:{hash}"),
            hash,
            EventKind::EvidenceRecorded,
            payload,
            facts.facts().collected_at,
        )
        .map_err(|_| NativeStoreError::Validation)?;
        match self.append_event(expected_revision, &event).await? {
            EventAppend::Appended(_) => Ok(event.event_id()),
            EventAppend::AlreadyPresent(id) => Ok(id),
        }
    }
}
