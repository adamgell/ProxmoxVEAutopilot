#[allow(
    dead_code,
    reason = "shared registration support has other harness consumers"
)]
mod osdeploy_support;

#[tokio::test]
async fn activation_creates_one_attempt_with_policy_deadline() {
    let f = osdeploy_support::Fixture::new().await;
    let p = osdeploy_support::plan();
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(g.attempt_number(), 1);
    assert_eq!((*g.deadline_at() - *g.acquired_at()).num_seconds(), 300);
    let s = f.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(s.attempt_id(), Some(g.attempt_id()));
    assert_eq!(s.state(), controller_domain::ExecutionState::Leased);
    assert!(
        f.other_scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
    f.scheduler()
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    let starts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(op.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!(starts, 1);
}

use osdeploy_support::{Fixture, plan};
use serde_json::{Value, json};
use sqlx::{PgConnection, postgres::PgPoolOptions};
use std::time::Duration;
use uuid::Uuid;

const TABLES: [&str; 9] = [
    "osdeploy_decisions",
    "osdeploy_deadlines",
    "osdeploy_attempt_bindings",
    "osdeploy_lease_epochs",
    "osdeploy_pve_evidence",
    "osdeploy_pve_dispatches",
    "osdeploy_pve_receipts",
    "osdeploy_run_cancellations",
    "osdeploy_schedule_projection",
];

#[tokio::test]
async fn lifecycle_same_operation_race_and_start_replay_are_atomic() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let first = f.scheduler();
    let second = f.other_scheduler();
    let (a, b) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            first.claim_osdeploy_bound(op, ids.workflow_sha256(), 1),
            second.claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        )
    })
    .await
    .unwrap();
    let (g, scheduler) = match (a.unwrap(), b.unwrap()) {
        (Some(g), None) => (g, first),
        (None, Some(g)) => (g, second),
        _ => panic!("one claimant must win"),
    };
    let other_pool_same_owner = postgres_store::Scheduler::new(
        f.other.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        g.worker_id(),
    )
    .unwrap();
    let (a, b) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            scheduler.start_osdeploy_bound(&g, ids.workflow_sha256()),
            other_pool_same_owner.start_osdeploy_bound(&g, ids.workflow_sha256())
        )
    })
    .await
    .unwrap();
    assert_eq!(a.unwrap(), controller_domain::ExecutionState::Running);
    assert_eq!(b.unwrap(), controller_domain::ExecutionState::Running);
    let before = f.snapshot().await;
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(f.snapshot().await, before);
    assert_eq!(before["attempts"].as_array().unwrap().len(), 1);
    assert_eq!(before["osdeploy_lease_epochs"].as_array().unwrap().len(), 1);
    let chain: Vec<(i64,String,chrono::DateTime<chrono::Utc>,Uuid)> = sqlx::query_as("SELECT aggregate_revision,event_kind,observed_at,attempt_id FROM rust_controller.journal_events WHERE operation_id=$1 ORDER BY aggregate_revision")
        .bind(op.as_uuid()).fetch_all(&f.pool).await.unwrap();
    assert_eq!(
        chain
            .iter()
            .map(|r| (r.0, r.1.as_str()))
            .collect::<Vec<_>>(),
        vec![
            (1, "decision_recorded"),
            (2, "decision_recorded"),
            (3, "execution_state_changed"),
            (4, "attempt_started"),
            (5, "decision_recorded"),
            (6, "execution_state_changed")
        ]
    );
    assert_eq!(chain[3].2, chain[4].2);
    assert_eq!(chain[4].2, chain[5].2);
    assert!(chain.iter().all(|r| r.3 == g.attempt_id().as_uuid()));
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        6
    );
    let secret_in_history: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND payload::text LIKE '%' || $2 || '%') OR EXISTS(SELECT 1 FROM rust_controller.outbox WHERE operation_id=$1 AND payload::text LIKE '%' || $2 || '%')")
        .bind(op.as_uuid()).bind(g.lease_token().to_string()).fetch_one(&f.pool).await.unwrap();
    assert!(!secret_in_history);
}

fn other_lifecycle_plan() -> osdeploy_adapter::OsDeployPlanV1 {
    osdeploy_support::altered(|v| {
        v["vm"]["target_vmid"] = json!(102);
        v["vm"]["uuid"] = json!("33333333-3333-4333-8333-333333333302");
        v["vm"]["mac"] = json!("02:00:00:00:01:02");
        v["names"]["requested_name"] = json!("Another");
        v["names"]["windows_name"] = json!("Another");
        v["names"]["expected_agent_id"] = json!("agent-another");
    })
}

#[tokio::test]
async fn lifecycle_last_cap_race_counts_old_generation_leases() {
    let f = Fixture::new().await;
    let first = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let second = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &other_lifecycle_plan())
        .await
        .unwrap();
    let a = f.scheduler();
    let b = f.other_scheduler();
    let (left, right) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            a.claim_osdeploy_bound(
                first.operation(osdeploy_adapter::OsDeployStage::Clone),
                first.workflow_sha256(),
                1
            ),
            b.claim_osdeploy_bound(
                second.operation(osdeploy_adapter::OsDeployStage::Clone),
                second.workflow_sha256(),
                1
            )
        )
    })
    .await
    .unwrap();
    let (loser, winner_grant) = match (left.unwrap(), right.unwrap()) {
        (Some(g), None) => (second, g),
        (None, Some(g)) => (first, g),
        _ => panic!("last cap slot must have one owner"),
    };
    sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
        .execute(&f.pool)
        .await
        .unwrap();
    let current = postgres_store::Scheduler::new(
        f.other.clone(),
        postgres_store::ExecutorKind::Rust,
        2,
        "generation-two",
    )
    .unwrap();
    let before = f.snapshot().await;
    assert!(
        current
            .claim_osdeploy_bound(
                loser.operation(osdeploy_adapter::OsDeployStage::Clone),
                loser.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(f.snapshot().await, before);
    assert_eq!(winner_grant.generation(), 1);
}

#[tokio::test]
async fn lifecycle_heartbeat_refreshes_epoch_without_extending_scope() {
    let f = Fixture::new().await;
    let p = osdeploy_support::altered(|v| {
        v["policy"]["mutation_seconds"] = json!(12);
        v["policy"]["evidence_freshness_seconds"] = json!(1);
    });
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let scheduler = f.scheduler();
    let g = scheduler
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(g.lease_expires_at(), g.deadline_at());
    let before = f.snapshot().await;
    let early = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(early.revision(), 6);
    assert_eq!(f.snapshot().await, before);
    tokio::time::timeout(Duration::from_secs(13),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz + interval '10.05 seconds' - clock_timestamp()))))")
        .bind(g.acquired_at()).execute(&f.pool)).await.unwrap().unwrap();
    let refreshed = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(refreshed.revision(), 7);
    assert_eq!(refreshed.grant().deadline_at(), g.deadline_at());
    assert_eq!(refreshed.grant().acquired_at(), g.acquired_at());
    assert_eq!(refreshed.grant().attempt_id(), g.attempt_id());
    assert_eq!(refreshed.grant().lease_token(), g.lease_token());
    assert_eq!(refreshed.grant().lease_expires_at(), g.deadline_at());
    assert!(refreshed.grant().heartbeat_at() > g.heartbeat_at());
    assert!(refreshed.remaining() < Duration::from_secs(2));
    let before = f.snapshot().await;
    let observed = scheduler
        .continuation_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(observed.grant(), refreshed.grant());
    assert!(observed.checked_at() >= refreshed.checked_at());
    assert_eq!(f.snapshot().await, before);
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        7
    );
}

#[tokio::test]
async fn lifecycle_heartbeat_extends_only_the_existing_short_lease() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let scheduler = f.scheduler();
    let g = scheduler
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(Duration::from_secs(13),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz + interval '10.05 seconds' - clock_timestamp()))))")
        .bind(g.acquired_at()).execute(&f.pool)).await.unwrap().unwrap();
    let refreshed = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(refreshed.revision(), 4);
    assert!(*refreshed.grant().lease_expires_at() > *g.lease_expires_at());
    assert_eq!(
        (*refreshed.grant().lease_expires_at() - *refreshed.grant().heartbeat_at()).num_seconds(),
        30
    );
    assert_eq!(refreshed.grant().deadline_at(), g.deadline_at());
    assert_eq!(refreshed.grant().attempt_id(), g.attempt_id());
    assert_eq!(refreshed.grant().acquired_at(), g.acquired_at());
    let before = f.snapshot().await;
    let observed = scheduler
        .continuation_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(observed.grant(), refreshed.grant());
    assert!(observed.checked_at() >= refreshed.checked_at());
    assert!(
        f.snapshot().await == before,
        "continuation changed durable state"
    );
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        7
    );
}

