use sqlx::PgPool;
use thiserror::Error;

use chrono::{DateTime, Utc};
use controller_domain::{
    CommandEnvelope, EventId, ExecutionState, OperationId, TransitionError, WorkflowKind,
};
use event_journal::{EventKind, JournalEvent};
use uuid::Uuid;

type ClaimedOutboxRow = (
    i64,
    Uuid,
    Uuid,
    String,
    serde_json::Value,
    DateTime<Utc>,
    DateTime<Utc>,
    Uuid,
);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommandAppend {
    Appended(OperationId),
    AlreadyPresent(OperationId),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationProjection {
    operation_id: OperationId,
    state: ExecutionState,
    revision: i64,
    last_event_id: Option<EventId>,
}

impl OperationProjection {
    #[must_use]
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }

    #[must_use]
    pub const fn state(&self) -> ExecutionState {
        self.state
    }

    #[must_use]
    pub const fn revision(&self) -> i64 {
        self.revision
    }

    #[must_use]
    pub const fn last_event_id(&self) -> Option<EventId> {
        self.last_event_id
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct OutboxMessage {
    outbox_id: i64,
    event_id: EventId,
    operation_id: OperationId,
    topic: String,
    payload: serde_json::Value,
    claimed_at: DateTime<Utc>,
    claim_expires_at: DateTime<Utc>,
    claim_token: Uuid,
}

impl OutboxMessage {
    #[must_use]
    pub const fn outbox_id(&self) -> i64 {
        self.outbox_id
    }

    #[must_use]
    pub const fn event_id(&self) -> EventId {
        self.event_id
    }

    #[must_use]
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }

    #[must_use]
    pub fn topic(&self) -> &str {
        &self.topic
    }

    #[must_use]
    pub const fn payload(&self) -> &serde_json::Value {
        &self.payload
    }

    #[must_use]
    pub const fn claimed_at(&self) -> &DateTime<Utc> {
        &self.claimed_at
    }

    #[must_use]
    pub const fn claim_expires_at(&self) -> &DateTime<Utc> {
        &self.claim_expires_at
    }

    #[must_use]
    pub const fn claim_token(&self) -> Uuid {
        self.claim_token
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum EventAppend {
    Appended(OperationProjection),
    AlreadyPresent(EventId),
}

#[derive(Clone, Debug)]
pub struct PgStore {
    pool: PgPool,
}

impl PgStore {
    #[must_use]
    pub const fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    pub(crate) const fn pool(&self) -> &PgPool {
        &self.pool
    }

    pub async fn migrate(&self) -> Result<(), StoreError> {
        sqlx::raw_sql(include_str!("../migrations/0001_foundation.sql"))
            .execute(&self.pool)
            .await?;
        sqlx::raw_sql(include_str!("../migrations/0002_scheduler.sql"))
            .execute(&self.pool)
            .await?;
        sqlx::raw_sql(include_str!("../migrations/0003_native_pve.sql"))
            .execute(&self.pool)
            .await?;
        sqlx::raw_sql(include_str!("../migrations/0004_osdeploy_registration.sql"))
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn append_command(
        &self,
        operation_id: OperationId,
        command: &CommandEnvelope,
    ) -> Result<CommandAppend, StoreError> {
        // Preserve the public boundary's validation order even when storage
        // is unavailable; the private helper also validates native intake.
        validate_digest(command.payload_digest())?;
        i16::try_from(command.semantic_key().contract_version())
            .map_err(|_| StoreError::ContractVersionOutOfRange)?;
        if command.semantic_key().workflow_kind() == WorkflowKind::OsDeploy {
            return Err(StoreError::TypedWorkflowRequired);
        }
        let mut transaction = self.pool.begin().await?;
        // Share typed registration's run lock before any command/operation lock.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(format!(
                "native:run:{}",
                command.semantic_key().run_id().as_uuid()
            ))
            .execute(&mut *transaction)
            .await?;
        let typed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.operations WHERE run_id=$1 AND workflow_kind='os_deploy')")
            .bind(command.semantic_key().run_id().as_uuid()).fetch_one(&mut *transaction).await?;
        if typed {
            return Err(StoreError::TypedWorkflowRequired);
        }
        let result = Self::append_command_tx(&mut transaction, operation_id, command).await?;
        transaction.commit().await?;
        Ok(result)
    }

    pub(crate) async fn append_command_tx(
        transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        operation_id: OperationId,
        command: &CommandEnvelope,
    ) -> Result<CommandAppend, StoreError> {
        validate_digest(command.payload_digest())?;
        let contract_version = i16::try_from(command.semantic_key().contract_version())
            .map_err(|_| StoreError::ContractVersionOutOfRange)?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(command.idempotency_key())
            .execute(&mut **transaction)
            .await?;

        let existing: Option<(Uuid, String)> = sqlx::query_as(
            "SELECT operation_id, payload_digest FROM rust_controller.commands \
             WHERE idempotency_key = $1",
        )
        .bind(command.idempotency_key())
        .fetch_optional(&mut **transaction)
        .await?;
        if let Some((existing_id, existing_digest)) = existing {
            let existing_operation_id = decode_operation_id(existing_id)?;
            if existing_digest == command.payload_digest() {
                sqlx::query("SELECT operation_id FROM rust_controller.operations WHERE operation_id = $1 FOR UPDATE")
                    .bind(existing_id).fetch_one(&mut **transaction).await?;
                require_command_binding(
                    transaction,
                    existing_operation_id,
                    command.payload_digest(),
                )
                .await?;
                return Ok(CommandAppend::AlreadyPresent(existing_operation_id));
            }
            return Err(StoreError::CommandDigestConflict {
                existing_operation_id,
            });
        }

        let semantic = command.semantic_key();
        let inserted_operation_id: Option<Uuid> = sqlx::query_scalar(
            "INSERT INTO rust_controller.operations \
             (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
             VALUES ($1, $2, $3, $4, $5, 'pending', 0) \
             ON CONFLICT (workflow_kind, run_id, operation_key, contract_version) DO NOTHING \
             RETURNING operation_id",
        )
        .bind(operation_id.as_uuid())
        .bind(workflow_kind_name(semantic.workflow_kind()))
        .bind(semantic.run_id().as_uuid())
        .bind(semantic.operation_key())
        .bind(contract_version)
        .fetch_optional(&mut **transaction)
        .await?;

        let (persisted_id, inserted_operation) = if let Some(inserted) = inserted_operation_id {
            (inserted, true)
        } else {
            (
                sqlx::query_scalar(
                    "SELECT operation_id FROM rust_controller.operations \
                     WHERE workflow_kind = $1 AND run_id = $2 AND operation_key = $3 \
                       AND contract_version = $4 FOR UPDATE",
                )
                .bind(workflow_kind_name(semantic.workflow_kind()))
                .bind(semantic.run_id().as_uuid())
                .bind(semantic.operation_key())
                .bind(contract_version)
                .fetch_one(&mut **transaction)
                .await?,
                false,
            )
        };

        if !inserted_operation {
            require_command_binding(
                transaction,
                decode_operation_id(persisted_id)?,
                command.payload_digest(),
            )
            .await?;
        }

        sqlx::query(
            "INSERT INTO rust_controller.commands \
             (idempotency_key, operation_id, payload_digest) VALUES ($1, $2, $3)",
        )
        .bind(command.idempotency_key())
        .bind(persisted_id)
        .bind(command.payload_digest())
        .execute(&mut **transaction)
        .await?;
        if inserted_operation {
            sqlx::query(
                "INSERT INTO rust_controller.operation_projection (operation_id, state, revision) \
                 VALUES ($1, 'pending', 0)",
            )
            .bind(persisted_id)
            .execute(&mut **transaction)
            .await?;
        }

        Ok(CommandAppend::Appended(decode_operation_id(persisted_id)?))
    }

    pub async fn append_event(
        &self,
        expected_revision: i64,
        event: &JournalEvent,
    ) -> Result<EventAppend, StoreError> {
        // Only observations may arrive without a scheduler capability. In
        // particular AttemptStarted drives recovery policy even without a state.
        if event.kind() != EventKind::EvidenceRecorded {
            return Err(StoreError::SchedulerOwnedEvent);
        }
        validate_digest(event.payload_digest())?;
        if expected_revision < 0 {
            return Err(StoreError::InvalidExpectedRevision { expected_revision });
        }
        let required_event_revision = expected_revision
            .checked_add(1)
            .ok_or(StoreError::InvalidExpectedRevision { expected_revision })?;
        if event.aggregate_revision() != required_event_revision {
            return Err(StoreError::EventRevisionMismatch {
                expected: required_event_revision,
                actual: event.aggregate_revision(),
            });
        }

        let mut transaction = self.pool.begin().await?;
        let locked: Option<(String, i64)> = sqlx::query_as(
            "SELECT state, revision FROM rust_controller.operations \
             WHERE operation_id = $1 FOR UPDATE",
        )
        .bind(event.operation_id().as_uuid())
        .fetch_optional(&mut *transaction)
        .await?;
        let (current_state_name, actual_revision) =
            locked.ok_or(StoreError::OperationNotFound(event.operation_id()))?;

        let existing: Option<(Uuid, String)> = sqlx::query_as(
            "SELECT event_id, payload_digest FROM rust_controller.journal_events \
             WHERE operation_id = $1 AND semantic_key = $2",
        )
        .bind(event.operation_id().as_uuid())
        .bind(event.semantic_key())
        .fetch_optional(&mut *transaction)
        .await?;
        if let Some((existing_id, existing_digest)) = existing {
            let existing_event_id = decode_event_id(existing_id)?;
            if existing_digest == event.payload_digest() {
                transaction.commit().await?;
                return Ok(EventAppend::AlreadyPresent(existing_event_id));
            }
            return Err(StoreError::EventDigestConflict { existing_event_id });
        }
        if actual_revision != expected_revision {
            return Err(StoreError::RevisionConflict {
                expected: expected_revision,
                actual: actual_revision,
            });
        }

        let next_state = decode_execution_state(&current_state_name)?;
        let outbox_payload = serde_json::to_value(event)?;

        sqlx::query(
            "INSERT INTO rust_controller.journal_events \
             (event_id, operation_id, attempt_id, aggregate_revision, semantic_key, \
              payload_digest, event_kind, execution_state, payload, observed_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        )
        .bind(event.event_id().as_uuid())
        .bind(event.operation_id().as_uuid())
        .bind(event.attempt_id().map(|id| id.as_uuid()))
        .bind(event.aggregate_revision())
        .bind(event.semantic_key())
        .bind(event.payload_digest())
        .bind("evidence_recorded")
        .bind(None::<String>)
        .bind(event.payload())
        .bind(event.observed_at())
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "INSERT INTO rust_controller.outbox (event_id, operation_id, topic, payload) \
             VALUES ($1, $2, 'journal_event', $3)",
        )
        .bind(event.event_id().as_uuid())
        .bind(event.operation_id().as_uuid())
        .bind(outbox_payload)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE rust_controller.operations \
             SET state = $2, revision = $3, updated_at = clock_timestamp() \
             WHERE operation_id = $1",
        )
        .bind(event.operation_id().as_uuid())
        .bind(execution_state_name(next_state))
        .bind(event.aggregate_revision())
        .execute(&mut *transaction)
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
        .bind(execution_state_name(next_state))
        .bind(event.aggregate_revision())
        .bind(event.event_id().as_uuid())
        .execute(&mut *transaction)
        .await?;

        let projection = OperationProjection {
            operation_id: event.operation_id(),
            state: next_state,
            revision: event.aggregate_revision(),
            last_event_id: Some(event.event_id()),
        };
        transaction.commit().await?;
        Ok(EventAppend::Appended(projection))
    }

    pub async fn load_operation(
        &self,
        operation_id: OperationId,
    ) -> Result<Option<OperationProjection>, StoreError> {
        let row: Option<(Uuid, String, i64, Option<Uuid>)> = sqlx::query_as(
            "SELECT operation_id, state, revision, last_event_id \
             FROM rust_controller.operation_projection WHERE operation_id = $1",
        )
        .bind(operation_id.as_uuid())
        .fetch_optional(&self.pool)
        .await?;
        row.map(decode_projection).transpose()
    }

    pub async fn rebuild_projection(
        &self,
        operation_id: OperationId,
    ) -> Result<OperationProjection, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let authoritative: Option<(String, i64)> = sqlx::query_as(
            "SELECT state, revision FROM rust_controller.operations \
             WHERE operation_id = $1 FOR UPDATE",
        )
        .bind(operation_id.as_uuid())
        .fetch_optional(&mut *transaction)
        .await?;
        let (authoritative_state, authoritative_revision) =
            authoritative.ok_or(StoreError::OperationNotFound(operation_id))?;
        let events: Vec<(i64, String, Option<String>, Uuid)> = sqlx::query_as(
            "SELECT aggregate_revision, event_kind, execution_state, event_id \
             FROM rust_controller.journal_events WHERE operation_id = $1 \
             ORDER BY aggregate_revision",
        )
        .bind(operation_id.as_uuid())
        .fetch_all(&mut *transaction)
        .await?;

        let mut state = ExecutionState::Pending;
        let mut revision = 0_i64;
        let mut last_event_id = None;
        for (event_revision, event_kind, event_state, event_id) in events {
            let wanted = revision + 1;
            if event_revision != wanted {
                return Err(StoreError::JournalRevisionGap {
                    expected: wanted,
                    actual: event_revision,
                });
            }
            if event_kind == "execution_state_changed" {
                state = decode_execution_state(event_state.as_deref().ok_or(
                    StoreError::PersistedState {
                        value: "missing event execution state".to_owned(),
                    },
                )?)?;
            }
            revision = event_revision;
            last_event_id = Some(decode_event_id(event_id)?);
        }
        let decoded_authoritative_state = decode_execution_state(&authoritative_state)?;
        if revision != authoritative_revision || state != decoded_authoritative_state {
            return Err(StoreError::ProjectionSourceMismatch {
                journal_state: state,
                journal_revision: revision,
                operation_state: decoded_authoritative_state,
                operation_revision: authoritative_revision,
            });
        }

        sqlx::query(
            "INSERT INTO rust_controller.operation_projection \
             (operation_id, state, revision, last_event_id, rebuilt_at) \
             VALUES ($1, $2, $3, $4, clock_timestamp()) \
             ON CONFLICT (operation_id) DO UPDATE SET \
                 state = EXCLUDED.state, revision = EXCLUDED.revision, \
                 last_event_id = EXCLUDED.last_event_id, rebuilt_at = EXCLUDED.rebuilt_at",
        )
        .bind(operation_id.as_uuid())
        .bind(execution_state_name(state))
        .bind(revision)
        .bind(last_event_id.map(|id| id.as_uuid()))
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;

        Ok(OperationProjection {
            operation_id,
            state,
            revision,
            last_event_id,
        })
    }

    pub async fn dequeue_outbox(&self, limit: i64) -> Result<Vec<OutboxMessage>, StoreError> {
        if !(1..=1000).contains(&limit) {
            return Err(StoreError::InvalidOutboxLimit { limit });
        }
        let claim_token = Uuid::now_v7();
        let rows: Vec<ClaimedOutboxRow> = sqlx::query_as(
            "WITH selected AS ( \
                     SELECT outbox_id FROM rust_controller.outbox \
                     WHERE delivered_at IS NULL \
                       AND (claim_expires_at IS NULL OR \
                            claim_expires_at <= clock_timestamp()) \
                       AND available_at <= clock_timestamp() \
                     ORDER BY outbox_id FOR UPDATE SKIP LOCKED LIMIT $1 \
                 ) \
                 UPDATE rust_controller.outbox AS outbox \
                 SET claimed_at = clock_timestamp(), \
                     claim_expires_at = clock_timestamp() + interval '30 seconds', \
                     claim_token = $2, \
                     attempt_count = outbox.attempt_count + 1 \
                 FROM selected WHERE outbox.outbox_id = selected.outbox_id \
                 RETURNING outbox.outbox_id, outbox.event_id, outbox.operation_id, \
                           outbox.topic, outbox.payload, outbox.claimed_at, \
                           outbox.claim_expires_at, outbox.claim_token",
        )
        .bind(limit)
        .bind(claim_token)
        .fetch_all(&self.pool)
        .await?;

        rows.into_iter()
            .map(
                |(
                    outbox_id,
                    event_id,
                    operation_id,
                    topic,
                    payload,
                    claimed_at,
                    claim_expires_at,
                    claim_token,
                )| {
                    Ok(OutboxMessage {
                        outbox_id,
                        event_id: decode_event_id(event_id)?,
                        operation_id: decode_operation_id(operation_id)?,
                        topic,
                        payload,
                        claimed_at,
                        claim_expires_at,
                        claim_token,
                    })
                },
            )
            .collect()
    }

    pub async fn ack_outbox(&self, outbox_id: i64, claim_token: Uuid) -> Result<bool, StoreError> {
        if outbox_id <= 0 || claim_token.is_nil() {
            return Ok(false);
        }
        let acknowledged: Option<i64> = sqlx::query_scalar(
            "UPDATE rust_controller.outbox \
             SET delivered_at = clock_timestamp() \
             WHERE outbox_id = $1 AND claim_token = $2 AND delivered_at IS NULL \
               AND claim_expires_at > clock_timestamp() \
             RETURNING outbox_id",
        )
        .bind(outbox_id)
        .bind(claim_token)
        .fetch_optional(&self.pool)
        .await?;
        Ok(acknowledged.is_some())
    }

    pub async fn release_outbox(
        &self,
        outbox_id: i64,
        claim_token: Uuid,
    ) -> Result<bool, StoreError> {
        if outbox_id <= 0 || claim_token.is_nil() {
            return Ok(false);
        }
        let released: Option<i64> = sqlx::query_scalar(
            "UPDATE rust_controller.outbox \
             SET claimed_at = NULL, claim_expires_at = NULL, claim_token = NULL, \
                 available_at = clock_timestamp() \
             WHERE outbox_id = $1 AND claim_token = $2 AND delivered_at IS NULL \
               AND claim_expires_at > clock_timestamp() \
             RETURNING outbox_id",
        )
        .bind(outbox_id)
        .bind(claim_token)
        .fetch_optional(&self.pool)
        .await?;
        Ok(released.is_some())
    }
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("workflow requires typed registration")]
    TypedWorkflowRequired,
    #[error("execution lifecycle events require a fenced scheduler capability")]
    SchedulerOwnedEvent,
    #[error("semantic operation has no unambiguous persisted command binding")]
    AmbiguousCommandBinding,
    #[error("command or semantic operation already exists with a different digest")]
    CommandDigestConflict { existing_operation_id: OperationId },
    #[error("event semantic key already exists with a different digest")]
    EventDigestConflict { existing_event_id: EventId },
    #[error("operation revision conflict: expected {expected}, actual {actual}")]
    RevisionConflict { expected: i64, actual: i64 },
    #[error("expected revision must be a nonnegative value with room for the next event")]
    InvalidExpectedRevision { expected_revision: i64 },
    #[error("event aggregate revision mismatch: expected {expected}, actual {actual}")]
    EventRevisionMismatch { expected: i64, actual: i64 },
    #[error("event state target {target:?} does not match domain transition result {actual:?}")]
    StateTargetMismatch {
        target: ExecutionState,
        actual: ExecutionState,
    },
    #[error(transparent)]
    InvalidStateTransition(#[from] TransitionError),
    #[error("operation {0:?} does not exist")]
    OperationNotFound(OperationId),
    #[error("journal revision gap: expected {expected}, actual {actual}")]
    JournalRevisionGap { expected: i64, actual: i64 },
    #[error("persisted execution state is invalid: {value}")]
    PersistedState { value: String },
    #[error(
        "journal projection source does not match operation row: journal {journal_state:?}/{journal_revision}, operation {operation_state:?}/{operation_revision}"
    )]
    ProjectionSourceMismatch {
        journal_state: ExecutionState,
        journal_revision: i64,
        operation_state: ExecutionState,
        operation_revision: i64,
    },
    #[error("outbox dequeue limit {limit} is outside 1..=1000")]
    InvalidOutboxLimit { limit: i64 },
    #[error("contract version exceeds the PostgreSQL smallint boundary")]
    ContractVersionOutOfRange,
    #[error("payload digest must be a lowercase SHA-256 hexadecimal string")]
    InvalidPayloadDigest,
    #[error("persisted {kind} failed domain validation")]
    PersistedIdentity { kind: &'static str },
    #[error(transparent)]
    Serialization(#[from] serde_json::Error),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

// The caller holds the operation row lock. All supported intake serializes on
// it, so the first persisted digest is immutable across idempotency keys. Older
// databases with conflicting commands are rejected rather than choosing one.
async fn require_command_binding(
    transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    operation_id: OperationId,
    fingerprint: &str,
) -> Result<(), StoreError> {
    let (distinct, digest): (i64, Option<String>) = sqlx::query_as(
        "SELECT count(DISTINCT payload_digest), min(payload_digest) FROM rust_controller.commands WHERE operation_id = $1",
    ).bind(operation_id.as_uuid()).fetch_one(&mut **transaction).await?;
    if distinct != 1 {
        return Err(StoreError::AmbiguousCommandBinding);
    }
    if digest.as_deref() != Some(fingerprint) {
        return Err(StoreError::CommandDigestConflict {
            existing_operation_id: operation_id,
        });
    }
    Ok(())
}

fn validate_digest(digest: &str) -> Result<(), StoreError> {
    if digest.len() == 64
        && digest
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return Ok(());
    }
    Err(StoreError::InvalidPayloadDigest)
}

fn decode_operation_id(id: Uuid) -> Result<OperationId, StoreError> {
    serde_json::from_value(serde_json::Value::String(id.to_string())).map_err(|_| {
        StoreError::PersistedIdentity {
            kind: "operation id",
        }
    })
}

fn decode_event_id(id: Uuid) -> Result<EventId, StoreError> {
    serde_json::from_value(serde_json::Value::String(id.to_string()))
        .map_err(|_| StoreError::PersistedIdentity { kind: "event id" })
}

fn decode_projection(
    (operation_id, state, revision, last_event_id): (Uuid, String, i64, Option<Uuid>),
) -> Result<OperationProjection, StoreError> {
    Ok(OperationProjection {
        operation_id: decode_operation_id(operation_id)?,
        state: decode_execution_state(&state)?,
        revision,
        last_event_id: last_event_id.map(decode_event_id).transpose()?,
    })
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

fn decode_execution_state(state: &str) -> Result<ExecutionState, StoreError> {
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
        value => Err(StoreError::PersistedState {
            value: value.to_owned(),
        }),
    }
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
