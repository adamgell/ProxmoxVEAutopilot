use std::{
    process::{Command, Stdio},
    time::Duration,
};

use chrono::Utc;
use controller_domain::{
    AttemptId, CommandEnvelope, EventId, ExecutionState, OperationId, RunId, SemanticOperationKey,
    WorkflowKind,
};
use event_journal::{EventKind, JournalEvent, payload_digest};
use postgres_store::{CommandAppend, EventAppend, PgStore, StoreError};
use sqlx::{PgPool, postgres::PgPoolOptions};

const EXPECTED_TABLES: [&str; 8] = [
    "attempts",
    "commands",
    "journal_events",
    "operation_projection",
    "operations",
    "orchestration_authority",
    "outbox",
    "worker_leases",
];

struct PostgresContainer {
    id: String,
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
            if let Ok(pool) = PgPoolOptions::new().max_connections(4).connect(&dsn).await {
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

fn command(key: &str) -> CommandEnvelope {
    CommandEnvelope::new(
        format!("command:{key}"),
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            format!("synthetic:{key}"),
            1,
        )
        .unwrap(),
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    .unwrap()
}

async fn create_operation(store: &PgStore, key: &str) -> OperationId {
    let operation_id = OperationId::new();
    assert_eq!(
        store
            .append_command(operation_id, &command(key))
            .await
            .unwrap(),
        CommandAppend::Appended(operation_id)
    );
    operation_id
}

fn journal_event(
    operation_id: OperationId,
    revision: i64,
    semantic_key: &str,
    kind: EventKind,
    payload: serde_json::Value,
) -> JournalEvent {
    JournalEvent::new(
        EventId::new(),
        operation_id,
        None,
        revision,
        semantic_key,
        payload_digest(&payload).unwrap(),
        kind,
        payload,
        Utc::now(),
    )
    .unwrap()
}

#[tokio::test]
async fn migration_creates_constrained_foundation_tables() {
    // Break caught: omitting the additive Rust-controller migration leaves the
    // isolated database without its authoritative persistence boundary.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    PgStore::new(pool.clone())
        .migrate()
        .await
        .expect("foundation migration must apply");

    let tables: Vec<String> = sqlx::query_scalar(
        "SELECT table_name FROM information_schema.tables \
         WHERE table_schema = 'rust_controller' ORDER BY table_name",
    )
    .fetch_all(&pool)
    .await
    .expect("foundation table inventory must be queryable");

    assert_eq!(tables, EXPECTED_TABLES);
}

#[tokio::test]
async fn schema_enforces_operation_identity_state_and_revision_invariants() {
    // Break caught: a loose schema could accept an unknown state, an empty
    // semantic identity, or a negative aggregate revision.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    PgStore::new(pool.clone())
        .migrate()
        .await
        .expect("foundation migration must apply");

    sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:one', 1, 'pending', 0)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000001")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await
    .expect("a valid operation must be accepted");

    let invalid_state = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:two', 1, 'done', 0)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000002")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await;
    assert!(invalid_state.is_err());

    let empty_key = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, '  ', 1, 'pending', 0)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000003")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await;
    assert!(empty_key.is_err());

    let negative_revision = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:three', 1, 'pending', -1)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000004")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await;
    assert!(negative_revision.is_err());

    let non_v7_operation_id = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:old-id', 1, 'pending', 0)",
    )
    .bind("550e8400-e29b-41d4-a716-446655440001")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await;
    assert!(non_v7_operation_id.is_err());

    let nil_run_id = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:nil-run', 1, 'pending', 0)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000006")
    .bind("00000000-0000-0000-0000-000000000000")
    .execute(&pool)
    .await;
    assert!(nil_run_id.is_err());

    let duplicate_semantic_identity = sqlx::query(
        "INSERT INTO rust_controller.operations \
         (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
         VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, 'sleep:one', 1, 'pending', 0)",
    )
    .bind("018f0f7e-7b7a-7cc0-8c1e-000000000005")
    .bind("550e8400-e29b-41d4-a716-446655440000")
    .execute(&pool)
    .await;
    assert!(duplicate_semantic_identity.is_err());
}