#[tokio::test]
async fn lifecycle_fences_workers_hashes_generation_and_disabled_stages_without_writes() {
    use postgres_store::OsDeployExecutionError as E;
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 0)
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler().claim_osdeploy_bound(op, "bad", 1).await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, &"a".repeat(64), 1)
            .await,
        Err(E::FenceLost)
    ));
    for stage in osdeploy_adapter::OsDeployStage::ALL.into_iter().skip(3) {
        assert!(
            matches!(
                f.scheduler()
                    .claim_osdeploy_bound(ids.operation(stage), ids.workflow_sha256(), 1)
                    .await,
                Err(E::CapabilityUnavailable)
            ),
            "{stage:?}"
        );
    }
    for stage in [
        osdeploy_adapter::OsDeployStage::DiskCapacity,
        osdeploy_adapter::OsDeployStage::ConfigurePe,
    ] {
        assert!(
            f.scheduler()
                .claim_osdeploy_bound(ids.operation(stage), ids.workflow_sha256(), 1)
                .await
                .unwrap()
                .is_none()
        );
    }
    assert_eq!(f.snapshot().await, before);
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.other_scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.other_scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.other_scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
        .execute(&f.pool)
        .await
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler().osdeploy_authority_snapshot().await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn lifecycle_activation_rolls_back_at_every_write_and_commit_boundary() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::raw_sql("CREATE SEQUENCE lifecycle_write_number; CREATE TABLE lifecycle_fault(target bigint); INSERT INTO lifecycle_fault VALUES(0); CREATE FUNCTION lifecycle_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('lifecycle_write_number')=(SELECT target FROM lifecycle_fault) THEN RAISE EXCEPTION 'owned_lifecycle_fault'; END IF; RETURN NEW; END $$;").execute(&f.pool).await.unwrap();
    for table in [
        "attempts",
        "journal_events",
        "outbox",
        "osdeploy_decisions",
        "operations",
        "operation_projection",
        "osdeploy_deadlines",
        "osdeploy_attempt_bindings",
        "osdeploy_lease_epochs",
        "worker_leases",
    ] {
        sqlx::query(&format!("CREATE TRIGGER lifecycle_write_fault BEFORE INSERT OR UPDATE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION lifecycle_fail_write()"))
            .execute(&f.pool).await.unwrap();
    }
    let before = f.snapshot().await;
    // Projection UPSERT fires its INSERT and UPDATE row triggers, so the two
    // decision projections and final state projection each have two boundaries.
    for boundary in 1..=22_i64 {
        sqlx::query("UPDATE lifecycle_fault SET target=$1")
            .bind(boundary)
            .execute(&f.pool)
            .await
            .unwrap();
        sqlx::query("SELECT setval('lifecycle_write_number',1,false)")
            .execute(&f.pool)
            .await
            .unwrap();
        assert!(
            matches!(
                f.scheduler()
                    .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
                    .await,
                Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
            ),
            "write boundary {boundary}"
        );
        assert_eq!(f.snapshot().await, before, "write boundary {boundary}");
    }
    sqlx::query("UPDATE lifecycle_fault SET target=0")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE FUNCTION lifecycle_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_lifecycle_commit_fault'; END $$; CREATE CONSTRAINT TRIGGER lifecycle_commit_fault AFTER INSERT ON rust_controller.worker_leases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lifecycle_fail_commit();").execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await,
        Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("DROP TRIGGER lifecycle_commit_fault ON rust_controller.worker_leases")
        .execute(&f.pool)
        .await
        .unwrap();
    assert!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_some()
    );
    f.store.load_osdeploy_operation(op).await.unwrap();
}

#[tokio::test]
async fn lifecycle_final_clock_check_rolls_back_expired_activation_and_start() {
    let f = Fixture::new().await;
    let p = osdeploy_support::altered(|v| {
        v["policy"]["mutation_seconds"] = json!(1);
        v["policy"]["evidence_freshness_seconds"] = json!(1);
    });
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::raw_sql("CREATE FUNCTION lifecycle_delay_state() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_kind='execution_state_changed' THEN PERFORM pg_sleep(1.05); END IF; RETURN NEW; END $$; CREATE TRIGGER lifecycle_delay BEFORE INSERT ON rust_controller.journal_events FOR EACH ROW EXECUTE FUNCTION lifecycle_delay_state();").execute(&f.pool).await.unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("ALTER TABLE rust_controller.journal_events DISABLE TRIGGER lifecycle_delay")
        .execute(&f.pool)
        .await
        .unwrap();
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    sqlx::query("ALTER TABLE rust_controller.journal_events ENABLE TRIGGER lifecycle_delay")
        .execute(&f.pool)
        .await
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn lifecycle_historical_generic_osdeploy_cannot_be_adopted() {
    use controller_domain::{
        CommandEnvelope, OperationId, RunId, SemanticOperationKey, WorkflowKind,
    };
    use postgres_store::OsDeployExecutionError as E;
    let f = Fixture::new().await;
    let op = OperationId::new();
    let command = CommandEnvelope::new(
        "historical-lifecycle",
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "historical",
            1,
        )
        .unwrap(),
        "a".repeat(64),
    )
    .unwrap();
    f.store.append_command(op, &command).await.unwrap();
    let g = f
        .scheduler()
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    sqlx::query(
        "UPDATE rust_controller.operations SET workflow_kind='os_deploy' WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .execute(&f.pool)
    .await
    .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, &"a".repeat(64), 1)
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(
        f.snapshot().await == before,
        "historical execution rejection wrote data"
    );
}

#[tokio::test]
async fn unactivated_reload_rejects_an_unbound_attempt() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,1,'pending')")
        .bind(controller_domain::AttemptId::new().as_uuid()).bind(op.as_uuid())
        .execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(op).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

