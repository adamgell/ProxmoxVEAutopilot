use axum::{Json, Router, extract::State, http::StatusCode, routing::get};
use chrono::{DateTime, Utc};
use postgres_store::{PgStore, StoreHealthSnapshot};
use serde::Serialize;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::RwLock;

#[derive(Clone, Default)]
pub struct Progress {
    pub last_success: Option<(Instant, DateTime<Utc>)>,
    pub successful_sweeps: u64,
    pub last_sweep_ok: bool,
}
impl Progress {
    pub fn succeeded(&mut self) {
        self.last_success = Some((Instant::now(), Utc::now()));
        self.successful_sweeps = self.successful_sweeps.saturating_add(1);
        self.last_sweep_ok = true;
    }
}

#[derive(Clone)]
pub struct HealthState {
    pub store: PgStore,
    pub progress: Arc<RwLock<Progress>>,
    pub mode: &'static str,
    pub generation: i64,
    pub adapter_versions: Vec<&'static str>,
}

#[derive(Serialize)]
pub struct ReadyResponse {
    version: &'static str,
    git_sha: &'static str,
    mode: &'static str,
    executor_kind: Option<&'static str>,
    authority_generation: Option<i64>,
    configured_generation: i64,
    database: bool,
    outbox: bool,
    active_leases: Option<i64>,
    expired_leases: Option<i64>,
    oldest_pending_age_seconds: Option<i64>,
    outbox_pending: Option<i64>,
    oldest_outbox_age_seconds: Option<i64>,
    database_observed_at: Option<DateTime<Utc>>,
    reconciler_last_success: Option<DateTime<Utc>>,
    successful_sweeps: u64,
    reconciler_fresh: bool,
    adapter_versions: Vec<&'static str>,
    pve_transport: &'static str,
    pve_evidence: &'static str,
    blocked: Option<i64>,
    unknown: Option<i64>,
    conflicted: Option<i64>,
    ready: bool,
}

