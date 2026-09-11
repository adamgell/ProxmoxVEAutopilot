mod authority;
mod lease;
mod native;
mod osdeploy;

use crate::PgStore;
use chrono::{DateTime, Utc};
use controller_domain::{
    AttemptId, DomainSignal, EventId, ExecutionState, OperationId, TransitionError, WorkflowKind,
    decide_transition,
};
use event_journal::{
    CanonicalizationError, EventKind, EventValidationError, JournalEvent, payload_digest,
};
use serde_json::json;
use sqlx::{Postgres, Transaction};
use thiserror::Error;
use uuid::Uuid;

pub use authority::{AuthoritySnapshot, ExecutorKind};
pub use lease::{LeaseGrant, ReapSummary};
#[cfg(feature = "fixture-ipc")]
pub use osdeploy::{
    FixtureBootFilesStagedResult, FixtureCredentialEnvelope, FixtureCredentialSink,
    FixtureDeliveryAck, FixtureDeliveryRecovery, FixturePeCompletionReport,
    FixturePeCompletionResult, FixturePeRegistrationIdentity, FixturePeRegistrationResult,
    FixtureStopOutboxConsumedV1,
};
pub use osdeploy::{
    OsDeployDispatchPermit, OsDeployLeaseStatus, OsDeployMaintenanceSummary,
    OsDeployResponseCapture,
};

const REAP_BATCH_LIMIT: i64 = 32;

#[derive(Clone)]
pub struct Scheduler {
    store: PgStore,
    executor_kind: ExecutorKind,
    generation: i64,
    worker_id: String,
    fixture_start_pe: bool,
    #[cfg(feature = "fixture-ipc")]
    fixture_credential_delivery: bool,
}

impl Scheduler {
    /// Admit only the fixture credential delivery path for credential origins.
    #[cfg(feature = "fixture-ipc")]
    pub fn with_fixture_credential_delivery(mut self) -> Self {
        self.fixture_start_pe = true;
        self.fixture_credential_delivery = true;
        self
    }
    /// Enables fixture boot arming only; conveys no callback authentication.
    #[cfg(feature = "fixture-ipc")]
    pub fn with_fixture_start_pe(mut self) -> Self {
        self.fixture_start_pe = true;
        self
    }

    pub fn fixture_start_pe_enabled(&self) -> bool {
        self.fixture_start_pe
    }
    /// Select only an explicitly configured contract and canonical plan, inside
    /// the same cap/authority/claim transaction. Unrelated work stays pending.
    pub async fn claim_next_bound(
        &self,
        kind: WorkflowKind,
        cap: u32,
        version: u16,
        fingerprint: &str,
    ) -> Result<Option<LeaseGrant>, SchedulerError> {
        self.claim_checked(kind, cap, Some((version, fingerprint)))
            .await
    }
    pub fn new(
        store: PgStore,
        executor_kind: ExecutorKind,
        generation: i64,
        worker_id: impl Into<String>,
    ) -> Result<Self, SchedulerError> {
        let worker_id = worker_id.into();
        if generation <= 0 {
            return Err(SchedulerError::InvalidGeneration { generation });
        }
        if worker_id.trim().is_empty() {
            return Err(SchedulerError::InvalidWorkerId);
        }
        Ok(Self {
            store,
            executor_kind,
            generation,
            worker_id,
            fixture_start_pe: false,
            #[cfg(feature = "fixture-ipc")]
            fixture_credential_delivery: false,
        })
    }

    pub async fn claim_next(
        &self,
        kind: WorkflowKind,
        cap: u32,
    ) -> Result<Option<LeaseGrant>, SchedulerError> {
        self.claim_checked(kind, cap, None).await
    }

    async fn claim_checked(
        &self,
        kind: WorkflowKind,
        cap: u32,
        binding: Option<(u16, &str)>,
    ) -> Result<Option<LeaseGrant>, SchedulerError> {
        if kind == WorkflowKind::NativePveVmBoot {
            return Ok(None);
        }
        if cap == 0 {
            return Err(SchedulerError::InvalidCap { cap });
        }
        if kind == WorkflowKind::OsDeploy {
            return Ok(None);
        }
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;

        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(format!(
                "rust-controller:scheduler:{}",
                workflow_kind_name(kind)
            ))
            .execute(&mut *transaction)
            .await?;

        let active: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM rust_controller.worker_leases leases \
             JOIN rust_controller.operations operations USING (operation_id) \
             WHERE operations.workflow_kind = $1 \
               AND leases.executor_kind = $2 AND leases.generation = $3 \
               AND leases.lease_expires_at > clock_timestamp()",
        )
        .bind(workflow_kind_name(kind))
        .bind(self.executor_kind.as_str())
        .bind(self.generation)
        .fetch_one(&mut *transaction)
        .await?;
        if active >= i64::from(cap) {
            transaction.commit().await?;
            return Ok(None);
        }