// Internally consistent SQL history exercises observational reload only. These
// rows never provide successful predecessors or enter a scheduler/send API.
struct ReloadHistory {
    operation: controller_domain::OperationId,
    attempt: controller_domain::AttemptId,
    run: controller_domain::RunId,
    activation: controller_domain::EventId,
    acquisition: controller_domain::EventId,
    at: chrono::DateTime<chrono::Utc>,
    deadline: chrono::DateTime<chrono::Utc>,
    workflow: String,
    stage: String,
}
impl ReloadHistory {
    async fn new(f: &Fixture) -> Self {
        Self::with_plan(f, plan()).await
    }
    async fn with_plan(f: &Fixture, p: osdeploy_adapter::OsDeployPlanV1) -> Self {
        let run = controller_domain::RunId::new();
        let ids = f.store.enqueue_osdeploy(run, &p).await.unwrap();
        let operation = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
        let registration = f.store.load_osdeploy_registration(run).await.unwrap();
        let at = "2026-09-05T12:00:00Z"
            .parse::<chrono::DateTime<chrono::Utc>>()
            .unwrap();
        let s = Self {
            operation,
            attempt: controller_domain::AttemptId::new(),
            run,
            activation: controller_domain::EventId::new(),
            acquisition: controller_domain::EventId::new(),
            at,
            deadline: at + chrono::Duration::seconds(i64::from(p.policy().mutation_seconds())),
            workflow: ids.workflow_sha256().to_owned(),
            stage: registration
                .stage(osdeploy_adapter::OsDeployStage::Clone)
                .fingerprint()
                .unwrap(),
        };
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'leased',$3,$4)")
            .bind(s.attempt.as_uuid()).bind(operation.as_uuid()).bind(s.at).bind(s.deadline).execute(&mut *tx).await.unwrap();
        let activation = s.envelope(
            "stage_activated",
            0,
            Value::Null,
            json!({"scope_key":"mutation_clone",
            "anchor_operation_id":operation,"anchor_event_id":s.activation,"opened_at":s.at,
            "budget_seconds":p.policy().mutation_seconds(),"deadline_at":s.deadline,
            "predecessor_operation_id":null,"predecessor_decision_event_id":null}),
        );
        s.decision(&mut tx, s.activation, activation).await;
        insert_row(
            &mut tx,
            "osdeploy_deadlines",
            &json!({"run_id":run,"scope_key":"mutation_clone",
            "anchor_operation_id":operation,"anchor_event_id":s.activation,"opened_at":s.at,
            "budget_seconds":p.policy().mutation_seconds(),"deadline_at":s.deadline}),
        )
        .await
        .unwrap();
        insert_row(
            &mut tx,
            "osdeploy_attempt_bindings",
            &json!({"operation_id":operation,"run_id":run,
            "attempt_id":s.attempt,"scope_key":"mutation_clone","activation_event_id":s.activation,
            "activated_at":s.at,"deadline_at":s.deadline,"activation_mode":"leased"}),
        )
        .await
        .unwrap();
        let token = Uuid::now_v7().to_string();
        let token_hash: String =
            sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
                .bind(&token)
                .fetch_one(&mut *tx)
                .await
                .unwrap();
        s.decision(&mut tx,s.acquisition,s.envelope("lease_acquired",1,Value::Null,json!({"purpose":"initial_evaluation",
            "acquisition_event_id":s.acquisition,"token_sha256":token_hash,"worker_id":"reload-only-worker",
            "acquired_at":s.at,"expires_at":s.at+chrono::Duration::seconds(30),"deadline_at":s.deadline,
            "prior_schedule_event_id":null}))).await;
        insert_row(&mut tx,"osdeploy_lease_epochs",&json!({"acquisition_event_id":s.acquisition,"operation_id":operation,
            "run_id":run,"attempt_id":s.attempt,"executor_kind":"rust","generation":1,"worker_id":"reload-only-worker",
            "lease_token_sha256":token_hash,"acquired_at":s.at,"initial_expires_at":s.at+chrono::Duration::seconds(30),
            "deadline_at":s.deadline,"purpose":"initial_evaluation"})).await.unwrap();
        s.event(
            &mut tx,
            controller_domain::EventId::new(),
            3,
            "execution_state_changed",
            Some("leased"),
            json!({"state":"leased","decision_event_id":s.acquisition}),
            s.at,
        )
        .await;
        sqlx::query("INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token,acquired_at,heartbeat_at,lease_expires_at,deadline_at) VALUES($1,$2,'rust',1,'reload-only-worker',$6,$3,$3,$4,$5)")
            .bind(operation.as_uuid()).bind(s.attempt.as_uuid()).bind(s.at).bind(s.at+chrono::Duration::seconds(30)).bind(s.deadline).bind(token).execute(&mut *tx).await.unwrap();
        tx.commit().await.unwrap();
        s
    }
    fn envelope(&self, action: &str, before: i64, resolution: Value, detail: Value) -> Value {
        json!({"contract_version":1,"action":action,"run_id":self.run,"operation_id":self.operation,
            "workflow_sha256":self.workflow,"stage_sha256":self.stage,"attempt_id":self.attempt,
            "generation":1,"before_revision":before,"evaluated_at":self.at,"resolution":resolution,"detail":detail})
    }
    async fn decision(
        &self,
        tx: &mut PgConnection,
        event: controller_domain::EventId,
        payload: Value,
    ) {
        let revision = payload["before_revision"].as_i64().unwrap() + 1;
        let at = serde_json::from_value(payload["evaluated_at"].clone()).unwrap();
        self.event(
            tx,
            event,
            revision,
            "decision_recorded",
            None,
            payload.clone(),
            at,
        )
        .await;
        let canonical =
            String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap();
        insert_row(tx,"osdeploy_decisions",&json!({"operation_id":self.operation,"decision_revision":revision,
            "event_id":event,"run_id":self.run,"attempt_id":self.attempt,"action":payload["action"],
            "resolution":payload["resolution"],"workflow_sha256":self.workflow,"stage_sha256":self.stage,
            "generation":1,"evaluated_at":at,"payload_canonical_json":canonical})).await.unwrap();
    }
    #[allow(clippy::too_many_arguments)]
    async fn event(
        &self,
        tx: &mut PgConnection,
        event: controller_domain::EventId,
        revision: i64,
        kind: &str,
        state: Option<&str>,
        payload: Value,
        at: chrono::DateTime<chrono::Utc>,
    ) {
        sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)")
            .bind(event.as_uuid()).bind(self.operation.as_uuid()).bind(self.attempt.as_uuid()).bind(revision)
            .bind(format!("reload-only:{}",event.as_uuid())).bind(event_journal::payload_digest(&payload).unwrap()).bind(kind).bind(state).bind(payload).bind(at).execute(&mut *tx).await.unwrap();
        sqlx::query("UPDATE rust_controller.operations SET revision=$2,state=coalesce($3,state) WHERE operation_id=$1")
            .bind(self.operation.as_uuid()).bind(revision).bind(state).execute(&mut *tx).await.unwrap();
        if let Some(state) = state {
            sqlx::query("UPDATE rust_controller.attempts SET state=$2,completed_at=CASE WHEN $2 IN ('satisfied','failed','blocked','unknown','conflicted') THEN $3 ELSE NULL END WHERE attempt_id=$1")
            .bind(self.attempt.as_uuid()).bind(state).bind(at).execute(&mut *tx).await.unwrap();
        }
    }

    async fn started(&self, f: &Fixture) {
        let mut tx = f.pool.begin().await.unwrap();
        self.event(
            &mut tx,
            controller_domain::EventId::new(),
            4,
            "attempt_started",
            None,
            json!({"phase":"mutation_started"}),
            self.at,
        )
        .await;
        let decision = controller_domain::EventId::new();
        self.decision(
            &mut tx,
            decision,
            self.envelope(
                "evaluation_started",
                4,
                Value::Null,
                json!({"lease_acquisition_event_id":self.acquisition,"activity":"preflight_read"}),
            ),
        )
        .await;
        self.event(
            &mut tx,
            controller_domain::EventId::new(),
            6,
            "execution_state_changed",
            Some("running"),
            json!({"state":"running","decision_event_id":decision}),
            self.at,
        )
        .await;
        tx.commit().await.unwrap();
    }
    async fn dispatched(
        &self,
        f: &Fixture,
        source: pve_port::ProvisioningVmConfigV1,
    ) -> (
        controller_domain::EventId,
        controller_domain::EventId,
        pve_port::ProvisioningDispatchV1,
    ) {
        use pve_port::*;
        self.started(f).await;
        let reg = f.store.load_osdeploy_registration(self.run).await.unwrap();
        let p = reg
            .stage(osdeploy_adapter::OsDeployStage::Clone)
            .pve()
            .unwrap()
            .clone();
        let binding = ProvisioningBindingV1::new(
            self.run,
            self.operation,
            self.attempt,
            &self.workflow,
            &p,
            6,
        )
        .unwrap();
        let power = VmPowerStatus::from_wire(
            p.expected().vm().node().clone(),
            p.expected().vm().source_vmid(),
            json!({"vmid":900,"status":"stopped","locked":0}),
            self.at,
        )
        .unwrap();
        let facts = ProvisioningEvidenceV1::new(ProvisioningEvidenceInputV1 {
            binding: binding.clone(),
            plan: p.clone(),
            source: NativeEvidenceSource::FakePve,
            collected_at: self.at,
            node: None,
            storage: None,
            bridges: None,
            inventory: None,
            inventory_coverage: ProvisioningCoverageV1::Partial,
            identities: vec![],
            source_config: Some(NativeRead::new(self.at, Ok(source.clone()))),
            source_power: Some(NativeRead::new(self.at, Ok(power.clone()))),
            target_config: None,
            target_power: None,
            media: vec![],
            qga: None,
            task: None,
            receipt: None,
        })
        .unwrap();
        let clone = CloneRequest::new(p.expected().vm().clone(), self.operation);
        let request = ProvisioningMutationRequestV1::Clone(
            CloneProvisioningRequestV1::new(
                binding,
                p.clone(),
                clone,
                ProvisioningBeforeStateV1::new(source, power).unwrap(),
                self.at,
                30,
            )
            .unwrap(),
        );
        let evidence = controller_domain::EventId::new();
        let dispatch = controller_domain::EventId::new();
        let d = ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
            request: request.clone(),
            source: NativeEvidenceSource::FakePve,
            preflight_event_id: evidence,
            original_generation: 1,
            dispatch_revision: 8,
            dispatched_at: self.at,
        })
        .unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let payload = serde_json::to_value(&facts).unwrap();
        self.event(
            &mut tx,
            evidence,
            7,
            "evidence_recorded",
            None,
            payload.clone(),
            self.at,
        )
        .await;
        insert_row(&mut tx,"osdeploy_pve_evidence",&json!({"event_id":evidence,"operation_id":self.operation,"run_id":self.run,
            "attempt_id":self.attempt,"evidence_revision":7,"evidence_sha256":event_journal::payload_digest(&payload).unwrap(),"source":"fake_pve",
            "evidence_canonical_json":String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap()})).await.unwrap();
        self.decision(&mut tx,dispatch,self.envelope("pve_dispatch_committed",7,json!("ready"),json!({"preflight_event_id":evidence,
            "pve_plan_sha256":p.fingerprint().unwrap(),"request_sha256":request.request_digest().unwrap(),"dispatched_at":self.at,
            "lease_acquisition_event_id":self.acquisition}))).await;
        insert_row(&mut tx,"osdeploy_pve_dispatches",&json!({"operation_id":self.operation,"run_id":self.run,"attempt_id":self.attempt,
            "dispatch_event_id":dispatch,"dispatch_revision":8,"preflight_event_id":evidence,"workflow_sha256":self.workflow,
            "pve_plan_sha256":p.fingerprint().unwrap(),"request_sha256":request.request_digest().unwrap(),
            "request_canonical_json":String::from_utf8(event_journal::canonical_json_bytes(&serde_json::to_value(request).unwrap()).unwrap()).unwrap(),
            "source":"fake_pve","original_generation":1,"dispatched_at":self.at,"lease_acquisition_event_id":self.acquisition})).await.unwrap();
        tx.commit().await.unwrap();
        (dispatch, evidence, d)
    }
}

fn reload_source() -> pve_port::ProvisioningVmConfigV1 {
    pve_port::ProvisioningVmConfigV1::from_wire(pve_port::NodeName::parse("node-a").unwrap(),pve_port::Vmid::new(900).unwrap(),pve_port::NativeEvidenceSource::FakePve,
        json!({"node":"node-a","vmid":900,"digest":"reload-template","name":"blank-template","cores":2,"memory":2048,
            "scsi0":"disk-store:vm-900-disk-0,size=80G","smbios1":"uuid=33333333-3333-4333-8333-333333333390",
            "net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0","bios":"seabios","cpu":"host","balloon":0,
            "agent":"enabled=0,type=virtio","boot":"order=scsi0","template":1}),"2026-09-05T12:00:00Z".parse().unwrap()).unwrap()
}

#[tokio::test]
async fn first_start_reload_rejects_timestamp_only_corruption_and_restores() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    h.started(&f).await;
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2 WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(h.operation.as_uuid()).bind(h.at+chrono::Duration::seconds(1)).execute(&f.pool).await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "timestamp-only first-start corruption was accepted"
    );
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2 WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(h.operation.as_uuid()).bind(h.at).execute(&f.pool).await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
}

#[tokio::test]
async fn first_start_reload_rejects_interleaved_history() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let mut tx = f.pool.begin().await.unwrap();
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        4,
        "attempt_started",
        None,
        json!({"phase":"mutation_started"}),
        h.at,
    )
    .await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "evidence_recorded",
        None,
        json!({"unrelated":"observational-only"}),
        h.at,
    )
    .await;
    let evaluation = controller_domain::EventId::new();
    h.decision(
        &mut tx,
        evaluation,
        h.envelope(
            "evaluation_started",
            5,
            Value::Null,
            json!({"lease_acquisition_event_id":h.acquisition,"activity":"preflight_read"}),
        ),
    )
    .await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        7,
        "execution_state_changed",
        Some("running"),
        json!({"state":"running","decision_event_id":evaluation}),
        h.at,
    )
    .await;
    tx.commit().await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "interleaved first-start transaction was accepted"
    );
}

