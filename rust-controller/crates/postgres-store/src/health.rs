use crate::{AuthoritySnapshot, ExecutorKind, PgStore, StoreError};
use chrono::{DateTime, Utc};
use sqlx::Row;

/// Aggregate facts only: no connection, credentials, identifiers or payloads.
#[derive(Clone, Debug)]
pub struct StoreHealthSnapshot {
    pub observed_at: DateTime<Utc>,
    pub authority: Option<AuthoritySnapshot>,
    pub active_leases: i64,
    pub expired_leases: i64,
    pub oldest_pending_age_seconds: Option<i64>,
    pub outbox_pending: i64,
    pub oldest_outbox_age_seconds: Option<i64>,
    pub blocked: i64,
    pub unknown: i64,
    pub conflicted: i64,
}

impl PgStore {
    /// One statement gives a consistent read-only MVCC snapshot, including an
    /// absent authority singleton. Missing schema/permissions fail the query.
    pub async fn health_snapshot(&self) -> Result<StoreHealthSnapshot, StoreError> {
        let row = sqlx::query(r#"
            SELECT clock_timestamp() AS observed_at,
                (SELECT executor_kind FROM rust_controller.orchestration_authority WHERE singleton_key=1) AS executor,
                (SELECT generation FROM rust_controller.orchestration_authority WHERE singleton_key=1) AS generation,
                (SELECT count(*) FROM rust_controller.worker_leases WHERE lease_expires_at > clock_timestamp()) AS active_leases,
                (SELECT count(*) FROM rust_controller.worker_leases WHERE lease_expires_at <= clock_timestamp()) AS expired_leases,
                (SELECT floor(extract(epoch FROM clock_timestamp()-min(created_at)))::bigint FROM rust_controller.operations WHERE state='pending') AS pending_age,
                (SELECT count(*) FROM rust_controller.outbox WHERE delivered_at IS NULL) AS outbox_pending,
                (SELECT floor(extract(epoch FROM clock_timestamp()-min(created_at)))::bigint FROM rust_controller.outbox WHERE delivered_at IS NULL) AS outbox_age,
                (SELECT count(*) FROM rust_controller.operations WHERE state='blocked') AS blocked,
                (SELECT count(*) FROM rust_controller.operations WHERE state='unknown') AS unknown,
                (SELECT count(*) FROM rust_controller.operations WHERE state='conflicted') AS conflicted
        "#).fetch_one(self.pool()).await?;
        let executor: Option<String> = row.try_get("executor")?;
        let generation: Option<i64> = row.try_get("generation")?;
        let authority = executor
            .as_deref()
            .and_then(ExecutorKind::from_persisted)
            .zip(generation)
            .map(|(executor, generation)| AuthoritySnapshot::new(executor, generation));
        Ok(StoreHealthSnapshot {
            observed_at: row.try_get("observed_at")?,
            authority,
            active_leases: row.try_get("active_leases")?,
            expired_leases: row.try_get("expired_leases")?,
            oldest_pending_age_seconds: row.try_get("pending_age")?,
            outbox_pending: row.try_get("outbox_pending")?,
            oldest_outbox_age_seconds: row.try_get("outbox_age")?,
            blocked: row.try_get("blocked")?,
            unknown: row.try_get("unknown")?,
            conflicted: row.try_get("conflicted")?,
        })
    }
}