        let candidate: Option<(Uuid, i64)> = sqlx::query_as(
            "SELECT operation_id, revision FROM rust_controller.operations \
             WHERE workflow_kind = $1 AND state = 'pending' \
             AND (SELECT count(DISTINCT c.payload_digest) FROM rust_controller.commands c \
                  WHERE c.operation_id = operations.operation_id) = 1 \
             AND ($2::integer IS NULL OR (contract_version = $2 AND EXISTS (\
                 SELECT 1 FROM rust_controller.commands c \
                 WHERE c.operation_id = operations.operation_id AND c.payload_digest = $3))) \
             ORDER BY created_at, operation_id \
             FOR UPDATE SKIP LOCKED LIMIT 1",
        )
        .bind(workflow_kind_name(kind))
        .bind(binding.map(|(version, _)| i32::from(version)))
        .bind(binding.map(|(_, fingerprint)| fingerprint))
        .fetch_optional(&mut *transaction)
        .await?;
        let Some((operation_uuid, revision)) = candidate else {
            transaction.commit().await?;
            return Ok(None);
        };
        let operation_id = decode_operation_id(operation_uuid)?;
        let grant = self
            .claim_operation_tx(&mut transaction, operation_id, revision)
            .await?;
        transaction.commit().await?;
        Ok(Some(grant))
    }

    async fn claim_operation_tx(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        operation_id: OperationId,
        revision: i64,
    ) -> Result<LeaseGrant, SchedulerError> {
        let operation_uuid = operation_id.as_uuid();
        let attempt_number: i32 = sqlx::query_scalar(
            "SELECT COALESCE(max(attempt_number), 0) + 1 \
             FROM rust_controller.attempts WHERE operation_id = $1",
        )
        .bind(operation_uuid)
        .fetch_one(&mut **transaction)
        .await?;
        let attempt_id = AttemptId::new();
        let lease_token = Uuid::now_v7();

        let (acquired_at, deadline_at): (DateTime<Utc>, DateTime<Utc>) = sqlx::query_as(
            "INSERT INTO rust_controller.attempts \
             (attempt_id, operation_id, attempt_number, state, started_at, deadline_at) \
             VALUES ($1, $2, $3, 'leased', clock_timestamp(), \
                     clock_timestamp() + interval '5 minutes') \
             RETURNING started_at, deadline_at",
        )
        .bind(attempt_id.as_uuid())
        .bind(operation_uuid)
        .bind(attempt_number)
        .fetch_one(&mut **transaction)
        .await?;
        let (lease_acquired_at, heartbeat_at, lease_expires_at, lease_deadline_at): (
            DateTime<Utc>,
            DateTime<Utc>,
            DateTime<Utc>,
            DateTime<Utc>,
        ) = sqlx::query_as(
            "INSERT INTO rust_controller.worker_leases \
             (operation_id, attempt_id, executor_kind, generation, worker_id, lease_token, \
              acquired_at, heartbeat_at, lease_expires_at, deadline_at) \
             VALUES ($1, $2, $3, $4, $5, $6, clock_timestamp(), clock_timestamp(), \
                     clock_timestamp() + interval '30 seconds', $7) \
             RETURNING acquired_at, heartbeat_at, lease_expires_at, deadline_at",
        )
        .bind(operation_uuid)
        .bind(attempt_id.as_uuid())
        .bind(self.executor_kind.as_str())
        .bind(self.generation)
        .bind(&self.worker_id)
        .bind(lease_token.to_string())
        .bind(deadline_at)
        .fetch_one(&mut **transaction)
        .await?;
        debug_assert!(lease_acquired_at >= acquired_at);

        let payload = json!({
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "executor_kind": self.executor_kind.as_str(),
            "generation": self.generation,
            "state": "leased",
            "worker_id": self.worker_id,
        });
        append_state_event(
            transaction,
            StateAppend {
                operation_id,
                attempt_id: Some(attempt_id),
                revision,
                current: ExecutionState::Pending,
                target: ExecutionState::Leased,
                semantic_key: format!("scheduler:leased:{}", attempt_id.as_uuid()),
                payload,
                observed_at: lease_acquired_at,
                policy: TransitionPolicy::Domain,
            },
        )
        .await?;
        Ok(LeaseGrant::new(
            operation_id,
            attempt_id,
            attempt_number,
            self.executor_kind,
            self.generation,
            self.worker_id.clone(),
            lease_token,
            lease_acquired_at,
            heartbeat_at,
            lease_expires_at,
            lease_deadline_at,
        ))
    }

    pub async fn authority_snapshot(&self) -> Result<AuthoritySnapshot, SchedulerError> {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT executor_kind, generation \
             FROM rust_controller.orchestration_authority WHERE singleton_key = 1",
        )
        .fetch_optional(self.store.pool())
        .await?;
        decode_authority(row)
    }

    pub async fn transition_authority(
        &self,
        next_executor: ExecutorKind,
        change_reference: &str,
    ) -> Result<AuthoritySnapshot, SchedulerError> {
        if change_reference.trim().is_empty() {
            return Err(SchedulerError::InvalidChangeReference);
        }
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority_for_update(&mut transaction).await?;
        let row: Option<(String, i64)> = sqlx::query_as(
            "UPDATE rust_controller.orchestration_authority \
             SET executor_kind = $1, generation = generation + 1, \
                 changed_at = clock_timestamp(), change_reference = $2 \
             WHERE singleton_key = 1 AND executor_kind = $3 AND generation = $4 \
             RETURNING executor_kind, generation",
        )
        .bind(next_executor.as_str())
        .bind(change_reference)
        .bind(self.executor_kind.as_str())
        .bind(self.generation)
        .fetch_optional(&mut *transaction)
        .await?;
        let snapshot = decode_authority(row)?;
        transaction.commit().await?;
        Ok(snapshot)
    }

    pub async fn start(&self, grant: &LeaseGrant) -> Result<ExecutionState, SchedulerError> {
        self.start_checked(grant, None).await
    }

    /// Atomically bind execution to the persisted command and operation schema.
    /// A registry-validated plan cannot be substituted under another lease.
    pub async fn start_bound(
        &self,
        grant: &LeaseGrant,
        kind: WorkflowKind,
        contract_version: u16,
        fingerprint: &str,
    ) -> Result<ExecutionState, SchedulerError> {
        self.start_checked(grant, Some((kind, contract_version, fingerprint)))
            .await
    }

    async fn start_checked(
        &self,
        grant: &LeaseGrant,
        binding: Option<(WorkflowKind, u16, &str)>,
    ) -> Result<ExecutionState, SchedulerError> {
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        self.validate_grant_owner(grant)?;
        let (state, revision) = lock_operation(&mut transaction, grant.operation_id()).await?;
        reject_typed_dispatch(&mut transaction, grant.operation_id()).await?;
        let persisted = lock_lease(&mut transaction, grant.operation_id()).await?;
        validate_persisted_lease(grant, &persisted)?;
        let distinct: i64 = sqlx::query_scalar(
            "SELECT count(DISTINCT payload_digest) FROM rust_controller.commands WHERE operation_id = $1",
        ).bind(grant.operation_id().as_uuid()).fetch_one(&mut *transaction).await?;
        if distinct != 1 {
            return Err(SchedulerError::PlanBindingMismatch);
        }
        if let Some((kind, version, fingerprint)) = binding {
            let matches: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM rust_controller.operations o \
                 JOIN rust_controller.commands c USING (operation_id) \
                 WHERE o.operation_id = $1 AND o.workflow_kind = $2 \
                 AND o.contract_version = $3 AND c.payload_digest = $4)",
            )
            .bind(grant.operation_id().as_uuid())
            .bind(workflow_kind_name(kind))
            .bind(i32::from(version))
            .bind(fingerprint)
            .fetch_one(&mut *transaction)
            .await?;
            if !matches {
                return Err(SchedulerError::PlanBindingMismatch);
            }
        }
        if state == ExecutionState::Cancelling {
            return Err(SchedulerError::CancellationRequested);
        }
        if state != ExecutionState::Leased || persisted.attempt_state != ExecutionState::Leased {
            return Err(SchedulerError::NotStartable { state });
        }
        if !persisted.active {
            return Err(SchedulerError::LeaseExpired);
        }
        self.start_operation_tx(&mut transaction, grant, revision)
            .await?;
        transaction.commit().await?;
        Ok(ExecutionState::Running)
    }

    async fn start_operation_tx(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        grant: &LeaseGrant,
        revision: i64,
    ) -> Result<(), SchedulerError> {
        let observed_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **transaction)
            .await?;
        let started_revision = append_attempt_started_event(
            transaction,
            grant.operation_id(),
            grant.attempt_id(),
            revision,
            observed_at,
        )
        .await?;
        append_state_event(
            transaction,
            StateAppend {
                operation_id: grant.operation_id(),
                attempt_id: Some(grant.attempt_id()),
                revision: started_revision,
                current: ExecutionState::Leased,
                target: ExecutionState::Running,
                semantic_key: format!("scheduler:running:{}", grant.attempt_id().as_uuid()),
                payload: json!({"state": "running"}),
                observed_at,
                policy: TransitionPolicy::Domain,
            },
        )
        .await?;
        sqlx::query(
            "UPDATE rust_controller.attempts SET state = 'running' \
             WHERE attempt_id = $1 AND operation_id = $2 AND state = 'leased'",
        )
        .bind(grant.attempt_id().as_uuid())
        .bind(grant.operation_id().as_uuid())
        .execute(&mut **transaction)
        .await?;
        Ok(())
    }

    pub async fn continuation(&self, grant: &LeaseGrant) -> Result<ExecutionState, SchedulerError> {
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        reject_osdeploy(&mut transaction, grant.operation_id()).await?;
        lock_native_run_if_present(&mut transaction, grant.operation_id()).await?;
        self.validate_grant_owner(grant)?;
        let (state, _) = lock_operation(&mut transaction, grant.operation_id()).await?;
        let persisted = lock_lease(&mut transaction, grant.operation_id()).await?;
        validate_persisted_lease(grant, &persisted)?;
        if state == ExecutionState::Cancelling {
            return Err(SchedulerError::CancellationRequested);
        }
        if !matches!(state, ExecutionState::Running | ExecutionState::Waiting)
            || persisted.attempt_state != state
        {
            return Err(SchedulerError::NotStarted);
        }
        if !persisted.active {
            return Err(SchedulerError::LeaseExpired);
        }
        transaction.commit().await?;
        Ok(state)
    }

    pub async fn heartbeat(&self, grant: &LeaseGrant) -> Result<LeaseGrant, SchedulerError> {
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        reject_osdeploy(&mut transaction, grant.operation_id()).await?;
        lock_native_run_if_present(&mut transaction, grant.operation_id()).await?;
        self.validate_grant_owner(grant)?;
        let (state, _) = lock_operation(&mut transaction, grant.operation_id()).await?;
        let persisted = lock_lease(&mut transaction, grant.operation_id()).await?;
        validate_persisted_lease(grant, &persisted)?;
        if state == ExecutionState::Cancelling {
            return Err(SchedulerError::CancellationRequested);
        }
        if !matches!(state, ExecutionState::Running | ExecutionState::Waiting)
            || persisted.attempt_state != state
        {
            return Err(SchedulerError::NotStarted);
        }
        if !persisted.active {
            return Err(SchedulerError::LeaseExpired);
        }

        let (heartbeat_at, lease_expires_at): (DateTime<Utc>, DateTime<Utc>) = sqlx::query_as(
            "UPDATE rust_controller.worker_leases \
             SET heartbeat_at = clock_timestamp(), \
                 lease_expires_at = LEAST(clock_timestamp() + interval '30 seconds', deadline_at) \
             WHERE operation_id = $1 \
             RETURNING heartbeat_at, lease_expires_at",
        )
        .bind(grant.operation_id().as_uuid())
        .fetch_one(&mut *transaction)
        .await?;
        transaction.commit().await?;

        Ok(LeaseGrant::new(
            grant.operation_id(),
            grant.attempt_id(),
            grant.attempt_number(),
            grant.executor_kind(),
            grant.generation(),
            grant.worker_id().to_owned(),
            grant.lease_token(),
            *grant.acquired_at(),
            heartbeat_at,
            lease_expires_at,
            *grant.deadline_at(),
        ))
    }

    pub async fn request_cancel(
        &self,
        operation_id: OperationId,
    ) -> Result<ExecutionState, SchedulerError> {
        self.cancel_checked(operation_id, None).await
    }

    /// Worker cancellation must validate its full lease in the same transaction
    /// as the state change. Operator cancellation retains its operation API.
    pub async fn request_cancel_bound(
        &self,
        grant: &LeaseGrant,
    ) -> Result<ExecutionState, SchedulerError> {
        self.cancel_checked(grant.operation_id(), Some(grant)).await
    }

    async fn cancel_checked(
        &self,
        operation_id: OperationId,
        grant: Option<&LeaseGrant>,
    ) -> Result<ExecutionState, SchedulerError> {
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        let (state, revision) = lock_operation(&mut transaction, operation_id).await?;
        reject_typed_dispatch(&mut transaction, operation_id).await?;
        if let Some(grant) = grant {
            self.validate_grant_owner(grant)?;
            let persisted = lock_lease(&mut transaction, operation_id).await?;
            validate_persisted_lease(grant, &persisted)?;
            if !persisted.active {
                return Err(SchedulerError::LeaseExpired);
            }
        }
        if state == ExecutionState::Cancelling {
            transaction.commit().await?;
            return Ok(state);
        }
        if !matches!(
            state,
            ExecutionState::Leased | ExecutionState::Running | ExecutionState::Waiting
        ) {
            return Err(SchedulerError::CancellationNotRunning { state });
        }
        let lease = lock_lease(&mut transaction, operation_id).await?;
        let attempt_id = decode_attempt_id(lease.attempt_id)?;
        let next_revision = revision
            .checked_add(1)
            .ok_or(SchedulerError::RevisionOverflow)?;
        let observed_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *transaction)
            .await?;
        append_state_event(
            &mut transaction,
            StateAppend {
                operation_id,
                attempt_id: Some(attempt_id),
                revision,
                current: state,
                target: ExecutionState::Cancelling,
                semantic_key: format!("scheduler:cancelling:{next_revision}"),
                payload: json!({"reason": "cancellation_requested", "state": "cancelling"}),
                observed_at,
                policy: if state == ExecutionState::Leased {
                    TransitionPolicy::Cancellation
                } else {
                    TransitionPolicy::Domain
                },
            },
        )
        .await?;
        sqlx::query(
            "UPDATE rust_controller.attempts SET state = 'cancelling' \
             WHERE attempt_id = $1 AND operation_id = $2",
        )
        .bind(attempt_id.as_uuid())
        .bind(operation_id.as_uuid())
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(ExecutionState::Cancelling)
    }

    pub async fn finalize(
        &self,
        grant: &LeaseGrant,
        state: ExecutionState,
    ) -> Result<ExecutionState, SchedulerError> {
        if !state.is_terminal() {
            return Err(SchedulerError::InvalidFinalState { state });
        }
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        self.validate_grant_owner(grant)?;
        let (current, revision) = lock_operation(&mut transaction, grant.operation_id()).await?;
        reject_typed_dispatch(&mut transaction, grant.operation_id()).await?;
        let persisted = lock_lease(&mut transaction, grant.operation_id()).await?;
        validate_persisted_lease(grant, &persisted)?;
        if current == ExecutionState::Leased {
            return Err(SchedulerError::NotStarted);
        }
        if current == ExecutionState::Cancelling && state != ExecutionState::Unknown {
            return Err(SchedulerError::CancellationRequiresUnknown);
        }
        if !matches!(
            current,
            ExecutionState::Running | ExecutionState::Waiting | ExecutionState::Cancelling
        ) {
            return Err(SchedulerError::NotFinalizable { state: current });
        }
        if !persisted.active {
            return Err(SchedulerError::LeaseExpired);
        }
        let observed_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *transaction)
            .await?;
        append_state_event(
            &mut transaction,
            StateAppend {
                operation_id: grant.operation_id(),
                attempt_id: Some(grant.attempt_id()),
                revision,
                current,
                target: state,
                semantic_key: format!(
                    "scheduler:final:{}:{}",
                    grant.attempt_id().as_uuid(),
                    execution_state_name(state)
                ),
                payload: json!({"state": execution_state_name(state)}),
                observed_at,
                policy: if current == ExecutionState::Cancelling {
                    TransitionPolicy::CancellationUnknown
                } else {
                    TransitionPolicy::Domain
                },
            },
        )
        .await?;
        sqlx::query(
            "UPDATE rust_controller.attempts \
             SET state = $3, completed_at = clock_timestamp() \
             WHERE attempt_id = $1 AND operation_id = $2",
        )
        .bind(grant.attempt_id().as_uuid())
        .bind(grant.operation_id().as_uuid())
        .bind(execution_state_name(state))
        .execute(&mut *transaction)
        .await?;
        sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id = $1")
            .bind(grant.operation_id().as_uuid())
            .execute(&mut *transaction)
            .await?;
        transaction.commit().await?;
        Ok(state)
    }

    pub async fn reap_expired(&self) -> Result<ReapSummary, SchedulerError> {
        let native_summary = self.reap_native_expired().await?;
        let mut transaction = self.store.pool().begin().await?;
        self.lock_authority(&mut transaction).await?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind("rust-controller:scheduler:reaper")
            .execute(&mut *transaction)
            .await?;
        let operation_ids: Vec<Uuid> = sqlx::query_scalar(
            "SELECT operations.operation_id FROM rust_controller.operations operations \
             JOIN rust_controller.worker_leases leases USING (operation_id) \
             WHERE leases.lease_expires_at <= clock_timestamp() \
               AND operations.workflow_kind NOT IN ('native_pve_vm_boot','os_deploy') \
             ORDER BY operations.operation_id \
             FOR UPDATE OF operations SKIP LOCKED LIMIT $1",
        )
        .bind(REAP_BATCH_LIMIT)
        .fetch_all(&mut *transaction)
        .await?;
        let mut summary = native_summary;

        for operation_uuid in operation_ids {
            let operation_id = decode_operation_id(operation_uuid)?;
            let (current, revision) = lock_operation(&mut transaction, operation_id).await?;
            if is_osdeploy(&mut transaction, operation_id).await? {
                continue;
            }
            let lease = lock_lease(&mut transaction, operation_id).await?;
            if lease.active {
                continue;
            }
            let attempt_id = decode_attempt_id(lease.attempt_id)?;
            let mutation_started: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM rust_controller.journal_events \
                 WHERE operation_id = $1 AND attempt_id = $2 AND event_kind = 'attempt_started')",
            )
            .bind(operation_uuid)
            .bind(lease.attempt_id)
            .fetch_one(&mut *transaction)
            .await?;

            let decision = match (current, mutation_started) {
                (ExecutionState::Leased, false) => Some((
                    ExecutionState::Pending,
                    TransitionPolicy::ExpiredUnstarted,
                    "lease_expired_before_mutation",
                )),
                (ExecutionState::Leased, true) => Some((
                    ExecutionState::Unknown,
                    TransitionPolicy::ExpiredAfterStart,
                    "lease_expired_after_mutation_start",
                )),
                (
                    ExecutionState::Running | ExecutionState::Waiting | ExecutionState::Cancelling,
                    _,
                ) => Some((
                    ExecutionState::Unknown,
                    TransitionPolicy::ExpiredAfterStart,
                    "lease_expired_in_flight",
                )),
                _ => None,
            };
            if let Some((target, policy, reason)) = decision {
                let observed_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
                    .fetch_one(&mut *transaction)
                    .await?;
                append_state_event(
                    &mut transaction,
                    StateAppend {
                        operation_id,
                        attempt_id: Some(attempt_id),
                        revision,
                        current,
                        target,
                        semantic_key: format!("scheduler:lease-expired:{}", attempt_id.as_uuid()),
                        payload: json!({"reason": reason, "state": execution_state_name(target)}),
                        observed_at,
                        policy,
                    },
                )
                .await?;
                sqlx::query(
                    "UPDATE rust_controller.attempts \
                     SET state = 'unknown', completed_at = clock_timestamp() \
                     WHERE attempt_id = $1 AND operation_id = $2",
                )
                .bind(lease.attempt_id)
                .bind(operation_uuid)
                .execute(&mut *transaction)
                .await?;
                if target == ExecutionState::Pending {
                    summary.reset_to_pending += 1;
                } else {
                    summary.marked_unknown += 1;
                }
            }
            sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id = $1")
                .bind(operation_uuid)
                .execute(&mut *transaction)
                .await?;
        }
        transaction.commit().await?;
        Ok(summary)
    }

    async fn lock_authority(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
    ) -> Result<AuthoritySnapshot, SchedulerError> {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT executor_kind, generation \
             FROM rust_controller.orchestration_authority \
             WHERE singleton_key = 1 FOR SHARE",
        )
        .fetch_optional(&mut **transaction)
        .await?;
        let snapshot = decode_authority(row)?;
        if snapshot.generation() != self.generation {
            return Err(SchedulerError::StaleAuthority {
                expected: snapshot.generation(),
                actual: self.generation,
            });
        }
        if snapshot.executor_kind() != self.executor_kind {
            return Err(SchedulerError::StaleExecutor {
                expected: snapshot.executor_kind(),
                actual: self.executor_kind,
            });
        }
        Ok(snapshot)
    }

    async fn lock_authority_for_update(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
    ) -> Result<AuthoritySnapshot, SchedulerError> {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT executor_kind, generation \
             FROM rust_controller.orchestration_authority \
             WHERE singleton_key = 1 FOR UPDATE",
        )
        .fetch_optional(&mut **transaction)
        .await?;
        let snapshot = decode_authority(row)?;
        if snapshot.generation() != self.generation {
            return Err(SchedulerError::StaleAuthority {
                expected: snapshot.generation(),
                actual: self.generation,
            });
        }
        if snapshot.executor_kind() != self.executor_kind {
            return Err(SchedulerError::StaleExecutor {
                expected: snapshot.executor_kind(),
                actual: self.executor_kind,
            });
        }
        Ok(snapshot)
    }

    fn validate_grant_owner(&self, grant: &LeaseGrant) -> Result<(), SchedulerError> {
        if grant.generation() != self.generation {
            return Err(SchedulerError::StaleAuthority {
                expected: self.generation,
                actual: grant.generation(),
            });
        }
        if grant.executor_kind() != self.executor_kind {
            return Err(SchedulerError::StaleExecutor {
                expected: self.executor_kind,
                actual: grant.executor_kind(),
            });
        }
        if grant.worker_id() != self.worker_id {
            return Err(SchedulerError::WorkerMismatch {
                expected: grant.worker_id().to_owned(),
                actual: self.worker_id.clone(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum SchedulerError {
    #[error("plan does not match the persisted operation command and schema")]
    PlanBindingMismatch,
    #[error("orchestration authority is missing")]
    MissingAuthority,
    #[error("stale authority generation: expected {expected}, actual {actual}")]
    StaleAuthority { expected: i64, actual: i64 },
    #[error("stale executor authority: expected {expected:?}, actual {actual:?}")]
    StaleExecutor {
        expected: ExecutorKind,
        actual: ExecutorKind,
    },
    #[error("lease worker mismatch: expected {expected}, actual {actual}")]
    WorkerMismatch { expected: String, actual: String },
    #[error("the lease acquisition token is stale")]
    StaleLeaseToken,
    #[error("claim cap must be greater than zero, got {cap}")]
    InvalidCap { cap: u32 },
    #[error("authority generation must be greater than zero, got {generation}")]
    InvalidGeneration { generation: i64 },
    #[error("worker id must not be blank")]
    InvalidWorkerId,
    #[error("authority change reference must not be blank")]
    InvalidChangeReference,
    #[error("leased work must cross the durable start boundary before finalization")]
    NotStarted,
    #[error("operation is not startable from {state:?}")]
    NotStartable { state: ExecutionState },
    #[error("operation is not finalizable from {state:?}")]
    NotFinalizable { state: ExecutionState },
    #[error("cancellation has been requested")]
    CancellationRequested,
    #[error("cancelling work may only finalize as unknown")]
    CancellationRequiresUnknown,
    #[error("operation revision cannot be incremented")]
    RevisionOverflow,
    #[error("lease is missing")]
    LeaseNotFound,
    #[error("lease has expired")]
    LeaseExpired,
    #[error("operation {0:?} does not exist")]
    OperationNotFound(OperationId),
    #[error("cancellation requires running or waiting state, got {state:?}")]
    CancellationNotRunning { state: ExecutionState },
    #[error("final state must be terminal, got {state:?}")]
    InvalidFinalState { state: ExecutionState },
    #[error("persisted executor kind is invalid: {value}")]
    InvalidPersistedExecutor { value: String },
    #[error("persisted execution state is invalid: {value}")]
    InvalidPersistedState { value: String },
    #[error("persisted scheduler identity is invalid: {kind}")]
    InvalidPersistedIdentity { kind: &'static str },
    #[error("invalid scheduler transition from {current:?} to {target:?}")]
    InvalidSchedulerTransition {
        current: ExecutionState,
        target: ExecutionState,
    },
    #[error(transparent)]
    Transition(#[from] TransitionError),
    #[error(transparent)]
    Event(#[from] EventValidationError),
    #[error(transparent)]
    Canonicalization(#[from] CanonicalizationError),
    #[error(transparent)]
    Serialization(#[from] serde_json::Error),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

struct PersistedLease {
    attempt_id: Uuid,
    executor_kind: String,
    generation: i64,
    worker_id: String,
    lease_token: String,
    active: bool,
    attempt_state: ExecutionState,
}

struct StateAppend {
    operation_id: OperationId,
    attempt_id: Option<AttemptId>,
    revision: i64,
    current: ExecutionState,
    target: ExecutionState,
    semantic_key: String,
    payload: serde_json::Value,
    observed_at: DateTime<Utc>,
    policy: TransitionPolicy,
}

#[derive(Clone, Copy)]
enum TransitionPolicy {
    Domain,
    #[cfg(feature = "fixture-ipc")]
    FixtureGraceWaiting,
    CredentialDeliveryReclaim,
    CredentialDeliveryResume,
    Cancellation,
    CancellationUnknown,
    ExpiredUnstarted,
    ExpiredAfterStart,
    NativeReconciliation,
}

async fn lock_operation(
    transaction: &mut Transaction<'_, Postgres>,
    operation_id: OperationId,
) -> Result<(ExecutionState, i64), SchedulerError> {
    let row: Option<(String, i64)> = sqlx::query_as(
        "SELECT state, revision FROM rust_controller.operations \
         WHERE operation_id = $1 FOR UPDATE",
    )
    .bind(operation_id.as_uuid())
    .fetch_optional(&mut **transaction)
    .await?;
    let (state, revision) = row.ok_or(SchedulerError::OperationNotFound(operation_id))?;
    Ok((decode_execution_state(&state)?, revision))
}

async fn lock_lease(
    transaction: &mut Transaction<'_, Postgres>,
    operation_id: OperationId,
) -> Result<PersistedLease, SchedulerError> {
    let row: Option<(Uuid, String, i64, String, String, bool, String)> = sqlx::query_as(
        "SELECT leases.attempt_id, leases.executor_kind, leases.generation, \
                leases.worker_id, leases.lease_token, \
                leases.lease_expires_at > clock_timestamp() \
                    AND leases.deadline_at > clock_timestamp(), attempts.state \
         FROM rust_controller.worker_leases leases \
         JOIN rust_controller.attempts attempts \
           ON attempts.attempt_id = leases.attempt_id \
          AND attempts.operation_id = leases.operation_id \
         WHERE leases.operation_id = $1 FOR UPDATE OF leases, attempts",
    )
    .bind(operation_id.as_uuid())
    .fetch_optional(&mut **transaction)
    .await?;
    let (attempt_id, executor_kind, generation, worker_id, lease_token, active, attempt_state) =
        row.ok_or(SchedulerError::LeaseNotFound)?;
    Ok(PersistedLease {
        attempt_id,
        executor_kind,
        generation,
        worker_id,
        lease_token,
        active,
        attempt_state: decode_execution_state(&attempt_state)?,
    })
}

fn validate_persisted_lease(
    grant: &LeaseGrant,
    persisted: &PersistedLease,
) -> Result<(), SchedulerError> {
    if persisted.lease_token != grant.lease_token().to_string() {
        return Err(SchedulerError::StaleLeaseToken);
    }
    if persisted.attempt_id != grant.attempt_id().as_uuid()
        || persisted.generation != grant.generation()
        || persisted.executor_kind != grant.executor_kind().as_str()
        || persisted.worker_id != grant.worker_id()
    {
        return Err(SchedulerError::StaleLeaseToken);
    }
    Ok(())
}

async fn append_attempt_started_event(
    transaction: &mut Transaction<'_, Postgres>,
    operation_id: OperationId,
    attempt_id: AttemptId,
    revision: i64,
    observed_at: DateTime<Utc>,
) -> Result<i64, SchedulerError> {
    let next_revision = revision
        .checked_add(1)
        .ok_or(SchedulerError::RevisionOverflow)?;
    let payload = json!({"phase": "mutation_started"});
    let event = JournalEvent::new(
        EventId::new(),
        operation_id,
        Some(attempt_id),
        next_revision,
        format!("scheduler:attempt-started:{}", attempt_id.as_uuid()),
        payload_digest(&payload)?,
        EventKind::AttemptStarted,
        payload,
        observed_at,
    )?;
    let outbox_payload = serde_json::to_value(&event)?;

    sqlx::query(
        "INSERT INTO rust_controller.journal_events \
         (event_id, operation_id, attempt_id, aggregate_revision, semantic_key, \
          payload_digest, event_kind, execution_state, payload, observed_at) \
         VALUES ($1, $2, $3, $4, $5, $6, 'attempt_started', NULL, $7, $8)",
    )
    .bind(event.event_id().as_uuid())
    .bind(event.operation_id().as_uuid())
    .bind(event.attempt_id().map(|id| id.as_uuid()))
    .bind(event.aggregate_revision())
    .bind(event.semantic_key())
    .bind(event.payload_digest())
    .bind(event.payload())
    .bind(event.observed_at())
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO rust_controller.outbox (event_id, operation_id, topic, payload) \
         VALUES ($1, $2, 'journal_event', $3)",
    )
    .bind(event.event_id().as_uuid())
    .bind(event.operation_id().as_uuid())
    .bind(outbox_payload)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "UPDATE rust_controller.operations \
         SET revision = $2, updated_at = clock_timestamp() WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .bind(next_revision)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "UPDATE rust_controller.operation_projection \
         SET revision = $2, last_event_id = $3, rebuilt_at = clock_timestamp() \
         WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .bind(next_revision)
    .bind(event.event_id().as_uuid())
    .execute(&mut **transaction)
    .await?;
    Ok(next_revision)
}

async fn append_state_event(
    transaction: &mut Transaction<'_, Postgres>,
    append: StateAppend,
) -> Result<i64, SchedulerError> {
    validate_transition(append.current, append.target, append.policy)?;
    persist_state_event(transaction, append).await
}

// Callers must validate their family-specific transition before reaching this
// persistence primitive. Existing domain/native policy checks remain above.
async fn persist_state_event(
    transaction: &mut Transaction<'_, Postgres>,
    append: StateAppend,
) -> Result<i64, SchedulerError> {
    let event_id = EventId::new();
    let next_revision = append
        .revision
        .checked_add(1)
        .ok_or(SchedulerError::RevisionOverflow)?;
    let event = JournalEvent::new(
        event_id,
        append.operation_id,
        append.attempt_id,
        next_revision,
        append.semantic_key,
        payload_digest(&append.payload)?,
        EventKind::ExecutionStateChanged(append.target),
        append.payload,
        append.observed_at,
    )?;
    let outbox_payload = serde_json::to_value(&event)?;

    sqlx::query(
        "INSERT INTO rust_controller.journal_events \
         (event_id, operation_id, attempt_id, aggregate_revision, semantic_key, \
          payload_digest, event_kind, execution_state, payload, observed_at) \
         VALUES ($1, $2, $3, $4, $5, $6, 'execution_state_changed', $7, $8, $9)",
    )
    .bind(event.event_id().as_uuid())
    .bind(event.operation_id().as_uuid())
    .bind(event.attempt_id().map(|id| id.as_uuid()))
    .bind(event.aggregate_revision())
    .bind(event.semantic_key())
    .bind(event.payload_digest())
    .bind(execution_state_name(append.target))
    .bind(event.payload())
    .bind(event.observed_at())
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO rust_controller.outbox (event_id, operation_id, topic, payload) \
         VALUES ($1, $2, 'journal_event', $3)",
    )
    .bind(event.event_id().as_uuid())
    .bind(event.operation_id().as_uuid())
    .bind(outbox_payload)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "UPDATE rust_controller.operations \
         SET state = $2, revision = $3, updated_at = clock_timestamp() \
         WHERE operation_id = $1",
    )
    .bind(event.operation_id().as_uuid())
    .bind(execution_state_name(append.target))
    .bind(next_revision)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO rust_controller.operation_projection \
         (operation_id, state, revision, last_event_id, rebuilt_at) \
         VALUES ($1, $2, $3, $4, clock_timestamp()) \
         ON CONFLICT (operation_id) DO UPDATE SET \
             state = EXCLUDED.state, revision = EXCLUDED.revision, \
             last_event_id = EXCLUDED.last_event_id, rebuilt_at = EXCLUDED.rebuilt_at",
    )
    .bind(event.operation_id().as_uuid())
    .bind(execution_state_name(append.target))
    .bind(next_revision)
    .bind(event.event_id().as_uuid())
    .execute(&mut **transaction)
    .await?;
    Ok(next_revision)
}

fn validate_transition(
    current: ExecutionState,
    target: ExecutionState,
    policy: TransitionPolicy,
) -> Result<(), SchedulerError> {
    let valid = match policy {
        #[cfg(feature = "fixture-ipc")]
        TransitionPolicy::FixtureGraceWaiting => {
            current == ExecutionState::Pending && target == ExecutionState::Waiting
        }
        TransitionPolicy::CredentialDeliveryReclaim => {
            current == ExecutionState::Running && target == ExecutionState::Pending
        }
        TransitionPolicy::CredentialDeliveryResume => {
            current == ExecutionState::Pending && target == ExecutionState::Running
        }
        TransitionPolicy::NativeReconciliation => {
            current == ExecutionState::Unknown
                && matches!(
                    target,
                    ExecutionState::Satisfied | ExecutionState::Failed | ExecutionState::Conflicted
                )
        }
        TransitionPolicy::Domain => {
            let signal = signal_for_target(target)
                .ok_or(SchedulerError::InvalidSchedulerTransition { current, target })?;
            decide_transition(current, signal)?.next == target
        }
        TransitionPolicy::Cancellation => {
            current == ExecutionState::Leased && target == ExecutionState::Cancelling
        }
        TransitionPolicy::CancellationUnknown => {
            current == ExecutionState::Cancelling && target == ExecutionState::Unknown
        }
        TransitionPolicy::ExpiredUnstarted => {
            current == ExecutionState::Leased && target == ExecutionState::Pending
        }
        TransitionPolicy::ExpiredAfterStart => {
            matches!(
                current,
                ExecutionState::Leased
                    | ExecutionState::Running
                    | ExecutionState::Waiting
                    | ExecutionState::Cancelling
            ) && target == ExecutionState::Unknown
        }
    };
    if valid {
        Ok(())
    } else {
        Err(SchedulerError::InvalidSchedulerTransition { current, target })
    }
}

const fn signal_for_target(target: ExecutionState) -> Option<DomainSignal> {
    match target {
        ExecutionState::Leased => Some(DomainSignal::Claimed),
        ExecutionState::Running => Some(DomainSignal::Started),
        ExecutionState::Waiting => Some(DomainSignal::WaitRequested),
        ExecutionState::Cancelling => Some(DomainSignal::CancellationRequested),
        ExecutionState::Satisfied => Some(DomainSignal::Satisfied),
        ExecutionState::Failed => Some(DomainSignal::Failed),
        ExecutionState::Blocked => Some(DomainSignal::Blocked),
        ExecutionState::Unknown => Some(DomainSignal::DeadlineElapsed),
        ExecutionState::Conflicted => Some(DomainSignal::ConflictDetected),
        ExecutionState::Pending => None,
    }
}

fn decode_authority(row: Option<(String, i64)>) -> Result<AuthoritySnapshot, SchedulerError> {
    let (executor, generation) = row.ok_or(SchedulerError::MissingAuthority)?;
    let executor_kind = ExecutorKind::from_persisted(&executor)
        .ok_or(SchedulerError::InvalidPersistedExecutor { value: executor })?;
    Ok(AuthoritySnapshot::new(executor_kind, generation))
}

fn decode_operation_id(value: Uuid) -> Result<OperationId, SchedulerError> {
    serde_json::from_value(serde_json::Value::String(value.to_string())).map_err(|_| {
        SchedulerError::InvalidPersistedIdentity {
            kind: "operation id",
        }
    })
}

fn decode_attempt_id(value: Uuid) -> Result<AttemptId, SchedulerError> {
    serde_json::from_value(serde_json::Value::String(value.to_string()))
        .map_err(|_| SchedulerError::InvalidPersistedIdentity { kind: "attempt id" })
}

const fn workflow_kind_name(kind: WorkflowKind) -> &'static str {
    match kind {
        WorkflowKind::CloudOsd => "cloud_osd",
        WorkflowKind::OsDeploy => "os_deploy",
        WorkflowKind::TaskSequence => "task_sequence",
        WorkflowKind::SyntheticLongSleep => "synthetic_long_sleep",
        WorkflowKind::NativePveVmBoot => "native_pve_vm_boot",
    }
}

const fn execution_state_name(state: ExecutionState) -> &'static str {
    match state {
        ExecutionState::Pending => "pending",
        ExecutionState::Leased => "leased",
        ExecutionState::Running => "running",
        ExecutionState::Waiting => "waiting",
        ExecutionState::Cancelling => "cancelling",
        ExecutionState::Satisfied => "satisfied",
        ExecutionState::Failed => "failed",
        ExecutionState::Blocked => "blocked",
        ExecutionState::Unknown => "unknown",
        ExecutionState::Conflicted => "conflicted",
    }
}

fn decode_execution_state(state: &str) -> Result<ExecutionState, SchedulerError> {
    match state {
        "pending" => Ok(ExecutionState::Pending),
        "leased" => Ok(ExecutionState::Leased),
        "running" => Ok(ExecutionState::Running),
        "waiting" => Ok(ExecutionState::Waiting),
        "cancelling" => Ok(ExecutionState::Cancelling),
        "satisfied" => Ok(ExecutionState::Satisfied),
        "failed" => Ok(ExecutionState::Failed),
        "blocked" => Ok(ExecutionState::Blocked),
        "unknown" => Ok(ExecutionState::Unknown),
        "conflicted" => Ok(ExecutionState::Conflicted),
        value => Err(SchedulerError::InvalidPersistedState {
            value: value.to_owned(),
        }),
    }
}

async fn reject_typed_dispatch(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<(), SchedulerError> {
    let native:bool=sqlx::query_scalar("SELECT workflow_kind IN ('native_pve_vm_boot','os_deploy') FROM rust_controller.operations WHERE operation_id=$1").bind(operation.as_uuid()).fetch_one(&mut **tx).await?;
    if native {
        Err(SchedulerError::PlanBindingMismatch)
    } else {
        Ok(())
    }
}
async fn is_osdeploy(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<bool, SchedulerError> {
    Ok(sqlx::query_scalar(
        "SELECT workflow_kind='os_deploy' FROM rust_controller.operations WHERE operation_id=$1",
    )
    .bind(operation.as_uuid())
    .fetch_one(&mut **tx)
    .await?)
}
async fn reject_osdeploy(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<(), SchedulerError> {
    if is_osdeploy(tx, operation).await? {
        Err(SchedulerError::PlanBindingMismatch)
    } else {
        Ok(())
    }
}
async fn lock_native_run_if_present(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<(), SchedulerError> {
    let run:Option<Uuid>=sqlx::query_scalar("SELECT run_id FROM rust_controller.operations WHERE operation_id=$1 AND workflow_kind='native_pve_vm_boot'").bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
    if let Some(run) = run {
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(format!("native:run:{run}"))
            .execute(&mut **tx)
            .await?;
        let cancelled: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM rust_controller.native_run_cancellations WHERE run_id=$1)",
        )
        .bind(run)
        .fetch_one(&mut **tx)
        .await?;
        if cancelled {
            return Err(SchedulerError::CancellationRequested);
        }
    }
    Ok(())
}