#[tokio::test]
async fn ordinary_lease_reload_park_resume_and_later_start() {
    let f = Fixture::new().await;
    let source = reload_source();
    let p = osdeploy_support::altered(|v| {
        v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
    });
    let h = ReloadHistory::with_plan(&f, p).await;
    let (_, evidence, _) = h.dispatched(&f, source).await;
    let old_lease: Value = sqlx::query_scalar(
        "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .fetch_one(&f.pool)
    .await
    .unwrap();
    let evidence_hash: String = sqlx::query_scalar(
        "SELECT evidence_sha256 FROM rust_controller.osdeploy_pve_evidence WHERE event_id=$1",
    )
    .bind(evidence.as_uuid())
    .fetch_one(&f.pool)
    .await
    .unwrap();
    let park = controller_domain::EventId::new();
    let resumed_at = h.at + chrono::Duration::seconds(2);
    let mut tx = f.pool.begin().await.unwrap();
    h.decision(&mut tx, park, h.envelope("pve_evaluated",8,json!("waiting"),json!({"mode":"outcome","advice":"waiting",
        "evidence_event_id":evidence,"evidence_sha256":evidence_hash,"scope_key":"mutation_clone","deadline_at":h.deadline,
        "reason":"task_running","lease_acquisition_event_id":h.acquisition,
        "schedule":{"mode":"waiting","next_check_at":resumed_at,"unavailable_count":0}}))).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        10,
        "execution_state_changed",
        Some("waiting"),
        json!({"state":"waiting","decision_event_id":park}),
        h.at,
    )
    .await;
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let parked = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(parked.state(), controller_domain::ExecutionState::Waiting);
    assert_eq!(parked.next_check_at(), Some(resumed_at));
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT (jsonb_populate_record(NULL::rust_controller.worker_leases,$1::jsonb)).*").bind(&old_lease).execute(&f.pool).await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "ordinary park accepted the evaluator lease it ended"
    );

    // A later acquisition is a distinct durable epoch; Waiting itself is not
    // forbidden from carrying that new lease before its resumed start.
    let resume = controller_domain::EventId::new();
    let token = Uuid::now_v7().to_string();
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    let token_hash: String =
        sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
            .bind(&token)
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    let expires = resumed_at + chrono::Duration::seconds(30);
    let mut payload = h.envelope("lease_acquired",10,Value::Null,json!({"purpose":"resume_evaluation","acquisition_event_id":resume,
        "token_sha256":token_hash,"worker_id":"reload-only-resumed-worker","acquired_at":resumed_at,"expires_at":expires,
        "deadline_at":h.deadline,"prior_schedule_event_id":park}));
    payload["evaluated_at"] = json!(resumed_at);
    h.decision(&mut tx, resume, payload).await;
    insert_row(&mut tx,"osdeploy_lease_epochs",&json!({"acquisition_event_id":resume,"operation_id":h.operation,"run_id":h.run,
        "attempt_id":h.attempt,"executor_kind":"rust","generation":1,"worker_id":"reload-only-resumed-worker",
        "lease_token_sha256":token_hash,"acquired_at":resumed_at,"initial_expires_at":expires,"deadline_at":h.deadline,"purpose":"resume_evaluation"})).await.unwrap();
    let mut resumed_lease = old_lease;
    resumed_lease["worker_id"] = json!("reload-only-resumed-worker");
    resumed_lease["lease_token"] = json!(token);
    resumed_lease["acquired_at"] = json!(resumed_at);
    resumed_lease["heartbeat_at"] = json!(resumed_at);
    resumed_lease["lease_expires_at"] = json!(expires);
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT (jsonb_populate_record(NULL::rust_controller.worker_leases,$1::jsonb)).*").bind(resumed_lease).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    let resumed = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(resumed.state(), controller_domain::ExecutionState::Waiting);
    assert_eq!(resumed.next_check_at(), None);
    assert_eq!(resumed.revision(), 11);
    let evaluation = controller_domain::EventId::new();
    let mut payload = h.envelope(
        "evaluation_started",
        11,
        Value::Null,
        json!({"lease_acquisition_event_id":resume,"activity":"outcome_read"}),
    );
    payload["evaluated_at"] = json!(resumed_at);
    let mut tx = f.pool.begin().await.unwrap();
    h.decision(&mut tx, evaluation, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        13,
        "execution_state_changed",
        Some("running"),
        json!({"state":"running","decision_event_id":evaluation}),
        resumed_at,
    )
    .await;
    tx.commit().await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
    let starts:i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'").bind(h.operation.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!(
        starts, 1,
        "resuming the same attempt must not fabricate another start"
    );
}

#[tokio::test]
async fn ordinary_lease_reload_preserves_exact_terminal_residual_lease() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let expiry = controller_domain::EventId::new();
    let mut payload = h.envelope("activated_scope_expired",3,json!("unknown"),json!({"scope_key":"mutation_clone",
        "anchor_operation_id":h.operation,"anchor_event_id":h.activation,"deadline_at":h.deadline,
        "pe_complete_operation_id":null,"pe_complete_decision_event_id":null,"reason":"phase_deadline_expired"}));
    payload["evaluated_at"] = json!(h.deadline);
    let mut tx = f.pool.begin().await.unwrap();
    h.decision(&mut tx, expiry, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "execution_state_changed",
        Some("unknown"),
        json!({"state":"unknown","decision_event_id":expiry}),
        h.deadline,
    )
    .await;
    tx.commit().await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Unknown
    );
    let leases: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.worker_leases WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .fetch_one(&f.pool)
    .await
    .unwrap();
    assert_eq!(leases, 1);
    sqlx::query("UPDATE rust_controller.worker_leases SET worker_id='wrong-residual-owner' WHERE operation_id=$1").bind(h.operation.as_uuid()).execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_changed_binding_scope_epoch_and_aggregate_times() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    for (table, change, restore) in [
        (
            "osdeploy_attempt_bindings",
            "activated_at=activated_at+interval '1 second'",
            "activated_at=activated_at-interval '1 second'",
        ),
        (
            "osdeploy_deadlines",
            "opened_at=opened_at+interval '1 second',deadline_at=deadline_at+interval '1 second'",
            "opened_at=opened_at-interval '1 second',deadline_at=deadline_at-interval '1 second'",
        ),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at=initial_expires_at+interval '1 second'",
            "initial_expires_at=initial_expires_at-interval '1 second'",
        ),
        ("osdeploy_lease_epochs", "generation=2", "generation=1"),
    ] {
        assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
        f.corrupt_immutable(
            table,
            &format!("UPDATE rust_controller.{table} SET {change}"),
        )
        .await;
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{table}: {change}"
        );
        f.corrupt_immutable(
            table,
            &format!("UPDATE rust_controller.{table} SET {restore}"),
        )
        .await;
    }
    sqlx::query("UPDATE rust_controller.operations SET revision=revision+1 WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&f.pool)
        .await
        .unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_acquisition_without_its_atomic_state_event() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::query(
        "DELETE FROM rust_controller.journal_events WHERE operation_id=$1 AND aggregate_revision=3",
    )
    .bind(h.operation.as_uuid())
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query(
        "UPDATE rust_controller.operations SET revision=2,state='pending' WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.attempts SET state='pending' WHERE attempt_id=$1")
        .bind(h.attempt.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn evidence_restore_rejects_source_fence_hash_shape_missing_nulls_and_duplicates() {
    let f = Fixture::new().await;
    let source = reload_source();
    let h = ReloadHistory::with_plan(
        &f,
        osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
        }),
    )
    .await;
    let (_, event, _) = h.dispatched(&f, source).await;
    let original: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut cases = Vec::new();
    let mut v = original.clone();
    v["binding"]["evidence_fence"] = json!(7);
    cases.push(("changed fence", v));
    let mut v = original.clone();
    v["binding"]["run_id"] = json!(controller_domain::RunId::new());
    cases.push(("foreign run", v));
    let mut v = original.clone();
    v["binding"]["workflow_sha256"] = json!(h.workflow.to_uppercase());
    cases.push(("uppercase hash", v));
    let mut v = original.clone();
    v["source"] = json!("pve_api");
    cases.push(("unadmitted source", v));
    let mut v = original.clone();
    v.as_object_mut().unwrap().remove("receipt");
    cases.push(("missing required nullable", v));
    let mut v = original.clone();
    v["unexpected"] = json!(false);
    cases.push(("unknown root", v));
    for (label, v) in cases {
        replace_reload_evidence(&f, event, &v, None).await;
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{label}"
        );
        replace_reload_evidence(&f, event, &original, None).await;
        assert!(
            f.store.load_osdeploy_operation(h.operation).await.is_ok(),
            "restored {label}"
        );
    }
    let duplicate = String::from_utf8(event_journal::canonical_json_bytes(&original).unwrap())
        .unwrap()
        .replacen("\"source\":", "\"source\":\"fake_pve\",\"source\":", 1);
    replace_reload_evidence(&f, event, &original, Some(&duplicate)).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