fn evaluate(
    state: &HealthState,
    snapshot: Option<&StoreHealthSnapshot>,
    authority: Option<(&'static str, i64)>,
    progress: &Progress,
    transport: &'static str,
) -> ReadyResponse {
    let outbox = snapshot.is_some_and(|s| {
        s.outbox_pending <= 1000 && s.oldest_outbox_age_seconds.is_none_or(|age| age <= 300)
    });
    let fresh = progress.last_sweep_ok
        && progress
            .last_success
            .is_some_and(|(time, _)| time.elapsed() <= Duration::from_secs(15));
    let ready = snapshot.is_some()
        && outbox
        && fresh
        && transport == "fake"
        && authority.is_some_and(|(executor, generation)| {
            executor == "rust" && generation == state.generation
        });
    ReadyResponse {
        version: env!("CARGO_PKG_VERSION"),
        git_sha: env!("CONTROLLER_GIT_SHA"),
        mode: state.mode,
        executor_kind: authority.map(|a| a.0),
        authority_generation: authority.map(|a| a.1),
        configured_generation: state.generation,
        database: snapshot.is_some(),
        outbox,
        active_leases: snapshot.map(|s| s.active_leases),
        expired_leases: snapshot.map(|s| s.expired_leases),
        oldest_pending_age_seconds: snapshot.and_then(|s| s.oldest_pending_age_seconds),
        outbox_pending: snapshot.map(|s| s.outbox_pending),
        oldest_outbox_age_seconds: snapshot.and_then(|s| s.oldest_outbox_age_seconds),
        database_observed_at: snapshot.map(|s| s.observed_at),
        reconciler_last_success: progress.last_success.map(|(_, utc)| utc),
        successful_sweeps: progress.successful_sweeps,
        reconciler_fresh: fresh,
        adapter_versions: state.adapter_versions.clone(),
        pve_transport: transport,
        pve_evidence: "synthetic_no_device_readiness",
        blocked: snapshot.map(|s| s.blocked),
        unknown: snapshot.map(|s| s.unknown),
        conflicted: snapshot.map(|s| s.conflicted),
        ready,
    }
}

pub fn router(state: HealthState) -> Router {
    Router::new()
        .route("/healthz", get(liveness))
        .route("/readyz", get(readiness))
        .with_state(state)
}
async fn liveness() -> Json<serde_json::Value> {
    Json(
        serde_json::json!({"alive":true,"version":env!("CARGO_PKG_VERSION"),"git_sha":env!("CONTROLLER_GIT_SHA")}),
    )
}
async fn readiness(State(state): State<HealthState>) -> (StatusCode, Json<ReadyResponse>) {
    let snapshot = tokio::time::timeout(Duration::from_secs(2), state.store.health_snapshot())
        .await
        .ok()
        .and_then(Result::ok);
    let progress = state.progress.read().await;
    let authority = snapshot
        .as_ref()
        .and_then(|s| s.authority)
        .map(|a| (a.executor_kind().as_str(), a.generation()));
    response(evaluate(
        &state,
        snapshot.as_ref(),
        authority,
        &progress,
        "fake",
    ))
}
fn response(body: ReadyResponse) -> (StatusCode, Json<ReadyResponse>) {
    (
        if body.ready {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        Json(body),
    )
}

#[cfg(test)]
fn test_router(
    database: bool,
    authority: Option<i64>,
    age: Option<u64>,
    backlog: i64,
    transport: &'static str,
) -> Router {
    // Aggregate values exercise the same policy evaluator and response serializer.
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect_lazy("postgresql://127.0.0.1/unused")
        .unwrap();
    let state = HealthState {
        store: PgStore::new(pool),
        progress: Arc::default(),
        mode: "observe",
        generation: 1,
        adapter_versions: vec![],
    };
    let progress = Progress {
        last_success: age.map(|age| (Instant::now() - Duration::from_secs(age), Utc::now())),
        successful_sweeps: u64::from(age.is_some()),
        last_sweep_ok: true,
    };
    let snapshot = StoreHealthSnapshot {
        observed_at: Utc::now(),
        authority: None,
        active_leases: 0,
        expired_leases: 0,
        oldest_pending_age_seconds: None,
        outbox_pending: backlog,
        oldest_outbox_age_seconds: None,
        blocked: 0,
        unknown: 0,
        conflicted: 0,
    };
    let body = evaluate(
        &state,
        database.then_some(&snapshot),
        authority.map(|generation| ("rust", generation)),
        &progress,
        transport,
    );
    let code = if body.ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    let value = serde_json::to_value(body).unwrap();
    Router::new()
        .route("/readyz", get(move || async move { (code, Json(value)) }))
        .route("/healthz", get(liveness))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, to_bytes},
        http::Request,
    };
    use tower::ServiceExt;

    #[tokio::test]
    async fn readiness_requires_current_dependencies_and_real_sweep_progress() {
        // Break: unconditional readiness or a startup timestamp masquerading as progress.
        for (db, authority, age, backlog, transport, expected) in [
            (true, Some(1), Some(0), 0, "fake", 200),
            (false, Some(1), Some(0), 0, "fake", 503),
            (true, None, Some(0), 0, "fake", 503),
            (true, Some(2), Some(0), 0, "fake", 503),
            (true, Some(1), None, 0, "fake", 503),
            (true, Some(1), Some(16), 0, "fake", 503),
            (true, Some(1), Some(0), 1001, "fake", 503),
            (true, Some(1), Some(0), 0, "real", 503),
        ] {
            let app = test_router(db, authority, age, backlog, transport);
            let response = app
                .clone()
                .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), expected);
            let live = app
                .oneshot(Request::get("/healthz").body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(live.status(), StatusCode::OK);
        }
    }

    #[tokio::test]
    async fn readiness_exposes_only_sanitized_operational_facts() {
        let response = test_router(true, Some(1), Some(0), 3, "fake")
            .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
            .await
            .unwrap();
        let bytes = to_bytes(response.into_body(), 16384).await.unwrap();
        let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["mode"], "observe");
        assert_eq!(body["executor_kind"], "rust");
        assert_eq!(body["authority_generation"], 1);
        assert_eq!(body["pve_transport"], "fake");
        assert_eq!(body["database"], true);
        assert_eq!(body["outbox"], true);
        assert_eq!(body["outbox_pending"], 3);
        assert!(body["reconciler_last_success"].is_string());
        for key in [
            "database_url",
            "dsn",
            "token",
            "vmid",
            "payload",
            "pve_base_url",
        ] {
            assert!(body.get(key).is_none());
        }
    }
}