#[tokio::test]
async fn schema_enforces_journal_outbox_projection_authority_and_lease_invariants() {
    // Break caught: unconstrained child tables could admit malformed digests,
    // cross-operation attempts, duplicate semantic events, or client-timed leases.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    PgStore::new(pool.clone())
        .migrate()
        .await
        .expect("foundation migration must apply");

    let operation_id = "018f0f7e-7b7a-7cc0-8c1e-000000000011";
    let other_operation_id = "018f0f7e-7b7a-7cc0-8c1e-000000000012";
    for (id, key) in [
        (operation_id, "sleep:journal"),
        (other_operation_id, "sleep:other"),
    ] {
        sqlx::query(
            "INSERT INTO rust_controller.operations \
             (operation_id, workflow_kind, run_id, operation_key, contract_version, state, revision) \
             VALUES ($1::uuid, 'synthetic_long_sleep', $2::uuid, $3, 1, 'pending', 0)",
        )
        .bind(id)
        .bind("550e8400-e29b-41d4-a716-446655440011")
        .bind(key)
        .execute(&pool)
        .await
        .expect("valid operation must insert");
    }

    let digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    sqlx::query(
        "INSERT INTO rust_controller.commands (idempotency_key, operation_id, payload_digest) \
         VALUES ('command:one', $1::uuid, $2)",
    )
    .bind(operation_id)
    .bind(digest)
    .execute(&pool)
    .await
    .expect("valid command must insert");
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.commands (idempotency_key, operation_id, payload_digest) \
             VALUES ('command:bad-digest', $1::uuid, 'NOT-SHA256')",
        )
        .bind(operation_id)
        .execute(&pool)
        .await
        .is_err()
    );
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.attempts \
             (attempt_id, operation_id, attempt_number, state) \
             VALUES ($1::uuid, $2::uuid, 2, 'running')",
        )
        .bind("550e8400-e29b-41d4-a716-446655440021")
        .bind(operation_id)
        .execute(&pool)
        .await
        .is_err()
    );

    let attempt_id = "018f0f7e-7b7a-7cc0-8c1e-000000000021";
    let (attempt_started_at, attempt_deadline_at): (
        chrono::DateTime<chrono::Utc>,
        chrono::DateTime<chrono::Utc>,
    ) = sqlx::query_as(
        "INSERT INTO rust_controller.attempts \
             (attempt_id, operation_id, attempt_number, state) \
             VALUES ($1::uuid, $2::uuid, 1, 'running') \
             RETURNING started_at, deadline_at",
    )
    .bind(attempt_id)
    .bind(operation_id)
    .fetch_one(&pool)
    .await
    .expect("valid attempt must insert");
    assert!(attempt_deadline_at > attempt_started_at);
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.attempts \
             (attempt_id, operation_id, attempt_number, state) \
             VALUES ($1::uuid, $2::uuid, 1, 'running')",
        )
        .bind("018f0f7e-7b7a-7cc0-8c1e-000000000022")
        .bind(operation_id)
        .execute(&pool)
        .await
        .is_err()
    );
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.journal_events \
             (event_id, operation_id, aggregate_revision, semantic_key, payload_digest, \
              event_kind, execution_state, payload, observed_at) \
             VALUES ($1::uuid, $2::uuid, 2, 'invalid-event-id', $3, \
                     'execution_state_changed', 'running', '{}'::jsonb, clock_timestamp())",
        )
        .bind("550e8400-e29b-41d4-a716-446655440031")
        .bind(operation_id)
        .bind(digest)
        .execute(&pool)
        .await
        .is_err()
    );
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.journal_events \
             (event_id, operation_id, aggregate_revision, semantic_key, payload_digest, \
              event_kind, execution_state, payload, observed_at) \
             VALUES ($1::uuid, $2::uuid, 2, 'invalid-kind-state-pair', $3, \
                     'evidence_recorded', 'running', '{}'::jsonb, clock_timestamp())",
        )
        .bind("018f0f7e-7b7a-7cc0-8c1e-000000000034")
        .bind(operation_id)
        .bind(digest)
        .execute(&pool)
        .await
        .is_err()
    );

    let event_id = "018f0f7e-7b7a-7cc0-8c1e-000000000031";
    sqlx::query(
        "INSERT INTO rust_controller.journal_events \
         (event_id, operation_id, attempt_id, aggregate_revision, semantic_key, \
          payload_digest, event_kind, execution_state, payload, observed_at) \
         VALUES ($1::uuid, $2::uuid, $3::uuid, 1, 'state:running', $4, \
                 'execution_state_changed', 'running', '{\"state\":\"running\"}'::jsonb, clock_timestamp())",
    )
    .bind(event_id)
    .bind(operation_id)
    .bind(attempt_id)
    .bind(digest)
    .execute(&pool)
    .await
    .expect("valid event must insert");
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.journal_events \
             (event_id, operation_id, aggregate_revision, semantic_key, payload_digest, \
              event_kind, payload, observed_at) \
             VALUES ($1::uuid, $2::uuid, 2, 'state:running', $3, \
                     'evidence_recorded', '{}'::jsonb, clock_timestamp())",
        )
        .bind("018f0f7e-7b7a-7cc0-8c1e-000000000032")
        .bind(operation_id)
        .bind(digest)
        .execute(&pool)
        .await
        .is_err()
    );
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.journal_events \
             (event_id, operation_id, attempt_id, aggregate_revision, semantic_key, \
              payload_digest, event_kind, payload, observed_at) \
             VALUES ($1::uuid, $2::uuid, $3::uuid, 2, 'wrong-attempt-owner', $4, \
                     'evidence_recorded', '{}'::jsonb, clock_timestamp())",
        )
        .bind("018f0f7e-7b7a-7cc0-8c1e-000000000033")
        .bind(other_operation_id)
        .bind(attempt_id)
        .bind(digest)
        .execute(&pool)
        .await
        .is_err()
    );

    sqlx::query(
        "INSERT INTO rust_controller.outbox (event_id, operation_id, topic, payload) \
         VALUES ($1::uuid, $2::uuid, 'journal_event', '{}'::jsonb)",
    )
    .bind(event_id)
    .bind(operation_id)
    .execute(&pool)
    .await
    .expect("valid outbox record must insert");

    sqlx::query(
        "INSERT INTO rust_controller.operation_projection \
         (operation_id, state, revision, last_event_id) \
         VALUES ($1::uuid, 'running', 1, $2::uuid)",
    )
    .bind(operation_id)
    .bind(event_id)
    .execute(&pool)
    .await
    .expect("valid projection must insert");

    sqlx::query(
        "INSERT INTO rust_controller.orchestration_authority \
         (singleton_key, executor_kind, generation, change_reference) \
         VALUES (1, 'rust', 1, 'local-test')",
    )
    .execute(&pool)
    .await
    .expect("valid authority must insert");
    assert!(
        sqlx::query(
            "INSERT INTO rust_controller.orchestration_authority \
             (singleton_key, executor_kind, generation, change_reference) \
             VALUES (2, 'rust', 2, 'invalid-second-row')",
        )
        .execute(&pool)
        .await
        .is_err()
    );

    let before: chrono::DateTime<chrono::Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .expect("database clock must be queryable");
    let (acquired_at, lease_expires_at, deadline_at): (
        chrono::DateTime<chrono::Utc>,
        chrono::DateTime<chrono::Utc>,
        chrono::DateTime<chrono::Utc>,
    ) = sqlx::query_as(
        "INSERT INTO rust_controller.worker_leases \
         (operation_id, attempt_id, executor_kind, generation, worker_id, lease_token) \
         VALUES ($1::uuid, $2::uuid, 'rust', 1, 'worker-one', \
                 '018f0f7e-7b7a-7cc0-8c1e-000000000041') \
         RETURNING acquired_at, lease_expires_at, deadline_at",
    )
    .bind(operation_id)
    .bind(attempt_id)
    .fetch_one(&pool)
    .await
    .expect("valid database-timed lease must insert");
    let after: chrono::DateTime<chrono::Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .expect("database clock must be queryable");
    assert!(acquired_at >= before && acquired_at <= after);
    assert!(lease_expires_at > acquired_at);
    assert!(deadline_at >= lease_expires_at);
}

