use std::{
    collections::HashSet,
    process::{Command, Stdio},
    time::Duration,
};

use chrono::Utc;
use controller_domain::{
    CommandEnvelope, EventId, ExecutionState, OperationId, RunId, SemanticOperationKey,
    WorkflowKind,
};
use event_journal::{EventKind, JournalEvent, payload_digest};
use postgres_store::{CommandAppend, PgStore, StoreError};
use scheduler::{ExecutorKind, Scheduler, SchedulerError};
use sqlx::{PgPool, postgres::PgPoolOptions};

struct PostgresContainer {
    id: String,
}

#[tokio::test]
async fn semantic_plan_cannot_be_substituted_after_claim_or_by_concurrent_intake() {
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 1).await;
    let semantic = SemanticOperationKey::new(
        WorkflowKind::SyntheticLongSleep,
        RunId::new(),
        "immutable-plan",
        1,
    )
    .unwrap();
    let first = CommandEnvelope::new("plan-a", semantic.clone(), "a".repeat(64)).unwrap();
    let second = CommandEnvelope::new("plan-b", semantic.clone(), "b".repeat(64)).unwrap();
    let operation = OperationId::new();
    fixture
        .store
        .append_command(operation, &first)
        .await
        .unwrap();
    let scheduler = fixture.scheduler("binding-worker", ExecutorKind::Rust, 1);
    let grant = scheduler
        .claim_next_bound(WorkflowKind::SyntheticLongSleep, 1, 1, &"a".repeat(64))
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(
        fixture
            .store
            .append_command(OperationId::new(), &second)
            .await,
        Err(StoreError::CommandDigestConflict { existing_operation_id }) if existing_operation_id == operation
    ));
    assert!(matches!(
        scheduler
            .start_bound(&grant, WorkflowKind::SyntheticLongSleep, 1, &"b".repeat(64))
            .await,
        Err(SchedulerError::PlanBindingMismatch)
    ));
    scheduler
        .start_bound(&grant, WorkflowKind::SyntheticLongSleep, 1, &"a".repeat(64))
        .await
        .unwrap();
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Running
    );

    for equal in [true, false] {
        let semantic = SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "racing-plan",
            1,
        )
        .unwrap();
        let a = CommandEnvelope::new(format!("race-a-{equal}"), semantic.clone(), "a".repeat(64))
            .unwrap();
        let b = CommandEnvelope::new(
            format!("race-b-{equal}"),
            semantic,
            if equal { "a" } else { "b" }.repeat(64),
        )
        .unwrap();
        let results = tokio::join!(
            fixture.store.append_command(OperationId::new(), &a),
            fixture.store.append_command(OperationId::new(), &b)
        );
        assert_eq!(
            usize::from(results.0.is_ok()) + usize::from(results.1.is_ok()),
            if equal { 2 } else { 1 }
        );
        if equal {
            assert_eq!(results.0.unwrap(), results.1.unwrap());
        } else {
            let (
                Ok(CommandAppend::Appended(winner)),
                Err(StoreError::CommandDigestConflict {
                    existing_operation_id,
                }),
            ) = (if results.0.is_ok() {
                results
            } else {
                (results.1, results.0)
            })
            else {
                panic!("conflicting intake did not return the semantic conflict");
            };
            assert_eq!(winner, existing_operation_id);
        }
    }
    let counts: (i64, i64) = sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.operations), (SELECT count(*) FROM rust_controller.commands)").fetch_one(&fixture.pool).await.unwrap();
    assert_eq!(counts, (3, 4));
}

#[tokio::test]
async fn generic_append_cannot_start_or_finalize_after_authority_loss() {
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 1).await;
    let operation = fixture
        .create_operation("generic-bypass", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("old-worker", ExecutorKind::Rust, 1);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    fixture.set_authority(ExecutorKind::Python, 2).await;
    for kind in [
        EventKind::AttemptStarted,
        EventKind::ExecutionStateChanged(ExecutionState::Running),
        EventKind::ExecutionStateChanged(ExecutionState::Satisfied),
        EventKind::CommandAccepted,
        EventKind::DecisionRecorded,
    ] {
        let payload = serde_json::json!({"phase": "mutation_started"});
        let event = JournalEvent::new(
            EventId::new(),
            operation,
            Some(grant.attempt_id()),
            2,
            "forged-lifecycle",
            payload_digest(&payload).unwrap(),
            kind,
            payload,
            Utc::now(),
        )
        .unwrap();
        assert!(
            matches!(
                fixture.store.append_event(1, &event).await,
                Err(StoreError::SchedulerOwnedEvent)
            ),
            "unfenced lifecycle event accepted: {kind:?}"
        );
    }
    let state: (String, i64, String, i64, i64, i64) = sqlx::query_as("SELECT o.state, o.revision, a.state, (SELECT count(*) FROM rust_controller.journal_events), (SELECT count(*) FROM rust_controller.outbox), (SELECT count(*) FROM rust_controller.worker_leases) FROM rust_controller.operations o JOIN rust_controller.attempts a USING(operation_id)").fetch_one(&fixture.pool).await.unwrap();
    assert_eq!(state, ("leased".into(), 1, "leased".into(), 1, 1, 1));
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .revision(),
        1
    );
    // Observations remain admissible but cannot manufacture a scheduler start.
    let payload = serde_json::json!({"phase": "mutation_started", "state": "satisfied"});
    let event = JournalEvent::new(
        EventId::new(),
        operation,
        Some(grant.attempt_id()),
        2,
        "external-evidence",
        payload_digest(&payload).unwrap(),
        EventKind::EvidenceRecorded,
        payload,
        Utc::now(),
    )
    .unwrap();
    fixture.store.append_event(1, &event).await.unwrap();
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Leased
    );
    fixture.set_authority(ExecutorKind::Rust, 3).await;
    fixture.expire(operation).await;
    let current = fixture.scheduler("current-worker", ExecutorKind::Rust, 3);
    assert_eq!(current.reap_expired().await.unwrap().reset_to_pending(), 1);
    let grant = current
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    current.start(&grant).await.unwrap();
    let before = fixture
        .store
        .load_operation(operation)
        .await
        .unwrap()
        .unwrap();
    fixture.set_authority(ExecutorKind::Python, 4).await;
    let payload = serde_json::json!({"state": "satisfied"});
    let final_event = JournalEvent::new(
        EventId::new(),
        operation,
        Some(grant.attempt_id()),
        before.revision() + 1,
        "forged-final",
        payload_digest(&payload).unwrap(),
        EventKind::ExecutionStateChanged(ExecutionState::Satisfied),
        payload,
        Utc::now(),
    )
    .unwrap();
    assert!(
        fixture
            .store
            .append_event(before.revision(), &final_event)
            .await
            .is_err()
    );
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap(),
        before
    );
    let durable: (String, i64, i64, i64) = sqlx::query_as("SELECT state, (SELECT count(*) FROM rust_controller.worker_leases), (SELECT count(*) FROM rust_controller.journal_events), (SELECT count(*) FROM rust_controller.outbox) FROM rust_controller.attempts WHERE attempt_id = $1").bind(grant.attempt_id().as_uuid()).fetch_one(&fixture.pool).await.unwrap();
    assert_eq!(
        durable,
        ("running".into(), 1, before.revision(), before.revision())
    );
}