async fn replace_reload_evidence(
    f: &Fixture,
    event: controller_domain::EventId,
    payload: &Value,
    text: Option<&str>,
) {
    let canonical =
        String::from_utf8(event_journal::canonical_json_bytes(payload).unwrap()).unwrap();
    let hash = event_journal::payload_digest(payload).unwrap();
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_canonical_json=$2,evidence_sha256=$3 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(text.unwrap_or(&canonical)).bind(&hash).execute(&mut *tx).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.journal_events SET payload=$2,payload_digest=$3 WHERE event_id=$1",
    )
    .bind(event.as_uuid())
    .bind(payload)
    .bind(hash)
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence ENABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
}

#[tokio::test]
async fn receipt_reload_keeps_original_evidence_fence_and_rejects_foreign_dispatch() {
    let f = Fixture::new().await;
    let source = reload_source();
    let p = osdeploy_support::altered(|v| {
        v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
    });
    let h = ReloadHistory::with_plan(&f, p).await;
    let (dispatch, evidence, original) = h.dispatched(&f, source).await;
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.dispatch(), Some(&original));
    assert_eq!(
        s.dispatch().unwrap().request().binding().evidence_fence(),
        6
    );
    let receipt = controller_domain::EventId::new();
    let at = h.at + chrono::Duration::seconds(1);
    let upid = "UPID:node-a:00000001:00000001:00000001:qmclone:900:root@pam:";
    let payload = json!({"contract_version":1,"action":"pve_receipt_captured","dispatch_event_id":dispatch,
        "request_sha256":original.request_sha256(),"receipt_kind":"task","upid":upid,"accepted_at":at});
    let mut tx = f.pool.begin().await.unwrap();
    h.event(
        &mut tx,
        receipt,
        9,
        "evidence_recorded",
        None,
        payload.clone(),
        at,
    )
    .await;
    insert_row(
        &mut tx,
        "osdeploy_pve_receipts",
        &json!({"operation_id":h.operation,"receipt_event_id":receipt,
        "receipt_kind":"task","upid":upid,"accepted_at":at,"recorded_at":at}),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let s = f.other.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.revision(), 9);
    assert_eq!(s.state(), controller_domain::ExecutionState::Running);
    assert_eq!(s.receipt().unwrap().dispatch(), &original);
    assert_eq!(s.receipt().unwrap().accepted_at(), at);
    assert_eq!(
        s.dispatch().unwrap().request().binding().evidence_fence(),
        6
    );
    // A plausible generic event is never promoted to the immutable typed index.
    f.corrupt_immutable("osdeploy_pve_evidence",&format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_revision=10 WHERE event_id='{}'",evidence.as_uuid())).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    f.corrupt_immutable("osdeploy_pve_evidence",&format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_revision=7 WHERE event_id='{}'",evidence.as_uuid())).await;
    let mut foreign = payload;
    foreign["dispatch_event_id"] = json!(controller_domain::EventId::new());
    sqlx::query(
        "UPDATE rust_controller.journal_events SET payload=$2,payload_digest=$3 WHERE event_id=$1",
    )
    .bind(receipt.as_uuid())
    .bind(&foreign)
    .bind(event_journal::payload_digest(&foreign).unwrap())
    .execute(&f.pool)
    .await
    .unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_dispatch_at_the_original_lease_expiry() {
    let f = Fixture::new().await;
    let source = reload_source();
    let h = ReloadHistory::with_plan(
        &f,
        osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
        }),
    )
    .await;
    let (event, _, _) = h.dispatched(&f, source).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut payload: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    let at = h.at + chrono::Duration::seconds(30);
    payload["evaluated_at"] = json!(at);
    payload["detail"]["dispatched_at"] = json!(at);
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql("ALTER TABLE rust_controller.osdeploy_pve_dispatches DISABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions DISABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.osdeploy_pve_dispatches SET dispatched_at=$2 WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .bind(at)
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.osdeploy_decisions SET evaluated_at=$2,payload_canonical_json=$3 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(at).bind(String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2,payload=$3,payload_digest=$4 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(at).bind(&payload).bind(event_journal::payload_digest(&payload).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE; ALTER TABLE rust_controller.osdeploy_pve_dispatches ENABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions ENABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn selected_terminal_decision_survives_later_cancellation_control() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let expiry = controller_domain::EventId::new();
    let cancellation = controller_domain::EventId::new();
    let mut tx = f.pool.begin().await.unwrap();
    let mut payload=h.envelope("activated_scope_expired",3,json!("unknown"),json!({"scope_key":"mutation_clone",
        "anchor_operation_id":h.operation,"anchor_event_id":h.activation,"deadline_at":h.deadline,
        "pe_complete_operation_id":null,"pe_complete_decision_event_id":null,"reason":"phase_deadline_expired"}));
    payload["evaluated_at"] = json!(h.deadline);
    h.decision(&mut tx, expiry, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "execution_state_changed",
        Some("unknown"),
        json!({"state":"unknown","decision_event_id":expiry}),
        h.deadline,
    )
    .await;
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    let mut payload = h.envelope(
        "run_cancelled",
        5,
        Value::Null,
        json!({"reason":"run_cancellation_requested"}),
    );
    payload["evaluated_at"] = json!(h.deadline);
    h.decision(&mut tx, cancellation, payload).await;
    insert_row(
        &mut tx,
        "osdeploy_run_cancellations",
        &json!({"run_id":h.run,"anchor_operation_id":h.operation,
        "decision_event_id":cancellation,"generation":1,"requested_at":h.deadline}),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.revision(), 6);
    assert!(s.cancelled());
    assert_eq!(s.state(), controller_domain::ExecutionState::Unknown);
    // Remove the selected decision index while keeping its journal fact;
    // the remaining control event must not stand in for the terminal proof.
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_decisions DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_decisions WHERE event_id=$1")
        .bind(expiry.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    // Drain this test-owned deletion's deferred FK checks before restoring its
    // immutable trigger; ALTER with pending constraint triggers is forbidden.
    sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE; ALTER TABLE rust_controller.osdeploy_decisions ENABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn declaration_and_bound_activation_reload_are_observations_without_writes() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let before = f.snapshot().await;
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.operation_id(), h.operation);
    assert_eq!(s.run_id(), h.run);
    assert_eq!(s.revision(), 3);
    assert_eq!(s.state(), controller_domain::ExecutionState::Leased);
    assert_eq!(s.attempt_id(), Some(h.attempt));
    assert_eq!(s.activated_at(), Some(h.at));
    assert_eq!(s.deadline_at(), Some(h.deadline));
    assert!(
        s.dispatch().is_none()
            && s.receipt().is_none()
            && s.next_check_at().is_none()
            && !s.cancelled()
    );
    let reg = f.store.load_osdeploy_registration(h.run).await.unwrap();
    for stage in osdeploy_adapter::OsDeployStage::ALL.into_iter().skip(1) {
        let s = f
            .other
            .load_osdeploy_operation(reg.ids().operation(stage))
            .await
            .unwrap();
        assert_eq!(s.state(), controller_domain::ExecutionState::Pending);
        assert_eq!(s.revision(), 0);
        assert!(s.attempt_id().is_none());
    }
    assert_eq!(before, f.snapshot().await);
}

#[tokio::test]
async fn reload_rejects_extra_attempt_and_changed_original_attempt_times() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    for statement in [
        "UPDATE rust_controller.attempts SET started_at=started_at+interval '1 second'",
        "UPDATE rust_controller.attempts SET deadline_at=deadline_at+interval '1 second'",
        "UPDATE rust_controller.attempts SET attempt_number=2",
    ] {
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::raw_sql(statement).execute(&mut *tx).await.unwrap();
        // Use the private transaction through its production public read by
        // committing corruption in this isolated fixture, then restore exactly.
        tx.commit().await.unwrap();
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{statement}"
        );
        sqlx::query(
            "UPDATE rust_controller.attempts SET started_at=$1,deadline_at=$2,attempt_number=1",
        )
        .bind(h.at)
        .bind(h.deadline)
        .execute(&f.pool)
        .await
        .unwrap();
    }
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,2,'pending')")
        .bind(controller_domain::AttemptId::new().as_uuid()).bind(h.operation.as_uuid()).execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_wrong_decision_kind_revision_hash_and_missing_index() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    for (field, value) in [
        ("event_kind", "'evidence_recorded'"),
        ("payload_digest", "repeat('a',64)"),
        ("observed_at", "observed_at+interval '1 second'"),
    ] {
        let original: Value = sqlx::query_scalar(
            "SELECT to_jsonb(e) FROM rust_controller.journal_events e WHERE event_id=$1",
        )
        .bind(h.activation.as_uuid())
        .fetch_one(&f.pool)
        .await
        .unwrap();
        sqlx::raw_sql(&format!(
            "UPDATE rust_controller.journal_events SET {field}={value} WHERE event_id='{}'",
            h.activation.as_uuid()
        ))
        .execute(&f.pool)
        .await
        .unwrap();
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{field}"
        );
        let replacement = original[field].as_str().unwrap();
        sqlx::raw_sql(&format!(
            "UPDATE rust_controller.journal_events SET {field}='{replacement}' WHERE event_id='{}'",
            h.activation.as_uuid()
        ))
        .execute(&f.pool)
        .await
        .unwrap();
    }
    f.corrupt_immutable(
        "osdeploy_decisions",
        &format!(
            "UPDATE rust_controller.osdeploy_decisions SET decision_revision=9 WHERE event_id='{}'",
            h.activation.as_uuid()
        ),
    )
    .await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    f.corrupt_immutable(
        "osdeploy_decisions",
        &format!(
            "UPDATE rust_controller.osdeploy_decisions SET decision_revision=1 WHERE event_id='{}'",
            h.activation.as_uuid()
        ),
    )
    .await;
    f.corrupt_immutable("osdeploy_decisions",&format!("UPDATE rust_controller.osdeploy_decisions SET stage_sha256=repeat('a',64) WHERE event_id='{}'",h.activation.as_uuid())).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn migration_is_additive_and_registration_stays_declaration_only() {
    let f = Fixture::new().await;
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_tables WHERE schemaname='rust_controller' AND tablename IN \
        ('osdeploy_decisions','osdeploy_deadlines','osdeploy_attempt_bindings','osdeploy_lease_epochs',\
        'osdeploy_pve_evidence','osdeploy_pve_dispatches','osdeploy_pve_receipts',\
        'osdeploy_run_cancellations','osdeploy_schedule_projection')",
    )
    .fetch_one(&f.pool)
    .await
    .unwrap();
    assert_eq!(count, 9);
    f.store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let before = f.snapshot().await;
    f.store.migrate().await.unwrap();
    assert_eq!(f.snapshot().await, before);
    f.assert_fresh_counts().await;
    for name in TABLES {
        let count: i64 =
            sqlx::query_scalar(&format!("SELECT count(*) FROM rust_controller.{name}"))
                .fetch_one(&f.pool)
                .await
                .unwrap();
        assert_eq!(count, 0, "{name}");
    }
}

