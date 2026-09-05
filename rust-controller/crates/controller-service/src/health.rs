use crate::pve_observation::{ObservationResult, ObservationStatus};
use axum::{Json, Router, extract::State, http::StatusCode, routing::get};
use chrono::{DateTime, Utc};
use postgres_store::{PgStore, StoreHealthSnapshot};
use serde::Serialize;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::RwLock;

#[derive(Default)]
pub(crate) struct ObservationProgress {
    result: Option<ObservationResult>,
    last_success: Option<(Instant, DateTime<Utc>)>,
}
impl ObservationProgress {
    pub(crate) fn record(&mut self, result: ObservationResult) {
        if result.status() == ObservationStatus::Fresh
            && result.counts().is_some()
            && let Some(observed_at) = result.observed_at()
        {
            self.last_success = Some((Instant::now(), observed_at));
        }
        self.result = Some(result);
    }
    fn snapshot(&self, now: Instant) -> Option<ObservationHealth> {
        let result = self.result.as_ref()?;
        let fresh = result.status() == ObservationStatus::Fresh
            && result.counts().is_some()
            && result.observed_at().is_some()
            && self.last_success.is_some_and(|(at, _)| {
                now.saturating_duration_since(at) <= Duration::from_secs(15)
            });
        Some(ObservationHealth {
            result: result.clone(),
            fresh,
            last_success: self.last_success.map(|(_, utc)| utc),
        })
    }
}
#[derive(Serialize)]
struct ObservationHealth {
    #[serde(flatten)]
    result: ObservationResult,
    fresh: bool,
    last_success: Option<DateTime<Utc>>,
}

#[derive(Clone, Default)]
pub struct Progress {
    pub last_success: Option<(Instant, DateTime<Utc>)>,
    pub successful_sweeps: u64,
    pub last_sweep_ok: bool,
    pub operational_failure: Option<OperationalFailure>,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationalFailure {
    AdapterPreparation,
    AdapterSpawn,
    AdapterProcess,
    WorkerJoin,
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
    pub pve_transport: &'static str,
    pub observation: Arc<RwLock<ObservationProgress>>,
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
    operational_failure: Option<OperationalFailure>,
    adapter_versions: Vec<&'static str>,
    pve_transport: &'static str,
    pve_evidence: &'static str,
    blocked: Option<i64>,
    unknown: Option<i64>,
    conflicted: Option<i64>,
    ready: bool,
    pve_observation: Option<ObservationHealth>,
    observation_ready: bool,
}

fn evaluate(
    state: &HealthState,
    snapshot: Option<&StoreHealthSnapshot>,
    authority: Option<(&'static str, i64)>,
    progress: &Progress,
    observation: &ObservationProgress,
) -> ReadyResponse {
    let outbox = snapshot.is_some_and(|s| {
        s.outbox_pending <= 1000 && s.oldest_outbox_age_seconds.is_none_or(|age| age <= 300)
    });
    let fresh = progress.last_sweep_ok
        && progress
            .last_success
            .is_some_and(|(time, _)| time.elapsed() <= Duration::from_secs(15));
    let ready = snapshot.is_some()
        && progress.operational_failure.is_none()
        && outbox
        && fresh
        && state.pve_transport == "fake"
        && authority.is_some_and(|(executor, generation)| {
            executor == "rust" && generation == state.generation
        });
    let pve_observation = observation.snapshot(Instant::now());
    let observation_ready = pve_observation.as_ref().is_some_and(|o| o.fresh);
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
        operational_failure: progress.operational_failure,
        adapter_versions: state.adapter_versions.clone(),
        pve_transport: state.pve_transport,
        pve_evidence: if state.pve_transport == "http-observe" {
            "visibility_only_coverage_unverified"
        } else {
            "synthetic_no_device_readiness"
        },
        blocked: snapshot.map(|s| s.blocked),
        unknown: snapshot.map(|s| s.unknown),
        conflicted: snapshot.map(|s| s.conflicted),
        ready,
        pve_observation,
        observation_ready,
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
    let observation = state.observation.read().await;
    let authority = snapshot
        .as_ref()
        .and_then(|s| s.authority)
        .map(|a| (a.executor_kind().as_str(), a.generation()));
    response(evaluate(
        &state,
        snapshot.as_ref(),
        authority,
        &progress,
        &observation,
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
        pve_transport: transport,
        observation: Arc::default(),
    };
    let progress = Progress {
        last_success: age.map(|age| (Instant::now() - Duration::from_secs(age), Utc::now())),
        successful_sweeps: u64::from(age.is_some()),
        last_sweep_ok: true,
        operational_failure: None,
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
        &ObservationProgress::default(),
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

    struct Visibility(bool);
    impl pve_port::PveVisibilityReadPort for Visibility {
        fn cluster_visibility<'a, 'b>(
            &'a self,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<
                        Output = Result<pve_port::ClusterVisibility, pve_port::PveReadError>,
                    > + Send
                    + 'b,
            >,
        >
        where
            'a: 'b,
            Self: 'b,
        {
            Box::pin(async move {
                if self.0 {
                    pve_port::ClusterVisibility::from_wire(serde_json::json!([]), Utc::now())
                } else {
                    Err(pve_port::PveReadError::Unauthorized)
                }
            })
        }
    }

    #[tokio::test]
    async fn observation_freshness_expires_monotonically_and_failure_cannot_reuse_last_success() {
        // Break: wall-clock age, stale success, or failed results granting observation readiness.
        let mut progress = ObservationProgress::default();
        assert!(progress.snapshot(Instant::now()).is_none());
        let success = crate::pve_observation::PveObservation::from_port(Box::new(Visibility(true)))
            .collect_once()
            .await;
        progress.record(success);
        let (at, timestamp) = progress.last_success.unwrap();
        assert!(
            progress
                .snapshot(at + Duration::from_secs(15))
                .unwrap()
                .fresh
        );
        assert!(
            !progress
                .snapshot(at + Duration::from_millis(15_001))
                .unwrap()
                .fresh
        );
        let failure =
            crate::pve_observation::PveObservation::from_port(Box::new(Visibility(false)))
                .collect_once()
                .await;
        progress.record(failure);
        let failed = progress.snapshot(at).unwrap();
        assert!(!failed.fresh);
        assert_eq!(failed.last_success, Some(timestamp));
        assert!(failed.result.observed_at().is_none());
        assert!(failed.result.counts().is_none());
    }

    #[tokio::test]
    async fn fresh_http_observation_cannot_mask_execution_failure_or_database_failure() {
        let mut observation = ObservationProgress::default();
        observation.record(
            crate::pve_observation::PveObservation::from_port(Box::new(Visibility(true)))
                .collect_once()
                .await,
        );
        let state = HealthState {
            store: PgStore::new(
                sqlx::postgres::PgPoolOptions::new()
                    .connect_lazy("postgresql://127.0.0.1/unused")
                    .unwrap(),
            ),
            progress: Arc::default(),
            mode: "observe",
            generation: 1,
            adapter_versions: vec![],
            pve_transport: "http-observe",
            observation: Arc::default(),
        };
        let mut progress = Progress::default();
        progress.succeeded();
        progress.operational_failure = Some(OperationalFailure::AdapterProcess);
        let response = evaluate(&state, None, Some(("rust", 1)), &progress, &observation);
        assert!(!response.ready);
        assert!(!response.database);
        assert!(!response.outbox);
        assert!(response.observation_ready);
        assert!(matches!(
            response.operational_failure,
            Some(OperationalFailure::AdapterProcess)
        ));
    }

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