#[tokio::test]
async fn ambiguous_legacy_bindings_fail_closed_without_rewriting_history() {
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 1).await;
    let semantic = SemanticOperationKey::new(
        WorkflowKind::SyntheticLongSleep,
        RunId::new(),
        "legacy-ambiguous",
        1,
    )
    .unwrap();
    let a = CommandEnvelope::new("legacy-a", semantic.clone(), "a".repeat(64)).unwrap();
    let op = OperationId::new();
    fixture.store.append_command(op, &a).await.unwrap();
    let scheduler = fixture.scheduler("legacy-worker", ExecutorKind::Rust, 1);
    let grant = scheduler
        .claim_next_bound(WorkflowKind::SyntheticLongSleep, 2, 1, &"a".repeat(64))
        .await
        .unwrap()
        .unwrap();
    // Simulate rows admitted by the previous release, including a conflict
    // committed after claim. No supported current writer can create these.
    sqlx::query("INSERT INTO rust_controller.commands (idempotency_key, operation_id, payload_digest) VALUES ('legacy-b', $1, $2)").bind(op.as_uuid()).bind("b".repeat(64)).execute(&fixture.pool).await.unwrap();
    fixture.store.migrate().await.unwrap();
    assert!(matches!(
        scheduler
            .start_bound(&grant, WorkflowKind::SyntheticLongSleep, 1, &"a".repeat(64))
            .await,
        Err(SchedulerError::PlanBindingMismatch)
    ));
    assert!(matches!(
        scheduler.start(&grant).await,
        Err(SchedulerError::PlanBindingMismatch)
    ));
    assert!(
        fixture
            .store
            .append_command(OperationId::new(), &a)
            .await
            .is_err()
    );
    let new_key = CommandEnvelope::new("legacy-c", semantic, "a".repeat(64)).unwrap();
    assert!(
        fixture
            .store
            .append_command(OperationId::new(), &new_key)
            .await
            .is_err()
    );
    fixture.expire(op).await;
    scheduler.reap_expired().await.unwrap();
    assert!(
        scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 2)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        scheduler
            .claim_next_bound(WorkflowKind::SyntheticLongSleep, 2, 1, &"a".repeat(64))
            .await
            .unwrap()
            .is_none()
    );
    let digests: Vec<String> = sqlx::query_scalar(
        "SELECT payload_digest FROM rust_controller.commands ORDER BY payload_digest",
    )
    .fetch_all(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(digests, vec!["a".repeat(64), "b".repeat(64)]);
    let started: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.journal_events WHERE event_kind='attempt_started'",
    )
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(started, 0);
    // An orphan legacy operation must not acquire a new plan implicitly either.
    sqlx::query("DELETE FROM rust_controller.commands WHERE operation_id=$1")
        .bind(op.as_uuid())
        .execute(&fixture.pool)
        .await
        .unwrap();
    assert!(matches!(
        fixture.store.append_command(OperationId::new(), &a).await,
        Err(StoreError::AmbiguousCommandBinding)
    ));
    assert!(
        scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 2)
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn health_snapshot_reads_counts_and_authority_without_write_privilege() {
    let fixture = Fixture::new().await;
    assert!(
        fixture
            .store
            .health_snapshot()
            .await
            .unwrap()
            .authority
            .is_none()
    );
    fixture.set_authority(ExecutorKind::Rust, 1).await;
    let pending = fixture
        .create_operation("pending-health", WorkflowKind::SyntheticLongSleep)
        .await;
    fixture
        .create_operation("leased-health", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("health-worker", ExecutorKind::Rust, 1);
    scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    sqlx::query("UPDATE rust_controller.operations SET created_at=clock_timestamp()-interval '60 seconds' WHERE state='pending'").execute(&fixture.pool).await.unwrap();
    sqlx::raw_sql("CREATE ROLE health_reader LOGIN PASSWORD 'local-health'; GRANT USAGE ON SCHEMA rust_controller TO health_reader; GRANT SELECT ON ALL TABLES IN SCHEMA rust_controller TO health_reader; ALTER ROLE health_reader SET default_transaction_read_only=on;").execute(&fixture.pool).await.unwrap();
    let options = fixture
        .pool
        .connect_options()
        .as_ref()
        .clone()
        .username("health_reader")
        .password("local-health");
    let reader = PgPoolOptions::new().connect_with(options).await.unwrap();
    let snapshot = PgStore::new(reader).health_snapshot().await.unwrap();
    assert_eq!(snapshot.authority.unwrap().generation(), 1);
    assert_eq!(snapshot.active_leases, 1);
    assert!(snapshot.oldest_pending_age_seconds.unwrap() >= 60);
    assert_eq!(snapshot.outbox_pending, 1);
    assert_eq!(
        (snapshot.blocked, snapshot.unknown, snapshot.conflicted),
        (0, 0, 0)
    );
    assert!(
        fixture
            .store
            .load_operation(pending)
            .await
            .unwrap()
            .is_some()
    );
    fixture.pool.close().await;
    assert!(fixture.store.health_snapshot().await.is_err());
}

#[tokio::test]
async fn bound_claim_skips_unrelated_fingerprints_and_contract_versions() {
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 1).await;
    let unrelated = fixture
        .create_operation("unrelated", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("bound-worker", ExecutorKind::Rust, 1);
    assert!(
        scheduler
            .claim_next_bound(WorkflowKind::SyntheticLongSleep, 1, 1, &"b".repeat(64))
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        scheduler
            .claim_next_bound(WorkflowKind::SyntheticLongSleep, 1, 2, &"a".repeat(64))
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        fixture
            .store
            .load_operation(unrelated)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Pending
    );
    assert_eq!(
        scheduler
            .claim_next_bound(WorkflowKind::SyntheticLongSleep, 1, 1, &"a".repeat(64))
            .await
            .unwrap()
            .unwrap()
            .operation_id(),
        unrelated
    );
}

impl PostgresContainer {
    async fn start() -> Self {
        let output = Command::new("docker")
            .args([
                "run",
                "--pull=never",
                "--detach",
                "--env",
                "POSTGRES_PASSWORD=postgres",
                "--env",
                "POSTGRES_DB=rust_controller_test",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ])
            .stderr(Stdio::inherit())
            .output()
            .expect("docker must be installed for PostgreSQL integration tests");
        assert!(
            output.status.success(),
            "failed to start PostgreSQL container"
        );

        let id = String::from_utf8(output.stdout)
            .expect("docker container id must be UTF-8")
            .trim()
            .to_owned();
        let container = Self { id };
        container.wait_for_pool().await;
        container
    }

    fn dsn(&self) -> String {
        let output = Command::new("docker")
            .args([
                "inspect",
                "--format",
                "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
                &self.id,
            ])
            .output()
            .expect("docker inspect must run");
        assert!(output.status.success(), "failed to inspect PostgreSQL port");
        let port = String::from_utf8(output.stdout)
            .expect("published Docker port must be UTF-8")
            .trim()
            .to_owned();
        format!("postgresql://postgres:postgres@127.0.0.1:{port}/rust_controller_test")
    }

    async fn wait_for_pool(&self) -> PgPool {
        let dsn = self.dsn();
        for _ in 0..60 {
            if let Ok(pool) = PgPoolOptions::new().max_connections(20).connect(&dsn).await {
                return pool;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        panic!("PostgreSQL did not become ready within 15 seconds");
    }
}

impl Drop for PostgresContainer {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "--force", &self.id])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

struct Fixture {
    _postgres: PostgresContainer,
    pool: PgPool,
    store: PgStore,
}

impl Fixture {
    async fn new() -> Self {
        let postgres = PostgresContainer::start().await;
        let pool = postgres.wait_for_pool().await;
        let store = PgStore::new(pool.clone());
        store.migrate().await.unwrap();
        Self {
            _postgres: postgres,
            pool,
            store,
        }
    }

    async fn set_authority(&self, executor: ExecutorKind, generation: i64) {
        sqlx::query(
            "INSERT INTO rust_controller.orchestration_authority \
             (singleton_key, executor_kind, generation, change_reference) \
             VALUES (1, $1, $2, 'scheduler-test') \
             ON CONFLICT (singleton_key) DO UPDATE SET \
                 executor_kind = EXCLUDED.executor_kind, \
                 generation = EXCLUDED.generation, \
                 changed_at = clock_timestamp(), \
                 change_reference = EXCLUDED.change_reference",
        )
        .bind(executor.as_str())
        .bind(generation)
        .execute(&self.pool)
        .await
        .unwrap();
    }

    fn scheduler(&self, worker: &str, executor: ExecutorKind, generation: i64) -> Scheduler {
        Scheduler::new(self.store.clone(), executor, generation, worker).unwrap()
    }

    async fn create_operation(&self, key: &str, kind: WorkflowKind) -> OperationId {
        let operation_id = OperationId::new();
        let command = CommandEnvelope::new(
            format!("command:{key}"),
            SemanticOperationKey::new(kind, RunId::new(), format!("operation:{key}"), 1).unwrap(),
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        assert_eq!(
            self.store
                .append_command(operation_id, &command)
                .await
                .unwrap(),
            CommandAppend::Appended(operation_id)
        );
        operation_id
    }

    async fn expire(&self, operation_id: OperationId) {
        sqlx::query(
            "UPDATE rust_controller.worker_leases \
             SET lease_expires_at = clock_timestamp() - interval '1 second', \
                 heartbeat_at = LEAST(heartbeat_at, clock_timestamp() - interval '2 seconds'), \
                 acquired_at = LEAST(acquired_at, clock_timestamp() - interval '3 seconds') \
             WHERE operation_id = $1",
        )
        .bind(operation_id.as_uuid())
        .execute(&self.pool)
        .await
        .unwrap();
    }
}

#[tokio::test]
async fn missing_authority_fails_closed_before_claim() {
    // Break caught: a missing singleton must not silently make Rust authoritative.
    let fixture = Fixture::new().await;
    fixture
        .create_operation("missing-authority", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);

    assert!(matches!(
        scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 1)
            .await,
        Err(SchedulerError::MissingAuthority)
    ));
}

#[tokio::test]
async fn stale_generation_cannot_heartbeat_or_finalize_after_immediate_authority_flip() {
    // Break caught: cached authority lets an old executor renew or commit a terminal result.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("stale-authority", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();

    fixture.set_authority(ExecutorKind::Python, 8).await;

    assert!(matches!(
        scheduler.heartbeat(&grant).await,
        Err(SchedulerError::StaleAuthority {
            expected: 8,
            actual: 7
        })
    ));
    assert!(matches!(
        scheduler.finalize(&grant, ExecutionState::Satisfied).await,
        Err(SchedulerError::StaleAuthority {
            expected: 8,
            actual: 7
        })
    ));
    let state: String =
        sqlx::query_scalar("SELECT state FROM rust_controller.operations WHERE operation_id = $1")
            .bind(operation_id.as_uuid())
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(state, "leased");
}

#[tokio::test]
async fn worker_mismatch_and_stale_token_cannot_continue_a_lease() {
    // Break caught: checking only operation identity lets another worker or an old capability renew work.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("lease-binding", WorkflowKind::SyntheticLongSleep)
        .await;
    let owner = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let other = fixture.scheduler("worker-b", ExecutorKind::Rust, 7);
    let first = owner
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();

    assert!(matches!(
        other.heartbeat(&first).await,
        Err(SchedulerError::WorkerMismatch { .. })
    ));

    fixture.expire(operation_id).await;
    assert_eq!(owner.reap_expired().await.unwrap().reset_to_pending(), 1);
    let second = other
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    assert_ne!(first.lease_token(), second.lease_token());
    assert_eq!(first.lease_token().get_version_num(), 7);
    assert_eq!(second.lease_token().get_version_num(), 7);
    assert!(matches!(
        owner.heartbeat(&first).await,
        Err(SchedulerError::StaleLeaseToken)
    ));
}

#[tokio::test]
async fn per_kind_cap_is_enforced_without_blocking_another_kind() {
    // Break caught: a global or racy cap can overclaim one kind or starve an unrelated kind.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    fixture
        .create_operation("synthetic-one", WorkflowKind::SyntheticLongSleep)
        .await;
    fixture
        .create_operation("synthetic-two", WorkflowKind::SyntheticLongSleep)
        .await;
    fixture
        .create_operation("osdeploy-one", WorkflowKind::OsDeploy)
        .await;
    let first = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let second = fixture.scheduler("worker-b", ExecutorKind::Rust, 7);

    assert!(
        first
            .claim_next(WorkflowKind::SyntheticLongSleep, 1)
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        second
            .claim_next(WorkflowKind::SyntheticLongSleep, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        second
            .claim_next(WorkflowKind::OsDeploy, 1)
            .await
            .unwrap()
            .is_some()
    );
    assert!(matches!(
        second.claim_next(WorkflowKind::OsDeploy, 0).await,
        Err(SchedulerError::InvalidCap { cap: 0 })
    ));
}

#[tokio::test]
async fn claim_skips_a_locked_oldest_operation() {
    // Break caught: a plain FOR UPDATE claim blocks the scheduler behind one busy row.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let oldest = fixture
        .create_operation("locked-oldest", WorkflowKind::SyntheticLongSleep)
        .await;
    let next = fixture
        .create_operation("unlocked-next", WorkflowKind::SyntheticLongSleep)
        .await;
    sqlx::query(
        "UPDATE rust_controller.operations SET created_at = CASE \
           WHEN operation_id = $1 THEN clock_timestamp() - interval '2 minutes' \
           ELSE clock_timestamp() - interval '1 minute' END \
         WHERE operation_id IN ($1, $2)",
    )
    .bind(oldest.as_uuid())
    .bind(next.as_uuid())
    .execute(&fixture.pool)
    .await
    .unwrap();
    let mut lock = fixture.pool.begin().await.unwrap();
    sqlx::query(
        "SELECT operation_id FROM rust_controller.operations \
         WHERE operation_id = $1 FOR UPDATE",
    )
    .bind(oldest.as_uuid())
    .execute(&mut *lock)
    .await
    .unwrap();

    let grant = fixture
        .scheduler("worker-a", ExecutorKind::Rust, 7)
        .claim_next(WorkflowKind::SyntheticLongSleep, 2)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(grant.operation_id(), next);
    lock.rollback().await.unwrap();
}

#[tokio::test]
async fn ten_concurrent_claimers_respect_cap_and_one_row_ownership() {
    // Break caught: cap checks outside the claim transaction let concurrent workers overbook.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    for index in 0..10 {
        fixture
            .create_operation(
                &format!("concurrent-{index}"),
                WorkflowKind::SyntheticLongSleep,
            )
            .await;
    }

    let mut tasks = tokio::task::JoinSet::new();
    for index in 0..10 {
        let scheduler = fixture.scheduler(&format!("worker-{index}"), ExecutorKind::Rust, 7);
        tasks.spawn(async move {
            scheduler
                .claim_next(WorkflowKind::SyntheticLongSleep, 3)
                .await
        });
    }
    let mut grants = Vec::new();
    while let Some(result) = tasks.join_next().await {
        if let Some(grant) = result.unwrap().unwrap() {
            grants.push(grant);
        }
    }

    assert_eq!(grants.len(), 3);
    assert_eq!(
        grants
            .iter()
            .map(|grant| grant.operation_id())
            .collect::<HashSet<_>>()
            .len(),
        3
    );
    assert_eq!(
        grants
            .iter()
            .map(|grant| grant.lease_token())
            .collect::<HashSet<_>>()
            .len(),
        3
    );
    let active: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.worker_leases \
         WHERE lease_expires_at > clock_timestamp()",
    )
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(active, 3);
}

#[tokio::test]
async fn running_cancellation_becomes_cancelling_atomically() {
    // Break caught: cancellation that only marks a lease leaves operation and projection disagreeing.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("cancel-running", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler.start(&grant).await.unwrap();

    assert_eq!(
        scheduler.request_cancel(operation_id).await.unwrap(),
        ExecutionState::Cancelling
    );
    let row: (String, i64, String, i64, i64) = sqlx::query_as(
        "SELECT o.state, o.revision, p.state, p.revision, \
                (SELECT count(*) FROM rust_controller.outbox WHERE operation_id = o.operation_id) \
         FROM rust_controller.operations o \
         JOIN rust_controller.operation_projection p USING (operation_id) \
         WHERE o.operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(
        row,
        ("cancelling".to_owned(), 4, "cancelling".to_owned(), 4, 4)
    );
}

#[tokio::test]
async fn expired_running_work_becomes_unknown() {
    // Break caught: timeout must not imply failure, success, or automatic retry permission.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("expired-running", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler.start(&grant).await.unwrap();
    fixture.expire(operation_id).await;

    let summary = scheduler.reap_expired().await.unwrap();
    assert_eq!(summary.marked_unknown(), 1);
    assert_eq!(summary.reset_to_pending(), 0);
    let row: (String, String, i64) = sqlx::query_as(
        "SELECT o.state, p.state, count(l.operation_id) \
         FROM rust_controller.operations o \
         JOIN rust_controller.operation_projection p USING (operation_id) \
         LEFT JOIN rust_controller.worker_leases l USING (operation_id) \
         WHERE o.operation_id = $1 GROUP BY o.state, p.state",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(row, ("unknown".to_owned(), "unknown".to_owned(), 0));
}

#[tokio::test]
async fn expired_unstarted_lease_returns_to_pending_and_can_be_reacquired() {
    // Break caught: harmless pre-start worker loss must not strand pending work forever.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("expired-unstarted", WorkflowKind::SyntheticLongSleep)
        .await;
    let first_scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let first = first_scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    fixture.expire(operation_id).await;

    let summary = first_scheduler.reap_expired().await.unwrap();
    assert_eq!(summary.reset_to_pending(), 1);
    assert_eq!(summary.marked_unknown(), 0);
    let second = fixture
        .scheduler("worker-b", ExecutorKind::Rust, 7)
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(second.operation_id(), operation_id);
    assert_ne!(second.attempt_id(), first.attempt_id());
    assert_ne!(second.lease_token(), first.lease_token());
}

#[tokio::test]
async fn mutation_start_evidence_blocks_pending_reset_even_if_state_still_says_leased() {
    // Break caught: a crashed worker can mutate before the running projection is committed.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("started-before-state", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler.start(&grant).await.unwrap();
    // Simulate an inconsistent legacy snapshot with a durable start but stale
    // state. Supported lifecycle writes now commit both atomically.
    sqlx::raw_sql("UPDATE rust_controller.operations SET state = 'leased'; UPDATE rust_controller.attempts SET state = 'leased'; UPDATE rust_controller.operation_projection SET state = 'leased';")
        .execute(&fixture.pool).await.unwrap();
    fixture.expire(operation_id).await;

    let summary = scheduler.reap_expired().await.unwrap();
    assert_eq!(summary.marked_unknown(), 1);
    assert_eq!(summary.reset_to_pending(), 0);
    let state: String =
        sqlx::query_scalar("SELECT state FROM rust_controller.operations WHERE operation_id = $1")
            .bind(operation_id.as_uuid())
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(state, "unknown");
}

#[tokio::test]
async fn missing_authority_fences_every_existing_lease_operation() {
    // Break caught: checking authority only at claim lets work continue after the singleton disappears.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("authority-deleted", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    fixture.expire(operation_id).await;
    // Simulate catastrophic administrator corruption while retaining the
    // fail-closed scheduler proof; normal SQL deletion is tested as forbidden.
    sqlx::query(
        "ALTER TABLE rust_controller.orchestration_authority \
         DISABLE TRIGGER trg_orchestration_authority_no_delete",
    )
    .execute(&fixture.pool)
    .await
    .unwrap();
    sqlx::query("DELETE FROM rust_controller.orchestration_authority")
        .execute(&fixture.pool)
        .await
        .unwrap();
    sqlx::query(
        "ALTER TABLE rust_controller.orchestration_authority \
         ENABLE TRIGGER trg_orchestration_authority_no_delete",
    )
    .execute(&fixture.pool)
    .await
    .unwrap();

    assert!(matches!(
        scheduler.heartbeat(&grant).await,
        Err(SchedulerError::MissingAuthority)
    ));
    assert!(matches!(
        scheduler.finalize(&grant, ExecutionState::Satisfied).await,
        Err(SchedulerError::MissingAuthority)
    ));
    assert!(matches!(
        scheduler.request_cancel(operation_id).await,
        Err(SchedulerError::MissingAuthority)
    ));
    assert!(matches!(
        scheduler.reap_expired().await,
        Err(SchedulerError::MissingAuthority)
    ));
}

#[tokio::test]
async fn claim_heartbeat_and_finalize_keep_database_timing_and_durable_state_atomic() {
    // Break caught: a happy-path worker can return success while its lease,
    // attempt, journal, projection, or outbox remains stale or application-timed.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("database-clock-finalize", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let before: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.pool)
        .await
        .unwrap();

    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    let after_claim: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.pool)
        .await
        .unwrap();
    assert!(*grant.acquired_at() >= before && *grant.acquired_at() <= after_claim);
    assert!(*grant.heartbeat_at() >= before && *grant.heartbeat_at() <= after_claim);
    let lease_ttl = *grant.lease_expires_at() - *grant.acquired_at();
    assert!(lease_ttl >= chrono::Duration::seconds(29));
    assert!(lease_ttl <= chrono::Duration::seconds(31));
    let persisted: (uuid::Uuid, String, i64, String, String) = sqlx::query_as(
        "SELECT attempt_id, executor_kind, generation, worker_id, lease_token \
         FROM rust_controller.worker_leases WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(persisted.0, grant.attempt_id().as_uuid());
    assert_eq!(persisted.1, "rust");
    assert_eq!(persisted.2, 7);
    assert_eq!(persisted.3, "worker-a");
    assert_eq!(persisted.4, grant.lease_token().to_string());
    let debug = format!("{grant:?}");
    assert!(!debug.contains(&grant.lease_token().to_string()));
    assert!(debug.contains("<redacted>"));

    assert_eq!(
        scheduler.start(&grant).await.unwrap(),
        ExecutionState::Running
    );
    let renewed = scheduler.heartbeat(&grant).await.unwrap();
    assert!(*renewed.heartbeat_at() >= *grant.heartbeat_at());
    assert!(*renewed.lease_expires_at() >= *grant.lease_expires_at());
    assert_eq!(
        scheduler
            .finalize(&renewed, ExecutionState::Satisfied)
            .await
            .unwrap(),
        ExecutionState::Satisfied
    );
    let completed_at: chrono::DateTime<Utc> = sqlx::query_scalar(
        "SELECT completed_at FROM rust_controller.attempts WHERE attempt_id = $1",
    )
    .bind(grant.attempt_id().as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    let after_finalize: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&fixture.pool)
        .await
        .unwrap();
    assert!(completed_at >= after_claim && completed_at <= after_finalize);
    let durable: (String, i64, String, i64, String, i64, i64, i64) = sqlx::query_as(
        "SELECT o.state, o.revision, p.state, p.revision, a.state, \
                (SELECT count(*) FROM rust_controller.worker_leases \
                 WHERE operation_id = o.operation_id), \
                (SELECT count(*) FROM rust_controller.journal_events \
                 WHERE operation_id = o.operation_id), \
                (SELECT count(*) FROM rust_controller.outbox \
                 WHERE operation_id = o.operation_id) \
         FROM rust_controller.operations o \
         JOIN rust_controller.operation_projection p USING (operation_id) \
         JOIN rust_controller.attempts a USING (operation_id) \
         WHERE o.operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(
        durable,
        (
            "satisfied".to_owned(),
            4,
            "satisfied".to_owned(),
            4,
            "satisfied".to_owned(),
            0,
            4,
            4,
        )
    );
}

#[tokio::test]
async fn start_atomically_records_mutation_boundary_before_execution() {
    // Break caught: an adapter can mutate while the durable operation is still
    // safely reapable as an unstarted lease.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("atomic-start", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(
        scheduler.start(&grant).await.unwrap(),
        ExecutionState::Running
    );
    let durable: (String, i64, String, i64, String, i64, i64) = sqlx::query_as(
        "SELECT o.state, o.revision, p.state, p.revision, a.state, \
                (SELECT count(*) FROM rust_controller.journal_events \
                 WHERE operation_id = o.operation_id AND event_kind = 'attempt_started'), \
                (SELECT count(*) FROM rust_controller.outbox \
                 WHERE operation_id = o.operation_id) \
         FROM rust_controller.operations o \
         JOIN rust_controller.operation_projection p USING (operation_id) \
         JOIN rust_controller.attempts a USING (operation_id) \
         WHERE o.operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(
        durable,
        (
            "running".to_owned(),
            3,
            "running".to_owned(),
            3,
            "running".to_owned(),
            1,
            3,
        )
    );
    assert_eq!(
        scheduler.continuation(&grant).await.unwrap(),
        ExecutionState::Running
    );
}

#[tokio::test]
async fn waiting_attempt_can_continue_only_with_its_live_capability() {
    // Break caught: continuation support that only recognizes running work can
    // strand a mutation after it durably enters a wait/retry boundary.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("waiting-continuation", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler.start(&grant).await.unwrap();

    // Historical waiting state remains readable and fenced. The foundation
    // currently has no public transition into waiting.
    sqlx::raw_sql("UPDATE rust_controller.operations SET state = 'waiting'; UPDATE rust_controller.attempts SET state = 'waiting'; UPDATE rust_controller.operation_projection SET state = 'waiting';")
        .execute(&fixture.pool).await.unwrap();

    assert_eq!(
        fixture
            .store
            .load_operation(operation_id)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Waiting
    );

    assert_eq!(
        scheduler.continuation(&grant).await.unwrap(),
        ExecutionState::Waiting
    );
    assert_eq!(
        scheduler.heartbeat(&grant).await.unwrap().attempt_id(),
        grant.attempt_id()
    );
}

#[tokio::test]
async fn leased_work_cannot_finalize_before_atomic_start() {
    // Break caught: accepting terminal state directly from leased lets a worker
    // bypass the only durable proof that mutation was authorized to begin.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("leased-finalize", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();

    assert!(matches!(
        scheduler.finalize(&grant, ExecutionState::Satisfied).await,
        Err(SchedulerError::NotStarted)
    ));
    let state: String =
        sqlx::query_scalar("SELECT state FROM rust_controller.operations WHERE operation_id = $1")
            .bind(operation_id.as_uuid())
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(state, "leased");
}

#[tokio::test]
async fn cancellation_fences_start_heartbeat_continuation_and_terminal_result() {
    // Break caught: a cancellation flag that does not gate capability checks
    // permits another mutation or lease renewal after the operator cancels.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("cancel-fence", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    let heartbeat_before: chrono::DateTime<Utc> = sqlx::query_scalar(
        "SELECT heartbeat_at FROM rust_controller.worker_leases WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();

    assert_eq!(
        scheduler.request_cancel(operation_id).await.unwrap(),
        ExecutionState::Cancelling
    );
    assert!(matches!(
        scheduler.start(&grant).await,
        Err(SchedulerError::CancellationRequested)
    ));
    assert!(matches!(
        scheduler.heartbeat(&grant).await,
        Err(SchedulerError::CancellationRequested)
    ));
    assert!(matches!(
        scheduler.continuation(&grant).await,
        Err(SchedulerError::CancellationRequested)
    ));
    assert!(matches!(
        scheduler.finalize(&grant, ExecutionState::Satisfied).await,
        Err(SchedulerError::CancellationRequiresUnknown)
    ));
    let heartbeat_after: chrono::DateTime<Utc> = sqlx::query_scalar(
        "SELECT heartbeat_at FROM rust_controller.worker_leases WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(heartbeat_after, heartbeat_before);
    assert_eq!(
        scheduler
            .finalize(&grant, ExecutionState::Unknown)
            .await
            .unwrap(),
        ExecutionState::Unknown
    );
}

#[tokio::test]
async fn authority_generation_cannot_aba_back_to_an_old_grant() {
    // Break caught: a caller-selected authority number can recycle generation
    // seven and make an old capability appear current again.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    fixture
        .create_operation("authority-aba", WorkflowKind::SyntheticLongSleep)
        .await;
    let old = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = old
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();

    let python = old
        .transition_authority(ExecutorKind::Python, "test:rust-to-python")
        .await
        .unwrap();
    assert_eq!(python.generation(), 8);
    let rust = fixture
        .scheduler("authority-worker", ExecutorKind::Python, 8)
        .transition_authority(ExecutorKind::Rust, "test:python-to-rust")
        .await
        .unwrap();
    assert_eq!(rust.generation(), 9);
    let aba = sqlx::query(
        "UPDATE rust_controller.orchestration_authority \
         SET generation = 7, changed_at = clock_timestamp(), change_reference = 'test:aba' \
         WHERE singleton_key = 1",
    )
    .execute(&fixture.pool)
    .await;
    assert!(aba.is_err());
    assert!(matches!(
        old.heartbeat(&grant).await,
        Err(SchedulerError::StaleAuthority {
            expected: 9,
            actual: 7
        })
    ));
}

#[tokio::test]
async fn authority_cannot_be_deleted_and_rebootstrapped_to_reauthorize_an_old_grant() {
    // Break caught: update-only monotonicity can be bypassed by deleting the
    // singleton and inserting the old executor/generation again.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    fixture
        .create_operation("authority-delete-aba", WorkflowKind::SyntheticLongSleep)
        .await;
    let old = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = old
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    old.start(&grant).await.unwrap();
    old.transition_authority(ExecutorKind::Python, "test:delete-aba-cutover")
        .await
        .unwrap();

    let deletion =
        sqlx::query("DELETE FROM rust_controller.orchestration_authority WHERE singleton_key = 1")
            .execute(&fixture.pool)
            .await;
    let rebootstrap = sqlx::query(
        "INSERT INTO rust_controller.orchestration_authority \
         (singleton_key, executor_kind, generation, change_reference) \
         VALUES (1, 'rust', 7, 'test:delete-aba-rebootstrap')",
    )
    .execute(&fixture.pool)
    .await;

    let heartbeat = old.heartbeat(&grant).await;
    let finalization = old.finalize(&grant, ExecutionState::Satisfied).await;
    assert!(matches!(
        heartbeat,
        Err(SchedulerError::StaleAuthority {
            expected: 8,
            actual: 7
        })
    ));
    assert!(matches!(
        finalization,
        Err(SchedulerError::StaleAuthority {
            expected: 8,
            actual: 7
        })
    ));
    assert!(deletion.is_err());
    assert!(rebootstrap.is_err());
}

#[tokio::test]
async fn concurrent_authority_transitions_serialize_with_one_domain_stale_loser() {
    // Break caught: two shared-lock readers can both try to upgrade the same
    // authority row and make PostgreSQL choose a raw deadlock loser.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let dsn = fixture._postgres.dsn();
    let left = Scheduler::new(
        PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        ),
        ExecutorKind::Rust,
        7,
        "authority-left",
    )
    .unwrap();
    let right = Scheduler::new(
        PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        ),
        ExecutorKind::Rust,
        7,
        "authority-right",
    )
    .unwrap();

    let mut shared_blocker = fixture.pool.begin().await.unwrap();
    sqlx::query(
        "SELECT singleton_key FROM rust_controller.orchestration_authority \
         WHERE singleton_key = 1 FOR SHARE",
    )
    .execute(&mut *shared_blocker)
    .await
    .unwrap();
    let left_task = tokio::spawn(async move {
        left.transition_authority(ExecutorKind::Python, "test:concurrent-left")
            .await
    });
    let right_task = tokio::spawn(async move {
        right
            .transition_authority(ExecutorKind::Python, "test:concurrent-right")
            .await
    });
    tokio::time::sleep(Duration::from_millis(200)).await;
    shared_blocker.commit().await.unwrap();

    let (left_result, right_result) = tokio::time::timeout(Duration::from_secs(5), async {
        (left_task.await.unwrap(), right_task.await.unwrap())
    })
    .await
    .expect("authority transitions must serialize without deadlock timeout");
    let results = [left_result, right_result];
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(
                result,
                Err(SchedulerError::StaleAuthority {
                    expected: 8,
                    actual: 7
                })
            ))
            .count(),
        1
    );
}