// Deliberately SQL-local constraint inputs, never executable predecessor proofs.
// The real execution loader must reject these empty canonical payloads.
struct SchemaRows(Vec<(&'static str, Value)>);

impl SchemaRows {
    async fn new(f: &Fixture) -> Self {
        let run = controller_domain::RunId::new();
        f.store.enqueue_osdeploy(run, &plan()).await.unwrap();
        let operations: Vec<Uuid> = sqlx::query_scalar(
            "SELECT operation_id FROM rust_controller.osdeploy_operation_plans ORDER BY ordinal LIMIT 2",
        ).fetch_all(&f.pool).await.unwrap();
        let mut rows = Vec::new();
        for (index, operation) in operations.into_iter().enumerate() {
            let attempt = Uuid::now_v7();
            let events: Vec<Uuid> = (0..8).map(|_| Uuid::now_v7()).collect();
            let scope = ["mutation_clone", "mutation_disk_capacity"][index];
            sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'leased','2026-09-05T12:00:00Z','2026-09-05T12:01:00Z')")
                .bind(attempt).bind(operation).execute(&f.pool).await.unwrap();
            for (revision, event) in events.iter().enumerate() {
                sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,$7,'{}','2026-09-05T12:00:00Z')")
                    .bind(event).bind(operation).bind(attempt).bind((revision + 1) as i64)
                    .bind(format!("schema-only-{revision}")).bind(osdeploy_support::SHA)
                    .bind(if [2,4].contains(&revision) { "evidence_recorded" } else { "decision_recorded" })
                    .execute(&f.pool).await.unwrap();
            }
            for (revision, action, resolution) in [
                (1, "stage_activated", None),
                (2, "lease_acquired", None),
                (4, "pve_dispatch_committed", Some("ready")),
                (6, "pve_evaluated", Some("waiting")),
                (7, "run_cancelled", None),
            ] {
                rows.push(("osdeploy_decisions", json!({
                    "operation_id":operation,"decision_revision":revision,"event_id":events[revision-1],
                    "run_id":run.as_uuid(),"attempt_id":attempt,"action":action,"resolution":resolution,
                    "workflow_sha256":osdeploy_support::SHA,"stage_sha256":osdeploy_support::SHA,
                    "generation":1,"evaluated_at":"2026-09-05T12:00:00Z","payload_canonical_json":"{}"
                })));
            }
            rows.push(("osdeploy_deadlines",json!({"run_id":run.as_uuid(),"scope_key":scope,
                "anchor_operation_id":operation,"anchor_event_id":events[0],"opened_at":"2026-09-05T12:00:00Z",
                "budget_seconds":60,"deadline_at":"2026-09-05T12:01:00Z"})));
            rows.push(("osdeploy_attempt_bindings",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"scope_key":scope,"activation_event_id":events[0],
                "activated_at":"2026-09-05T12:00:00Z","deadline_at":"2026-09-05T12:01:00Z","activation_mode":"leased"})));
            rows.push(("osdeploy_lease_epochs",json!({"acquisition_event_id":events[1],"operation_id":operation,
                "run_id":run.as_uuid(),"attempt_id":attempt,"executor_kind":"rust","generation":1,"worker_id":"schema-test",
                "lease_token_sha256":if index == 0 { "a".repeat(64) } else { "b".repeat(64) },
                "acquired_at":"2026-09-05T12:00:00Z","initial_expires_at":"2026-09-05T12:00:30Z",
                "deadline_at":"2026-09-05T12:01:00Z","purpose":"initial_evaluation"})));
            rows.push(("osdeploy_pve_evidence",json!({"event_id":events[2],"operation_id":operation,
                "run_id":run.as_uuid(),"attempt_id":attempt,"evidence_revision":3,"evidence_sha256":osdeploy_support::SHA,
                "source":"fake_pve","evidence_canonical_json":"{}"})));
            rows.push(("osdeploy_pve_dispatches",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"dispatch_event_id":events[3],"dispatch_revision":4,"preflight_event_id":events[2],
                "workflow_sha256":osdeploy_support::SHA,"pve_plan_sha256":osdeploy_support::SHA,
                "request_sha256":osdeploy_support::SHA,"request_canonical_json":"{}","source":"fake_pve",
                "original_generation":1,"dispatched_at":"2026-09-05T12:00:00Z","lease_acquisition_event_id":events[1]})));
            rows.push(("osdeploy_pve_receipts",json!({"operation_id":operation,"receipt_event_id":events[4],
                "receipt_kind":"task","upid":"schema-only-upid","accepted_at":"2026-09-05T12:00:00Z","recorded_at":"2026-09-05T12:00:00Z"})));
            if index == 0 {
                rows.push(("osdeploy_run_cancellations",json!({"run_id":run.as_uuid(),"anchor_operation_id":operation,
                    "decision_event_id":events[6],"generation":1,"requested_at":"2026-09-05T12:00:00Z"})));
            }
            rows.push(("osdeploy_schedule_projection",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"mode":"waiting","basis_event_id":events[5],"basis_revision":6,"scope_key":scope,
                "next_check_at":"2026-09-05T12:00:02Z","unavailable_count":0,"rebuilt_through_revision":7})));
        }
        // All immediate typed targets precede references, including cross-operation test inputs.
        rows.sort_by_key(|(table, _)| TABLES.iter().position(|name| name == table).unwrap());
        Self(rows)
    }

    fn row(&self, table: &str) -> &Value {
        &self.0.iter().find(|(name, _)| *name == table).unwrap().1
    }

    async fn insert(&self, connection: &mut PgConnection) -> Result<(), sqlx::Error> {
        for (table, row) in &self.0 {
            insert_row(connection, table, row).await?;
        }
        sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE")
            .execute(connection)
            .await?;
        Ok(())
    }

    async fn reject(&self, f: &Fixture, table: &str, field: &str, value: Value, code: &str) {
        let mut changed = Self(self.0.clone());
        changed
            .0
            .iter_mut()
            .find(|(name, _)| *name == table)
            .unwrap()
            .1[field] = value;
        let mut tx = f.pool.begin().await.unwrap();
        let error = changed
            .insert(&mut tx)
            .await
            .expect_err(&format!("accepted {table}.{field}"));
        assert_sqlstate(&error, code, &format!("{table}.{field}"));
        tx.rollback().await.unwrap();
    }
}

async fn insert_row(
    connection: &mut PgConnection,
    table: &str,
    row: &Value,
) -> Result<(), sqlx::Error> {
    assert!(TABLES.contains(&table), "unowned schema input");
    sqlx::query(&format!("INSERT INTO rust_controller.{table} SELECT (jsonb_populate_record(NULL::rust_controller.{table},$1::jsonb)).*"))
        .bind(row).execute(connection).await?;
    Ok(())
}

fn assert_sqlstate(error: &sqlx::Error, expected: &str, context: &str) {
    assert_eq!(
        error.as_database_error().and_then(|e| e.code()).as_deref(),
        Some(expected),
        "{context}: {error}"
    );
}

#[tokio::test]
async fn immutable_tables_reject_update_delete_and_truncate_and_repair_each_trigger() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    let mut tx = f.pool.begin().await.unwrap();
    rows.insert(&mut tx).await.unwrap();
    tx.commit().await.unwrap();
    let before = f.snapshot().await;
    for table in &TABLES[..8] {
        let column = if *table == "osdeploy_deadlines" || *table == "osdeploy_run_cancellations" {
            "run_id"
        } else {
            "operation_id"
        };
        for statement in [
            format!("UPDATE rust_controller.{table} SET {column}={column}"),
            format!("DELETE FROM rust_controller.{table}"),
            format!("TRUNCATE rust_controller.{table} CASCADE"),
        ] {
            let error = sqlx::raw_sql(&statement)
                .execute(&f.pool)
                .await
                .unwrap_err();
            assert_sqlstate(&error, "23514", &statement);
            assert_eq!(
                error.as_database_error().unwrap().message(),
                "native durable record is immutable"
            );
        }
        for trigger in ["osdeploy_no_mutation", "osdeploy_no_truncate"] {
            sqlx::raw_sql(&format!(
                "DROP TRIGGER {trigger} ON rust_controller.{table}"
            ))
            .execute(&f.pool)
            .await
            .unwrap();
            f.store.migrate().await.unwrap();
            let triggers: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_trigger WHERE tgrelid=$1::regclass AND tgname IN ('osdeploy_no_mutation','osdeploy_no_truncate') AND NOT tgisinternal")
                .bind(format!("rust_controller.{table}")).fetch_one(&f.pool).await.unwrap();
            assert_eq!(triggers, 2, "{table}/{trigger}");
            let statement = if trigger == "osdeploy_no_mutation" {
                format!("UPDATE rust_controller.{table} SET {column}={column}")
            } else {
                format!("TRUNCATE rust_controller.{table} CASCADE")
            };
            let error = sqlx::raw_sql(&statement)
                .execute(&f.pool)
                .await
                .unwrap_err();
            assert_sqlstate(&error, "23514", &statement);
            assert_eq!(
                error.as_database_error().unwrap().message(),
                "native durable record is immutable"
            );
            assert_eq!(f.snapshot().await, before);
        }
    }
    // Scheduling is the single disposable mutable table.
    sqlx::query("UPDATE rust_controller.osdeploy_schedule_projection SET unavailable_count=1")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("TRUNCATE rust_controller.osdeploy_schedule_projection")
        .execute(&f.pool)
        .await
        .unwrap();
}