#[tokio::test]
async fn append_command_returns_existing_for_same_digest_and_conflict_for_different_digest() {
    // Break caught: treating every duplicate idempotency key alike could replay
    // a changed command or reject a safe retry of the exact same command.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store
        .migrate()
        .await
        .expect("foundation migration must apply");

    let operation_id = OperationId::new();
    let semantic_key = SemanticOperationKey::new(
        WorkflowKind::SyntheticLongSleep,
        RunId::new(),
        "synthetic:sleep-one",
        1,
    )
    .unwrap();
    let accepted = CommandEnvelope::new(
        "command:sleep-one",
        semantic_key.clone(),
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    .unwrap();

    assert_eq!(
        store.append_command(operation_id, &accepted).await.unwrap(),
        CommandAppend::Appended(operation_id)
    );
    assert_eq!(
        store
            .append_command(OperationId::new(), &accepted)
            .await
            .unwrap(),
        CommandAppend::AlreadyPresent(operation_id)
    );

    let changed = CommandEnvelope::new(
        "command:sleep-one",
        semantic_key,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    .unwrap();
    assert!(matches!(
        store.append_command(OperationId::new(), &changed).await,
        Err(StoreError::CommandDigestConflict {
            existing_operation_id
        }) if existing_operation_id == operation_id
    ));

    let invalid_digest = CommandEnvelope::new(
        "command:invalid-digest",
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "synthetic:invalid-digest",
            1,
        )
        .unwrap(),
        "not-a-sha256-digest",
    )
    .unwrap();
    assert!(matches!(
        store
            .append_command(OperationId::new(), &invalid_digest)
            .await,
        Err(StoreError::InvalidPayloadDigest)
    ));

    let oversized_contract = CommandEnvelope::new(
        "command:oversized-contract",
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "synthetic:oversized-contract",
            32_768,
        )
        .unwrap(),
        "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    )
    .unwrap();
    assert!(matches!(
        store
            .append_command(OperationId::new(), &oversized_contract)
            .await,
        Err(StoreError::ContractVersionOutOfRange)
    ));

    let command_count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.commands")
        .fetch_one(&pool)
        .await
        .unwrap();
    let operation_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.operations")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!((command_count, operation_count), (1, 1));
}

#[tokio::test]
async fn two_connections_allow_one_append_winner_and_report_revision_conflict_for_loser() {
    // Break caught: checking a revision without locking lets two writers both
    // commit revision one, violating the single aggregate history.
    let postgres = PostgresContainer::start().await;
    let dsn = postgres.dsn();
    let setup_pool = postgres.wait_for_pool().await;
    let setup_store = PgStore::new(setup_pool.clone());
    setup_store
        .migrate()
        .await
        .expect("foundation migration must apply");
    let operation_id = create_operation(&setup_store, "two-writers").await;

    let left = PgStore::new(
        PgPoolOptions::new()
            .max_connections(1)
            .connect(&dsn)
            .await
            .unwrap(),
    );
    let right = PgStore::new(
        PgPoolOptions::new()
            .max_connections(1)
            .connect(&dsn)
            .await
            .unwrap(),
    );
    let left_event = journal_event(
        operation_id,
        1,
        "state:left-leased",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"writer": "left"}),
    );
    let right_event = journal_event(
        operation_id,
        1,
        "state:right-leased",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"writer": "right"}),
    );

    let (left_result, right_result) = tokio::join!(
        left.append_event(0, &left_event),
        right.append_event(0, &right_event)
    );
    let results = [left_result, right_result];
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(result, Ok(EventAppend::Appended(_))))
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(
                result,
                Err(StoreError::RevisionConflict {
                    expected: 0,
                    actual: 1
                })
            ))
            .count(),
        1
    );

    let event_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events")
            .fetch_one(&setup_pool)
            .await
            .unwrap();
    let outbox_count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.outbox")
        .fetch_one(&setup_pool)
        .await
        .unwrap();
    let projection = setup_store
        .load_operation(operation_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (event_count, outbox_count, projection.revision()),
        (1, 1, 1)
    );
    assert_eq!(projection.state(), ExecutionState::Leased);
}

