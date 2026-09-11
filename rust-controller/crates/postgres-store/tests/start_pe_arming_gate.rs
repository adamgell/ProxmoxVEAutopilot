#![cfg(feature = "fixture-ipc")]
use osdeploy_adapter::*;
use postgres_store::{OsDeployExecutionError, PgStore};
use uuid::Uuid;

#[tokio::test]
async fn unavailable_witness_refuses_before_connection_or_transaction() {
    let store = PgStore::new(
        sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgres://localhost:1/not_used")
            .unwrap(),
    );
    let context = StartPeArmingContextV1 {
        run_id: Uuid::from_u128(1),
        start_operation: Uuid::from_u128(2),
        registration_operation: Uuid::from_u128(3),
        attempt: Uuid::from_u128(4),
        generation: Uuid::from_u128(5),
        owner: Uuid::from_u128(6),
        session_id: Uuid::from_u128(7),
        request_sha256: "a".repeat(64),
    };
    let anchor =
        PeRegistrationAnchorV1::new(context.start_operation, Uuid::from_u128(8), 1000, 60).unwrap();
    let proposal = StartPeAtomicArmingProposalV1::new(context, anchor).unwrap();
    assert!(matches!(
        store
            .arm_osdeploy_start_pe_session(&proposal, AuthenticatedPeWitnessV1::Unavailable)
            .await,
        Err(OsDeployExecutionError::CapabilityUnavailable)
    ));
}

/// Explicit opt-in isolated local PostgreSQL; no Docker launcher or production DSN.
#[tokio::test]
#[ignore = "requires PVA_START_PE_SCHEMA_TEST_DSN for an empty isolated loopback database"]
async fn session_schema_creation_rolls_back_without_leaving_tables() {
    let dsn =
        std::env::var("PVA_START_PE_SCHEMA_TEST_DSN").expect("explicit isolated database required");
    let options: sqlx::postgres::PgConnectOptions = dsn.parse().unwrap();
    assert!(matches!(
        options.get_host(),
        "127.0.0.1" | "localhost" | "::1"
    ));
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect_with(options)
        .await
        .unwrap();
    let before: Option<String> =
        sqlx::query_scalar("SELECT to_regnamespace('rust_controller')::text")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(
        before.is_none(),
        "only empty isolated databases are allowed"
    );
    let mut tx = pool.begin().await.unwrap();
    for sql in [
        include_str!("../migrations/0001_foundation.sql"),
        include_str!("../migrations/0002_scheduler.sql"),
        include_str!("../migrations/0003_native_pve.sql"),
        include_str!("../migrations/0004_osdeploy_registration.sql"),
        include_str!("../migrations/0005_osdeploy_durability.sql"),
        postgres_store::START_PE_SESSION_SCHEMA_V1,
    ] {
        sqlx::raw_sql(sql).execute(&mut *tx).await.unwrap();
    }
    let exists: Option<String> = sqlx::query_scalar(
        "SELECT to_regclass('rust_controller.osdeploy_start_pe_session_proposals')::text",
    )
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert!(exists.is_some());
    tx.rollback().await.unwrap();
    let after: Option<String> =
        sqlx::query_scalar("SELECT to_regnamespace('rust_controller')::text")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(after.is_none());
}
