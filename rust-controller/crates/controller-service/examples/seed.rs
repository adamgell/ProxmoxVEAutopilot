//! Disposable Compose bootstrap only. Fixed literal loopback target; no options.
use api_compat::{JobEnvelope, normalize_job};
use controller_domain::{CommandEnvelope, OperationId, RunId, SemanticOperationKey, WorkflowKind};
use postgres_store::PgStore;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let pool = sqlx::PgPool::connect(
        "postgresql://postgres:local-proof@127.0.0.1:5432/rust_controller_test",
    )
    .await?;
    let store = PgStore::new(pool.clone());
    if std::env::args().nth(1).as_deref() == Some("cancel") {
        let id: String = sqlx::query_scalar("SELECT operation_id::text FROM rust_controller.operations WHERE state='running' ORDER BY created_at LIMIT 1").fetch_one(&pool).await?;
        let operation: OperationId = serde_json::from_value(serde_json::Value::String(id))?;
        scheduler::Scheduler::new(store, scheduler::ExecutorKind::Rust, 1, "compose-operator")?
            .request_cancel(operation)
            .await?;
        println!("requested fenced cancellation");
        return Ok(());
    }
    store.migrate().await?;
    sqlx::raw_sql("INSERT INTO rust_controller.orchestration_authority (singleton_key, executor_kind, generation, change_reference) VALUES (1,'rust',1,'disposable-compose'); CREATE ROLE observer_login LOGIN PASSWORD 'local-observe'; GRANT USAGE ON SCHEMA rust_controller TO observer_login; GRANT SELECT ON ALL TABLES IN SCHEMA rust_controller TO observer_login; ALTER ROLE observer_login SET default_transaction_read_only=on; CREATE TABLE jobs (id text, job_type text, playbook text, cmd_json jsonb, args_json jsonb, status text, created_at timestamptz DEFAULT clock_timestamp()); GRANT SELECT ON jobs TO observer_login;").execute(&pool).await?;
    let raw = include_str!("../../../fixtures/jobs/synthetic-long-sleep.json");
    let job = JobEnvelope::from_json_str(raw)?;
    let plan = normalize_job(&job)?;
    let reordered: serde_json::Value = serde_json::from_str(raw)?;
    assert_eq!(
        plan.fingerprint()?.as_hex(),
        normalize_job(&JobEnvelope::from_json_str(&serde_json::to_string(
            &reordered
        )?)?)?
        .fingerprint()?
        .as_hex()
    );
    // Independent golden from the accepted Python/Rust normalization contract.
    assert!(plan.fingerprint()?.as_hex().starts_with("d53da5428e15"));
    for index in 0..6 {
        let command = CommandEnvelope::new(
            format!("compose:{index}"),
            SemanticOperationKey::new(
                WorkflowKind::SyntheticLongSleep,
                RunId::new(),
                format!("compose-{index}"),
                1,
            )?,
            plan.fingerprint()?.as_hex(),
        )?;
        store.append_command(OperationId::new(), &command).await?;
    }
    // A different plan must remain pending throughout the proof.
    let command = CommandEnvelope::new(
        "compose:unrelated",
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "unrelated",
            1,
        )?,
        "b".repeat(64),
    )?;
    store.append_command(OperationId::new(), &command).await?;
    println!(
        "seeded six bound synthetic operations and one unrelated operation; fingerprint stable"
    );
    Ok(())
}