#[tokio::test]
async fn append_event_classifies_same_digest_duplicate_and_changed_digest_conflict() {
    // Break caught: a retry of one semantic event must be harmless, while the
    // same semantic key with changed evidence must never overwrite history.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "event-duplicate").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:ready",
        EventKind::EvidenceRecorded,
        serde_json::json!({"ready": true}),
    );

    assert!(matches!(
        store.append_event(-1, &event).await,
        Err(StoreError::InvalidExpectedRevision {
            expected_revision: -1
        })
    ));
    assert!(matches!(
        store.append_event(1, &event).await,
        Err(StoreError::EventRevisionMismatch {
            expected: 2,
            actual: 1
        })
    ));

    assert!(matches!(
        store.append_event(0, &event).await.unwrap(),
        EventAppend::Appended(_)
    ));
    assert_eq!(
        store.append_event(0, &event).await.unwrap(),
        EventAppend::AlreadyPresent(event.event_id())
    );

    let changed = journal_event(
        operation_id,
        2,
        "evidence:ready",
        EventKind::EvidenceRecorded,
        serde_json::json!({"ready": false}),
    );
    assert!(matches!(
        store.append_event(1, &changed).await,
        Err(StoreError::EventDigestConflict { existing_event_id })
            if existing_event_id == event.event_id()
    ));

    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM rust_controller.journal_events), \
                (SELECT count(*) FROM rust_controller.outbox)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(counts, (1, 1));
}

#[tokio::test]
async fn outbox_insert_failure_rolls_back_event_projection_and_revision() {
    // Break caught: committing the journal event before its outbox/projection
    // would leave durable state that consumers can never observe consistently.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "outbox-rollback").await;
    sqlx::query("DROP TABLE rust_controller.outbox")
        .execute(&pool)
        .await
        .unwrap();
    let event = journal_event(
        operation_id,
        1,
        "state:leased",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"state": "leased"}),
    );

    assert!(matches!(
        store.append_event(0, &event).await,
        Err(StoreError::Database(_))
    ));
    let event_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events")
            .fetch_one(&pool)
            .await
            .unwrap();
    let persisted: (String, i64) = sqlx::query_as(
        "SELECT state, revision FROM rust_controller.operations WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&pool)
    .await
    .unwrap();
    let projection = store.load_operation(operation_id).await.unwrap().unwrap();
    assert_eq!(event_count, 0);
    assert_eq!(persisted, ("pending".to_owned(), 0));
    assert_eq!(projection.state(), ExecutionState::Pending);
    assert_eq!(projection.revision(), 0);
}