#[tokio::test]
async fn schema_rejects_foreign_operation_references_and_duplicate_bindings_and_dispatches() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, field, source, source_field) in [
        (
            "osdeploy_decisions",
            "event_id",
            "osdeploy_decisions",
            "event_id",
        ),
        (
            "osdeploy_decisions",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_deadlines",
            "anchor_event_id",
            "osdeploy_deadlines",
            "anchor_event_id",
        ),
        (
            "osdeploy_attempt_bindings",
            "activation_event_id",
            "osdeploy_attempt_bindings",
            "activation_event_id",
        ),
        (
            "osdeploy_lease_epochs",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_pve_evidence",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "dispatch_event_id",
            "osdeploy_pve_dispatches",
            "dispatch_event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "preflight_event_id",
            "osdeploy_pve_evidence",
            "event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "lease_acquisition_event_id",
            "osdeploy_lease_epochs",
            "acquisition_event_id",
        ),
        (
            "osdeploy_pve_receipts",
            "receipt_event_id",
            "osdeploy_pve_receipts",
            "receipt_event_id",
        ),
        (
            "osdeploy_run_cancellations",
            "decision_event_id",
            "osdeploy_decisions",
            "event_id",
        ),
        (
            "osdeploy_schedule_projection",
            "basis_event_id",
            "osdeploy_schedule_projection",
            "basis_event_id",
        ),
    ] {
        let other_operation = rows
            .0
            .iter()
            .rev()
            .find(|(name, _)| *name == "osdeploy_attempt_bindings")
            .unwrap()
            .1["operation_id"]
            .clone();
        let other = rows
            .0
            .iter()
            .find(|(name, row)| {
                *name == source
                    && (row["operation_id"] == other_operation
                        || row["anchor_operation_id"] == other_operation)
            })
            .unwrap()
            .1[source_field]
            .clone();
        rows.reject(&f, table, field, other, "23503").await;
    }
    for table in ["osdeploy_attempt_bindings", "osdeploy_pve_dispatches"] {
        let mut tx = f.pool.begin().await.unwrap();
        rows.insert(&mut tx).await.unwrap();
        let error = insert_row(&mut tx, table, rows.row(table))
            .await
            .unwrap_err();
        assert_sqlstate(&error, "23505", table);
        tx.rollback().await.unwrap();
    }
    let generic_event: Uuid = sqlx::query_scalar("SELECT event_id FROM rust_controller.journal_events WHERE operation_id=$1 AND aggregate_revision=8")
        .bind(Uuid::parse_str(rows.row("osdeploy_pve_dispatches")["operation_id"].as_str().unwrap()).unwrap())
        .fetch_one(&f.pool).await.unwrap();
    for field in ["preflight_event_id", "lease_acquisition_event_id"] {
        // A same-operation generic journal event cannot replace typed evidence or an epoch.
        rows.reject(
            &f,
            "osdeploy_pve_dispatches",
            field,
            json!(generic_event),
            "23503",
        )
        .await;
    }
    for table in TABLES
        .into_iter()
        .filter(|table| *table != "osdeploy_pve_receipts")
    {
        rows.reject(&f, table, "run_id", json!(Uuid::now_v7()), "23503")
            .await;
    }
}

#[tokio::test]
async fn schema_closes_actions_resolutions_and_attempt_nullability() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (action, resolution, nullable) in [
        ("stage_activated", None, false),
        ("lease_acquired", None, false),
        ("evaluation_started", None, false),
        ("lease_renewed", None, false),
        ("pve_dispatch_committed", Some("ready"), false),
        ("pve_evaluated", Some("waiting"), false),
        ("lease_reclaimed_same_attempt", None, false),
        ("evaluation_reparked", Some("waiting"), false),
        ("scope_expired_before_activation", Some("unknown"), true),
        ("activated_scope_expired", Some("unknown"), false),
        ("run_cancelled", None, true),
        ("stage_cancelled_unexposed", Some("blocked"), true),
        ("stage_cancelled_exposed", Some("unknown"), false),
        ("residual_lease_revoked", None, false),
        ("reconciliation_scheduled", None, false),
        ("lease_expired_uncertain", Some("unknown"), false),
    ] {
        for attempt_null in [false, true] {
            for candidate_resolution in [
                None,
                Some("ready"),
                Some("waiting"),
                Some("satisfied"),
                Some("failed"),
                Some("blocked"),
                Some("unknown"),
                Some("conflicted"),
            ] {
                let mut row = rows.row("osdeploy_decisions").clone();
                row["action"] = json!(action);
                row["resolution"] = json!(candidate_resolution);
                if attempt_null {
                    row["attempt_id"] = Value::Null;
                }
                let resolution_valid = if action == "pve_evaluated" {
                    candidate_resolution.is_some_and(|v| v != "ready")
                } else {
                    candidate_resolution == resolution
                };
                let attempt_valid = if action == "scope_expired_before_activation" {
                    attempt_null
                } else {
                    !attempt_null || nullable
                };
                let mut tx = f.pool.begin().await.unwrap();
                let result = insert_row(&mut tx, "osdeploy_decisions", &row).await;
                if resolution_valid && attempt_valid {
                    result.unwrap();
                } else {
                    assert_sqlstate(&result.unwrap_err(), "23514", action);
                }
                tx.rollback().await.unwrap();
            }
        }
    }
    for action in ["grace_wait_activated", "pve_receipt_captured", "arbitrary"] {
        rows.reject(&f, "osdeploy_decisions", "action", json!(action), "23514")
            .await;
    }
}

#[tokio::test]
async fn schema_rejects_invalid_scope_hash_counter_time_and_payload_rows() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, field, value) in [
        ("osdeploy_decisions", "decision_revision", json!(0)),
        ("osdeploy_decisions", "generation", json!(0)),
        (
            "osdeploy_decisions",
            "workflow_sha256",
            json!("A".repeat(64)),
        ),
        ("osdeploy_decisions", "stage_sha256", json!("bad")),
        ("osdeploy_decisions", "resolution", json!("pending")),
        ("osdeploy_deadlines", "scope_key", json!("arbitrary")),
        ("osdeploy_deadlines", "budget_seconds", json!(0)),
        ("osdeploy_deadlines", "budget_seconds", json!(86401)),
        (
            "osdeploy_deadlines",
            "deadline_at",
            json!("2026-09-05T12:01:01Z"),
        ),
        (
            "osdeploy_attempt_bindings",
            "activation_mode",
            json!("resumed"),
        ),
        (
            "osdeploy_attempt_bindings",
            "deadline_at",
            json!("2026-09-05T12:00:00Z"),
        ),
        ("osdeploy_lease_epochs", "generation", json!(0)),
        ("osdeploy_lease_epochs", "executor_kind", json!("python")),
        ("osdeploy_lease_epochs", "worker_id", json!("   ")),
        (
            "osdeploy_lease_epochs",
            "lease_token_sha256",
            json!("raw-token"),
        ),
        ("osdeploy_lease_epochs", "purpose", json!("send")),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at",
            json!("2026-09-05T12:00:00Z"),
        ),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at",
            json!("2026-09-05T12:01:01Z"),
        ),
        ("osdeploy_pve_evidence", "evidence_revision", json!(0)),
        ("osdeploy_pve_evidence", "source", json!("real_pve")),
        ("osdeploy_pve_evidence", "evidence_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "dispatch_revision", json!(0)),
        ("osdeploy_pve_dispatches", "original_generation", json!(0)),
        ("osdeploy_pve_dispatches", "source", json!("real_pve")),
        ("osdeploy_pve_dispatches", "workflow_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "pve_plan_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "request_sha256", json!("bad")),
        ("osdeploy_pve_receipts", "receipt_kind", json!("other")),
        ("osdeploy_pve_receipts", "upid", Value::Null),
        ("osdeploy_pve_receipts", "upid", json!("  ")),
        (
            "osdeploy_pve_receipts",
            "receipt_kind",
            json!("synchronous"),
        ),
        (
            "osdeploy_pve_receipts",
            "recorded_at",
            json!("2026-09-05T11:59:59Z"),
        ),
        ("osdeploy_run_cancellations", "generation", json!(0)),
        ("osdeploy_schedule_projection", "mode", json!("execute")),
        ("osdeploy_schedule_projection", "basis_revision", json!(0)),
        (
            "osdeploy_schedule_projection",
            "rebuilt_through_revision",
            json!(0),
        ),
        (
            "osdeploy_schedule_projection",
            "unavailable_count",
            json!(-1),
        ),
        (
            "osdeploy_schedule_projection",
            "unavailable_count",
            json!(5),
        ),
        ("osdeploy_schedule_projection", "next_check_at", Value::Null),
    ] {
        rows.reject(&f, table, field, value, "23514").await;
    }
    for (table, field, limit) in [
        ("osdeploy_decisions", "payload_canonical_json", 65536),
        ("osdeploy_pve_evidence", "evidence_canonical_json", 1048576),
        ("osdeploy_pve_dispatches", "request_canonical_json", 1048576),
    ] {
        for value in [
            json!("[]"),
            json!("null"),
            json!(format!("{{\"x\":\"{}\"}}", "é".repeat(limit / 2))),
        ] {
            rows.reject(&f, table, field, value, "23514").await;
        }
        rows.reject(&f, table, field, json!("{invalid"), "22P02")
            .await;
        // Exactly the byte limit is admitted; non-ASCII oversize above tests octets, not characters.
        let mut exact = SchemaRows(rows.0.clone());
        exact
            .0
            .iter_mut()
            .find(|(name, _)| *name == table)
            .unwrap()
            .1[field] = json!(format!("{{\"x\":\"{}\"}}", "x".repeat(limit - 8)));
        let mut tx = f.pool.begin().await.unwrap();
        exact.insert(&mut tx).await.unwrap();
        tx.rollback().await.unwrap();
    }
}