#[tokio::test]
async fn concurrent_reapers_serialize_without_double_transition_or_error() {
    // Break caught: two reapers can select one expired lease before either
    // locks its operation, causing the loser to fail after the winner deletes it.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("reaper-race", WorkflowKind::SyntheticLongSleep)
        .await;
    fixture
        .scheduler("worker-a", ExecutorKind::Rust, 7)
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    fixture.expire(operation_id).await;
    let left = fixture.scheduler("reaper-left", ExecutorKind::Rust, 7);
    let right = fixture.scheduler("reaper-right", ExecutorKind::Rust, 7);

    let (left_result, right_result) = tokio::join!(left.reap_expired(), right.reap_expired());
    let summaries = [left_result.unwrap(), right_result.unwrap()];
    assert_eq!(
        summaries
            .iter()
            .map(|summary| summary.reset_to_pending())
            .sum::<u64>(),
        1
    );
    let event_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.journal_events \
         WHERE operation_id = $1 AND semantic_key LIKE 'scheduler:lease-expired:%'",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(event_count, 1);
}

#[tokio::test]
async fn one_reaper_sweep_is_bounded() {
    // Break caught: an unbounded expiry sweep can hold authority and operation
    // locks for every stale row in the database.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    for index in 0..33 {
        fixture
            .create_operation(
                &format!("bounded-reap-{index}"),
                WorkflowKind::SyntheticLongSleep,
            )
            .await;
        scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 33)
            .await
            .unwrap()
            .unwrap();
    }
    sqlx::query(
        "UPDATE rust_controller.worker_leases \
         SET acquired_at = clock_timestamp() - interval '3 seconds', \
             heartbeat_at = clock_timestamp() - interval '2 seconds', \
             lease_expires_at = clock_timestamp() - interval '1 second'",
    )
    .execute(&fixture.pool)
    .await
    .unwrap();

    assert_eq!(
        scheduler.reap_expired().await.unwrap().reset_to_pending(),
        32
    );
    assert_eq!(
        scheduler.reap_expired().await.unwrap().reset_to_pending(),
        1
    );
}

#[tokio::test]
async fn cancellation_revision_overflow_is_a_controlled_error() {
    // Break caught: formatting revision + 1 panics in debug builds at the
    // persisted bigint boundary instead of rolling back with a typed error.
    let fixture = Fixture::new().await;
    fixture.set_authority(ExecutorKind::Rust, 7).await;
    let operation_id = fixture
        .create_operation("cancel-overflow", WorkflowKind::SyntheticLongSleep)
        .await;
    let scheduler = fixture.scheduler("worker-a", ExecutorKind::Rust, 7);
    let grant = scheduler
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler.start(&grant).await.unwrap();
    sqlx::query("UPDATE rust_controller.operations SET revision = $2 WHERE operation_id = $1")
        .bind(operation_id.as_uuid())
        .bind(i64::MAX)
        .execute(&fixture.pool)
        .await
        .unwrap();
    sqlx::query(
        "UPDATE rust_controller.operation_projection \
         SET revision = $2 WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .bind(i64::MAX)
    .execute(&fixture.pool)
    .await
    .unwrap();

    assert!(matches!(
        scheduler.request_cancel(operation_id).await,
        Err(SchedulerError::RevisionOverflow)
    ));
}