#[tokio::test]
async fn rebuild_projection_from_empty_store_matches_transactional_projection() {
    // Break caught: a projection updater whose rules differ from replay would
    // make recovery after projection loss change operator-visible state.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "projection-rebuild").await;
    let leased = journal_event(
        operation_id,
        1,
        "state:leased",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"state": "leased"}),
    );
    store.append_event(0, &leased).await.unwrap();
    let running = journal_event(
        operation_id,
        2,
        "state:running",
        EventKind::ExecutionStateChanged(ExecutionState::Running),
        serde_json::json!({"state": "running"}),
    );
    store.append_event(1, &running).await.unwrap();
    let evidence = journal_event(
        operation_id,
        3,
        "evidence:heartbeat",
        EventKind::EvidenceRecorded,
        serde_json::json!({"heartbeat": "synthetic"}),
    );
    store.append_event(2, &evidence).await.unwrap();
    let expected = store.load_operation(operation_id).await.unwrap().unwrap();

    sqlx::query("DELETE FROM rust_controller.operation_projection WHERE operation_id = $1")
        .bind(operation_id.as_uuid())
        .execute(&pool)
        .await
        .unwrap();
    assert!(store.load_operation(operation_id).await.unwrap().is_none());
    let rebuilt = store.rebuild_projection(operation_id).await.unwrap();

    assert_eq!(rebuilt, expected);
    assert_eq!(rebuilt.state(), ExecutionState::Running);
    assert_eq!(rebuilt.revision(), 3);
}

#[tokio::test]
async fn dequeue_outbox_claims_with_database_time_and_rejects_invalid_limits() {
    // Break caught: client-timed or unbounded dequeue can authorize stale work
    // or monopolize the outbox under load.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "outbox-dequeue").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:queued",
        EventKind::EvidenceRecorded,
        serde_json::json!({"queued": true}),
    );
    store.append_event(0, &event).await.unwrap();
    let before: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();

    let claimed = store.dequeue_outbox(1).await.unwrap();
    let after: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(claimed.len(), 1);
    assert_eq!(claimed[0].event_id(), event.event_id());
    assert!(claimed[0].claimed_at() >= &before && claimed[0].claimed_at() <= &after);
    assert!(matches!(
        store.dequeue_outbox(0).await,
        Err(StoreError::InvalidOutboxLimit { limit: 0 })
    ));
}

#[tokio::test]
async fn invalid_execution_state_jump_is_rejected_before_any_journal_write() {
    // Break caught: persisting the target state directly lets pending work skip
    // its lease and commit running state without a valid domain transition.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "invalid-state-jump").await;
    let invalid = journal_event(
        operation_id,
        1,
        "state:running-without-lease",
        EventKind::ExecutionStateChanged(ExecutionState::Running),
        serde_json::json!({"state": "running"}),
    );

    assert!(store.append_event(0, &invalid).await.is_err());

    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM rust_controller.journal_events), \
                (SELECT count(*) FROM rust_controller.outbox)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    let operation: (String, i64) = sqlx::query_as(
        "SELECT state, revision FROM rust_controller.operations WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(counts, (0, 0));
    assert_eq!(operation, ("pending".to_owned(), 0));
}

#[tokio::test]
async fn worker_lease_requires_attempt_owned_by_the_same_operation() {
    // Break caught: an operation-only lease can be replayed against another
    // attempt and cannot prove which acquisition owns the execution capability.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "lease-owner").await;
    let other_operation_id = create_operation(&store, "lease-other").await;
    let attempt_id = AttemptId::new();
    sqlx::query(
        "INSERT INTO rust_controller.attempts \
         (attempt_id, operation_id, attempt_number, state) \
         VALUES ($1, $2, 1, 'leased')",
    )
    .bind(attempt_id.as_uuid())
    .bind(operation_id.as_uuid())
    .execute(&pool)
    .await
    .unwrap();

    sqlx::query(
        "INSERT INTO rust_controller.worker_leases \
         (operation_id, attempt_id, executor_kind, generation, worker_id, lease_token) \
         VALUES ($1, $2, 'rust', 1, 'worker-one', \
                 '018f0f7e-7b7a-7cc0-8c1e-000000000051')",
    )
    .bind(operation_id.as_uuid())
    .bind(attempt_id.as_uuid())
    .execute(&pool)
    .await
    .expect("the owning operation and attempt must be accepted");

    let wrong_owner = sqlx::query(
        "INSERT INTO rust_controller.worker_leases \
         (operation_id, attempt_id, executor_kind, generation, worker_id, lease_token) \
         VALUES ($1, $2, 'rust', 1, 'worker-two', \
                 '018f0f7e-7b7a-7cc0-8c1e-000000000052')",
    )
    .bind(other_operation_id.as_uuid())
    .bind(attempt_id.as_uuid())
    .execute(&pool)
    .await;
    assert!(wrong_owner.is_err());

    let invalid_token_operation_id = create_operation(&store, "lease-invalid-token").await;
    let invalid_token_attempt_id = AttemptId::new();
    sqlx::query(
        "INSERT INTO rust_controller.attempts \
         (attempt_id, operation_id, attempt_number, state) \
         VALUES ($1, $2, 1, 'leased')",
    )
    .bind(invalid_token_attempt_id.as_uuid())
    .bind(invalid_token_operation_id.as_uuid())
    .execute(&pool)
    .await
    .unwrap();
    let invalid_token = sqlx::query(
        "INSERT INTO rust_controller.worker_leases \
         (operation_id, attempt_id, executor_kind, generation, worker_id, lease_token) \
         VALUES ($1, $2, 'rust', 1, 'worker-three', 'not-a-uuidv7')",
    )
    .bind(invalid_token_operation_id.as_uuid())
    .bind(invalid_token_attempt_id.as_uuid())
    .execute(&pool)
    .await;
    assert!(invalid_token.is_err());
}