#[tokio::test]
async fn typed_decision_constraints_are_deferred_but_journal_targets_are_immediate() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, constraint, event_field) in [
        (
            "osdeploy_deadlines",
            "osdeploy_deadline_anchor_decision_fk",
            "anchor_event_id",
        ),
        (
            "osdeploy_attempt_bindings",
            "osdeploy_binding_activation_decision_fk",
            "activation_event_id",
        ),
        (
            "osdeploy_lease_epochs",
            "osdeploy_epoch_acquisition_decision_fk",
            "acquisition_event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "osdeploy_dispatch_decision_fk",
            "dispatch_event_id",
        ),
        (
            "osdeploy_run_cancellations",
            "osdeploy_cancellation_decision_fk",
            "decision_event_id",
        ),
        (
            "osdeploy_schedule_projection",
            "osdeploy_schedule_basis_decision_fk",
            "basis_event_id",
        ),
    ] {
        let flags: (bool,bool) = sqlx::query_as("SELECT condeferrable,condeferred FROM pg_constraint WHERE connamespace='rust_controller'::regnamespace AND conname=$1")
            .bind(constraint).fetch_one(&f.pool).await.unwrap();
        assert_eq!(flags, (true, true), "{constraint}");
        let mut tx = f.pool.begin().await.unwrap();
        let target = rows.row(table)[event_field].clone();
        let decision = rows
            .0
            .iter()
            .find(|(name, row)| *name == "osdeploy_decisions" && row["event_id"] == target)
            .unwrap();
        for (name, row) in &rows.0 {
            if *name == "osdeploy_decisions" && row["event_id"] == target {
                continue;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        let error = sqlx::raw_sql(&format!(
            "SET CONSTRAINTS rust_controller.{constraint} IMMEDIATE"
        ))
        .execute(&mut *tx)
        .await
        .unwrap_err();
        assert_sqlstate(&error, "23503", constraint);
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        for (name, row) in &rows.0 {
            if *name == "osdeploy_decisions" && row["event_id"] == target {
                continue;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        insert_row(&mut tx, decision.0, &decision.1).await.unwrap();
        sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE")
            .execute(&mut *tx)
            .await
            .unwrap();
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        for (name, row) in &rows.0 {
            if *name == table {
                let mut missing_event = row.clone();
                missing_event[event_field] = json!(Uuid::now_v7());
                let error = insert_row(&mut tx, name, &missing_event).await.unwrap_err();
                assert_sqlstate(&error, "23503", "journal target must exist at insertion");
                break;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        tx.rollback().await.unwrap();
    }
}

async fn assert_clean_before_durability(f: &Fixture, pid: i32, obstruction: bool) {
    let mut connection = tokio::time::timeout(Duration::from_secs(5), f.pool.acquire())
        .await
        .expect("primary reacquisition timed out")
        .unwrap();
    let (actual_pid, unassigned, answer): (i32, bool, i32) =
        sqlx::query_as("SELECT pg_backend_pid(),txid_current_if_assigned() IS NULL,40+2")
            .fetch_one(&mut *connection)
            .await
            .unwrap();
    assert_eq!(actual_pid, pid, "migration replaced the primary backend");
    assert!(unassigned, "untracked transaction retained");
    assert_eq!(answer, 42);
    let tables: Vec<String> = sqlx::query_scalar("SELECT tablename FROM pg_tables WHERE schemaname='rust_controller' AND tablename=ANY($1) ORDER BY tablename")
        .bind(TABLES.as_slice()).fetch_all(&mut *connection).await.unwrap();
    assert_eq!(
        tables,
        if obstruction {
            vec!["osdeploy_pve_dispatches"]
        } else {
            vec![]
        }
    );
    let authority: (String, i64) = sqlx::query_as(
        "SELECT executor_kind,generation FROM rust_controller.orchestration_authority",
    )
    .fetch_one(&mut *connection)
    .await
    .unwrap();
    assert_eq!(authority, ("rust".into(), 1));
    let runs: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_runs")
        .fetch_one(&mut *connection)
        .await
        .unwrap();
    assert_eq!(runs, 0);
}

#[tokio::test]
async fn migration_failure_rolls_back_and_reuses_clean_connection() {
    let f = Fixture::new_before_durability().await;
    let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
        .fetch_one(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE TABLE rust_controller.osdeploy_pve_dispatches(broken integer)")
        .execute(&f.pool)
        .await
        .unwrap();
    let error = tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap_err();
    assert!(
        error.to_string().contains("operation_id"),
        "unexpected migration failure: {error}"
    );
    assert_clean_before_durability(&f, pid, true).await;
    sqlx::raw_sql("DROP TABLE rust_controller.osdeploy_pve_dispatches")
        .execute(&f.pool)
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f.snapshot().await["osdeploy_decisions"], json!([]));
}

#[tokio::test]
async fn cancelled_migration_rolls_back_and_reuses_clean_connection() {
    let f = Fixture::new_before_durability().await;
    let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
        .fetch_one(&f.pool)
        .await
        .unwrap();
    let observer = tokio::time::timeout(
        Duration::from_secs(5),
        PgPoolOptions::new()
            .max_connections(1)
            .acquire_timeout(Duration::from_secs(1))
            .connect_with((*f.pool.connect_options()).clone()),
    )
    .await
    .expect("observer connection timed out")
    .unwrap();
    let mut barrier = observer.acquire().await.unwrap();
    let nonce = Uuid::now_v7();
    let key = i64::from_be_bytes(nonce.as_bytes()[8..16].try_into().unwrap());
    let name = format!("durability_barrier_{}", nonce.simple());
    tokio::time::timeout(
        Duration::from_secs(5),
        sqlx::query("SELECT pg_advisory_lock($1)")
            .bind(key)
            .execute(&mut *barrier),
    )
    .await
    .expect("owned barrier acquisition timed out")
    .unwrap();
    sqlx::raw_sql(&format!("CREATE FUNCTION rust_controller.{name}() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE command_tag='CREATE TABLE' AND object_identity='rust_controller.osdeploy_decisions') THEN PERFORM pg_advisory_xact_lock({key}); END IF; END $$; CREATE EVENT TRIGGER {name} ON ddl_command_end WHEN TAG IN ('CREATE TABLE') EXECUTE FUNCTION rust_controller.{name}();"))
        .execute(&f.pool).await.unwrap();
    let mut migration = Box::pin(f.store.migrate());
    tokio::select! {
        result = &mut migration => panic!("migration passed its barrier: {result:?}"),
        reached = tokio::time::timeout(Duration::from_secs(5),async {
            loop {
                let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a USING(pid) WHERE l.pid=$1 AND l.locktype='advisory' AND NOT l.granted AND l.classid=$2::bigint::oid AND l.objid=$3::bigint::oid AND l.objsubid=1 AND a.wait_event='advisory' AND a.query LIKE '%CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_decisions%')")
                    .bind(pid).bind(((key as u64)>>32) as i64).bind(((key as u64)&0xffff_ffff) as i64)
                    .fetch_one(&mut *barrier).await.unwrap();
                if waiting { break; }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }) => reached.expect("exact 0005 advisory barrier not reached")
    }
    let waiting: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted",
    )
    .bind(pid)
    .fetch_one(&mut *barrier)
    .await
    .unwrap();
    assert_eq!(waiting, 1, "0005 barrier not reached");
    drop(migration);
    // Release before pool acquisition so SQLx's queued rollback can finish.
    let unlocked: bool = tokio::time::timeout(
        Duration::from_secs(5),
        sqlx::query_scalar("SELECT pg_advisory_unlock($1)")
            .bind(key)
            .fetch_one(&mut *barrier),
    )
    .await
    .expect("owned barrier release timed out")
    .unwrap();
    assert!(unlocked);
    assert_clean_before_durability(&f, pid, false).await;
    sqlx::raw_sql(&format!(
        "DROP EVENT TRIGGER {name}; DROP FUNCTION rust_controller.{name}();"
    ))
    .execute(&f.pool)
    .await
    .unwrap();
    tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f.snapshot().await["osdeploy_decisions"], json!([]));
    drop(barrier);
    observer.close().await;
}
