#![cfg(feature = "fixture-ipc")]
use postgres_store::{OsDeployExecutionError, PgStore};
use uuid::Uuid;

#[tokio::test]
async fn nil_result_session_is_rejected_without_database_io() {
    let store = PgStore::new(
        sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgres://localhost:1/not_used")
            .unwrap(),
    );
    assert_eq!(
        store.probe_pe_register_result_lock(Uuid::nil()).await,
        Err(OsDeployExecutionError::Validation)
    );
}

#[tokio::test]
#[ignore = "requires explicit empty loopback PVA_START_PE_SCHEMA_TEST_DSN; no Docker launcher"]
async fn concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock() {
    let dsn =
        std::env::var("PVA_START_PE_SCHEMA_TEST_DSN").expect("explicit isolated database required");
    let options: sqlx::postgres::PgConnectOptions = dsn.parse().unwrap();
    assert!(matches!(
        options.get_host(),
        "127.0.0.1" | "localhost" | "::1"
    ));
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(3)
        .connect_with(options)
        .await
        .unwrap();
    let empty: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='rust_controller')",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(empty);
    let store = PgStore::new(pool.clone());
    let session = Uuid::now_v7();
    let key = format!("osdeploy:pe-register-result:{session}");
    let mut original = pool.begin().await.unwrap();
    let locked: bool =
        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))")
            .bind(&key)
            .fetch_one(&mut *original)
            .await
            .unwrap();
    assert!(locked);
    assert_eq!(
        store.probe_pe_register_result_lock(session).await,
        Err(OsDeployExecutionError::Conflict)
    );
    original.rollback().await.unwrap();
    assert_eq!(
        store.probe_pe_register_result_lock(session).await,
        Err(OsDeployExecutionError::CapabilityUnavailable)
    );
    let mut after = pool.begin().await.unwrap();
    let released: bool =
        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))")
            .bind(&key)
            .fetch_one(&mut *after)
            .await
            .unwrap();
    assert!(released);
    after.rollback().await.unwrap();
    let empty: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='rust_controller')",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(empty);
}