#[tokio::test]
async fn expired_outbox_claim_is_reclaimed_by_a_second_connection_after_consumer_crash() {
    // Break caught: filtering forever on claimed_at IS NULL strands a durable
    // message after its consumer crashes between dequeue and acknowledgement.
    let postgres = PostgresContainer::start().await;
    let dsn = postgres.dsn();
    let setup_pool = postgres.wait_for_pool().await;
    let setup_store = PgStore::new(setup_pool.clone());
    setup_store.migrate().await.unwrap();
    let operation_id = create_operation(&setup_store, "outbox-crash").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:crash-recovery",
        EventKind::EvidenceRecorded,
        serde_json::json!({"source": "synthetic"}),
    );
    setup_store.append_event(0, &event).await.unwrap();

    let first_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&dsn)
        .await
        .unwrap();
    let second_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&dsn)
        .await
        .unwrap();
    let first = PgStore::new(first_pool);
    let second = PgStore::new(second_pool);
    let first_claim = first.dequeue_outbox(1).await.unwrap().remove(0);
    sqlx::query(
        "UPDATE rust_controller.outbox \
         SET claimed_at = clock_timestamp() - interval '31 seconds', \
             claim_expires_at = clock_timestamp() - interval '1 second', \
             created_at = clock_timestamp() - interval '1 minute'",
    )
    .execute(&setup_pool)
    .await
    .unwrap();

    let reclaimed = second.dequeue_outbox(1).await.unwrap();
    assert_eq!(reclaimed.len(), 1);
    assert_eq!(reclaimed[0].event_id(), event.event_id());
    assert_ne!(reclaimed[0].claim_token(), first_claim.claim_token());
    assert!(
        !first
            .ack_outbox(first_claim.outbox_id(), first_claim.claim_token())
            .await
            .unwrap()
    );
    assert!(
        second
            .ack_outbox(reclaimed[0].outbox_id(), reclaimed[0].claim_token())
            .await
            .unwrap()
    );
}

#[tokio::test]
async fn terminal_execution_state_cannot_regress_and_emits_no_followup_event() {
    // Break caught: persistence must not bypass the domain terminal fence and
    // append a later state target after the operation is satisfied.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "terminal-regression").await;
    let satisfied = journal_event(
        operation_id,
        1,
        "state:satisfied",
        EventKind::ExecutionStateChanged(ExecutionState::Satisfied),
        serde_json::json!({"state": "satisfied"}),
    );
    store.append_event(0, &satisfied).await.unwrap();
    let regression = journal_event(
        operation_id,
        2,
        "state:leased-after-satisfied",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"state": "leased"}),
    );

    assert!(store.append_event(1, &regression).await.is_err());
    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM rust_controller.journal_events), \
                (SELECT count(*) FROM rust_controller.outbox)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    let operation: (String, i64) = sqlx::query_as(
        "SELECT state, revision FROM rust_controller.operations WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(counts, (1, 1));
    assert_eq!(operation, ("satisfied".to_owned(), 1));
}

#[tokio::test]
async fn append_command_never_recreates_a_missing_progressed_projection_as_pending() {
    // Break caught: a second idempotency key for an existing semantic operation
    // must not replace a missing progressed projection with pending revision zero.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let semantic_key = SemanticOperationKey::new(
        WorkflowKind::SyntheticLongSleep,
        RunId::new(),
        "synthetic:projection-gap",
        1,
    )
    .unwrap();
    let first_command = CommandEnvelope::new(
        "command:projection-first",
        semantic_key.clone(),
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    .unwrap();
    let operation_id = OperationId::new();
    store
        .append_command(operation_id, &first_command)
        .await
        .unwrap();
    let leased = journal_event(
        operation_id,
        1,
        "state:leased",
        EventKind::ExecutionStateChanged(ExecutionState::Leased),
        serde_json::json!({"state": "leased"}),
    );
    store.append_event(0, &leased).await.unwrap();
    sqlx::query("DELETE FROM rust_controller.operation_projection WHERE operation_id = $1")
        .bind(operation_id.as_uuid())
        .execute(&pool)
        .await
        .unwrap();

    let second_command = CommandEnvelope::new(
        "command:projection-second",
        semantic_key,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    .unwrap();
    assert_eq!(
        store
            .append_command(OperationId::new(), &second_command)
            .await
            .unwrap(),
        CommandAppend::Appended(operation_id)
    );

    if let Some(projection) = store.load_operation(operation_id).await.unwrap() {
        assert_eq!(projection.state(), ExecutionState::Leased);
        assert_eq!(projection.revision(), 1);
    }
    let authoritative: (String, i64) = sqlx::query_as(
        "SELECT state, revision FROM rust_controller.operations WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(authoritative, ("leased".to_owned(), 1));
}

#[tokio::test]
async fn two_connections_classify_same_and_different_command_digests_without_extra_rows() {
    // Break caught: sequential duplicate tests miss unique-key races that can
    // leak a database error or create two semantic operations.
    let postgres = PostgresContainer::start().await;
    let dsn = postgres.dsn();
    let setup_pool = postgres.wait_for_pool().await;
    PgStore::new(setup_pool.clone()).migrate().await.unwrap();

    for (case, right_digest) in [
        (
            "same",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        (
            "different",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ),
    ] {
        let semantic_key = SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            format!("synthetic:command-race-{case}"),
            1,
        )
        .unwrap();
        let left_command = CommandEnvelope::new(
            format!("command:race-{case}"),
            semantic_key.clone(),
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        let right_command =
            CommandEnvelope::new(format!("command:race-{case}"), semantic_key, right_digest)
                .unwrap();
        let left_operation_id = OperationId::new();
        let right_operation_id = OperationId::new();
        let left = PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        );
        let right = PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        );

        let (left_result, right_result) = tokio::join!(
            left.append_command(left_operation_id, &left_command),
            right.append_command(right_operation_id, &right_command)
        );
        let results = [left_result, right_result];
        assert_eq!(
            results
                .iter()
                .filter(|result| matches!(result, Ok(CommandAppend::Appended(_))))
                .count(),
            1
        );
        if case == "same" {
            assert_eq!(
                results
                    .iter()
                    .filter(|result| matches!(result, Ok(CommandAppend::AlreadyPresent(_))))
                    .count(),
                1
            );
        } else {
            assert_eq!(
                results
                    .iter()
                    .filter(|result| matches!(
                        result,
                        Err(StoreError::CommandDigestConflict { .. })
                    ))
                    .count(),
                1
            );
        }
    }

    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM rust_controller.commands), \
                (SELECT count(*) FROM rust_controller.operations)",
    )
    .fetch_one(&setup_pool)
    .await
    .unwrap();
    assert_eq!(counts, (2, 2));
}

#[tokio::test]
async fn two_connections_classify_same_and_different_event_digests_without_extra_rows() {
    // Break caught: sequential semantic-event tests miss races between the
    // operation lock and duplicate/digest-conflict classification.
    let postgres = PostgresContainer::start().await;
    let dsn = postgres.dsn();
    let setup_pool = postgres.wait_for_pool().await;
    let setup_store = PgStore::new(setup_pool.clone());
    setup_store.migrate().await.unwrap();

    for (case, right_payload) in [
        ("same", serde_json::json!({"claim": "same"})),
        ("different", serde_json::json!({"claim": "right"})),
    ] {
        let operation_id = create_operation(&setup_store, &format!("event-race-{case}")).await;
        let left_payload = if case == "same" {
            right_payload.clone()
        } else {
            serde_json::json!({"claim": "left"})
        };
        let left_event = journal_event(
            operation_id,
            1,
            "state:leased",
            EventKind::ExecutionStateChanged(ExecutionState::Leased),
            left_payload,
        );
        let right_event = journal_event(
            operation_id,
            1,
            "state:leased",
            EventKind::ExecutionStateChanged(ExecutionState::Leased),
            right_payload,
        );
        let left = PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        );
        let right = PgStore::new(
            PgPoolOptions::new()
                .max_connections(1)
                .connect(&dsn)
                .await
                .unwrap(),
        );

        let (left_result, right_result) = tokio::join!(
            left.append_event(0, &left_event),
            right.append_event(0, &right_event)
        );
        let results = [left_result, right_result];
        assert_eq!(
            results
                .iter()
                .filter(|result| matches!(result, Ok(EventAppend::Appended(_))))
                .count(),
            1
        );
        if case == "same" {
            assert_eq!(
                results
                    .iter()
                    .filter(|result| matches!(result, Ok(EventAppend::AlreadyPresent(_))))
                    .count(),
                1
            );
        } else {
            assert_eq!(
                results
                    .iter()
                    .filter(|result| matches!(result, Err(StoreError::EventDigestConflict { .. })))
                    .count(),
                1
            );
        }
    }

    let counts: (i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM rust_controller.journal_events), \
                (SELECT count(*) FROM rust_controller.outbox)",
    )
    .fetch_one(&setup_pool)
    .await
    .unwrap();
    assert_eq!(counts, (2, 2));
}

#[tokio::test]
async fn migration_creates_partial_outbox_claim_index_aligned_with_dequeue() {
    // Break caught: dequeue scans all delivered rows when the partial index does
    // not cover ordering, availability, and claim-expiry eligibility.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();

    let index_definition: Option<String> = sqlx::query_scalar(
        "SELECT indexdef FROM pg_indexes \
         WHERE schemaname = 'rust_controller' \
           AND indexname = 'idx_outbox_delivery_eligible'",
    )
    .fetch_optional(&pool)
    .await
    .unwrap();
    let index_definition = index_definition.expect("eligible outbox index must exist");
    assert!(index_definition.contains("(outbox_id, available_at, claim_expires_at)"));
    assert!(index_definition.contains("WHERE (delivered_at IS NULL)"));
}

#[tokio::test]
async fn acknowledge_outbox_requires_the_current_unexpired_claim_token() {
    // Break caught: acknowledgement by row ID alone lets an expired consumer
    // mark a message delivered after another consumer has reclaimed it.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "outbox-ack").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:ack",
        EventKind::EvidenceRecorded,
        serde_json::json!({"ack": "synthetic"}),
    );
    store.append_event(0, &event).await.unwrap();
    let claim = store.dequeue_outbox(1).await.unwrap().remove(0);

    assert!(
        !store
            .ack_outbox(claim.outbox_id(), uuid::Uuid::now_v7())
            .await
            .unwrap()
    );
    let before: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(
        store
            .ack_outbox(claim.outbox_id(), claim.claim_token())
            .await
            .unwrap()
    );
    let delivered_at: chrono::DateTime<Utc> =
        sqlx::query_scalar("SELECT delivered_at FROM rust_controller.outbox WHERE outbox_id = $1")
            .bind(claim.outbox_id())
            .fetch_one(&pool)
            .await
            .unwrap();
    let after: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(delivered_at >= before && delivered_at <= after);
    assert!(store.dequeue_outbox(1).await.unwrap().is_empty());
}

#[tokio::test]
async fn release_outbox_requires_the_current_claim_and_makes_the_row_available_again() {
    // Break caught: a consumer cannot safely abandon work when release lacks an
    // acquisition token or leaves the row unavailable until process restart.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "outbox-release").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:release",
        EventKind::EvidenceRecorded,
        serde_json::json!({"release": "synthetic"}),
    );
    store.append_event(0, &event).await.unwrap();
    let first_claim = store.dequeue_outbox(1).await.unwrap().remove(0);

    assert!(
        !store
            .release_outbox(first_claim.outbox_id(), uuid::Uuid::now_v7())
            .await
            .unwrap()
    );
    assert!(
        store
            .release_outbox(first_claim.outbox_id(), first_claim.claim_token())
            .await
            .unwrap()
    );
    let second_claim = store.dequeue_outbox(1).await.unwrap().remove(0);
    assert_eq!(second_claim.event_id(), event.event_id());
    assert_ne!(second_claim.claim_token(), first_claim.claim_token());
}

#[tokio::test]
async fn outbox_claim_persists_database_clock_expiry() {
    // Break caught: deriving expiry later from application policy makes an
    // existing durable claim change meaning across process versions/restarts.
    let postgres = PostgresContainer::start().await;
    let pool = postgres.wait_for_pool().await;
    let store = PgStore::new(pool.clone());
    store.migrate().await.unwrap();
    let operation_id = create_operation(&store, "outbox-expiry").await;
    let event = journal_event(
        operation_id,
        1,
        "evidence:expiry",
        EventKind::EvidenceRecorded,
        serde_json::json!({"expiry": "synthetic"}),
    );
    store.append_event(0, &event).await.unwrap();
    let before: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();
    let claim = store.dequeue_outbox(1).await.unwrap().remove(0);

    let claimed_at = *claim.claimed_at();
    let claim_expires_at = *claim.claim_expires_at();
    let after: chrono::DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await
        .unwrap();
    let persisted_expiry: chrono::DateTime<Utc> = sqlx::query_scalar(
        "SELECT claim_expires_at FROM rust_controller.outbox WHERE outbox_id = $1",
    )
    .bind(claim.outbox_id())
    .fetch_one(&pool)
    .await
    .unwrap();
    let ttl = claim_expires_at - claimed_at;
    assert!(claimed_at >= before && claimed_at <= after);
    assert_eq!(persisted_expiry, claim_expires_at);
    assert!(ttl >= chrono::Duration::seconds(29));
    assert!(ttl <= chrono::Duration::seconds(31));
}
