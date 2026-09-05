use crate::{
    config::{ControllerConfig, ControllerMode, ValidatedObservationConfig},
    health::{HealthState, ObservationProgress, OperationalFailure, Progress},
    infrastructure_health::InfrastructureProgress,
    infrastructure_observation::InfrastructureObservation,
    observe, pve_credentials,
    pve_observation::PveObservation,
};
use ansible_adapter::{
    AdapterError, AdapterRegistry, AdapterReport, AdapterRunner, SYNTHETIC_LONG_SLEEP_V1,
};
use anyhow::{Result, ensure};
use api_compat::{JobEnvelope, NormalizedPlan, normalize_job};
use controller_domain::WorkflowKind;
use postgres_store::PgStore;
use scheduler::{ExecutorKind, Scheduler};
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{env, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    sync::{RwLock, watch},
    task::JoinHandle,
};

struct AdapterWork {
    registry: AdapterRegistry,
    plan: NormalizedPlan,
    scheduler: Scheduler,
    cap: u32,
}

type AdapterResult = std::result::Result<AdapterReport, AdapterError>;
fn record_completion(
    progress: &mut Progress,
    result: std::result::Result<AdapterResult, tokio::task::JoinError>,
) {
    use ansible_adapter::CompletionReason;
    let failure = match result {
        Err(_) => Some(OperationalFailure::WorkerJoin),
        Ok(Err(_)) => Some(OperationalFailure::AdapterPreparation),
        Ok(Ok(report)) => match report.reason {
            CompletionReason::SpawnFailed => Some(OperationalFailure::AdapterSpawn),
            CompletionReason::ProcessError => Some(OperationalFailure::AdapterProcess),
            CompletionReason::Exited
            | CompletionReason::Cancelled
            | CompletionReason::TimedOut
            | CompletionReason::AuthorityLost => None,
        },
    };
    // Failures are fixed enum labels, never dependency errors or panic payloads.
    // Latch until restart after repair; a healthy DB sweep cannot clear a fault.
    if progress.operational_failure.is_none() {
        progress.operational_failure = failure;
    }
}

pub async fn serve(
    config: ControllerConfig,
    observation_config: Option<ValidatedObservationConfig>,
) -> Result<()> {
    ensure!(
        config.mode != ControllerMode::Native,
        "native executor unavailable"
    );
    ensure!(
        observation_config.is_none() || config.mode == ControllerMode::Observe,
        "observation requires observe mode"
    );
    // Selection and target validation precede credential I/O. The runtime consumes
    // the validated capability; environment text is never reparsed as transport.
    let observations = observation_config
        .as_ref()
        .map(|selected| {
            let token = pve_credentials::load_token(selected)?;
            let infrastructure = selected
                .node_target()
                .map(|_| InfrastructureObservation::new(selected, token.clone()))
                .transpose()
                .map_err(|_| anyhow::anyhow!("observation setup rejected"))?;
            let observation = PveObservation::new(selected, token)
                .map_err(|_| anyhow::anyhow!("observation setup rejected"))?;
            Ok::<_, anyhow::Error>((observation, infrastructure))
        })
        .transpose()?;
    let (observation, infrastructure) = match observations {
        Some((observation, infrastructure)) => (Some(observation), infrastructure),
        None => (None, None),
    };
    let (pve_transport, _fake) = if observation.is_some() {
        ("http-observe", None)
    } else {
        ("fake", Some(pve_port::FakePve::new()))
    };
    let generation: i64 = env::var("RUST_CONTROLLER_AUTHORITY_GENERATION")?.parse()?;
    ensure!(generation > 0, "authority generation must be positive");
    let listen: SocketAddr = env::var("RUST_CONTROLLER_LISTEN")
        .unwrap_or_else(|_| "127.0.0.1:9090".into())
        .parse()?;
    ensure!(
        listen.ip().is_loopback(),
        "health listener must be loopback"
    );
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .acquire_timeout(Duration::from_secs(2))
        .connect_lazy(&config.database_url)?;
    let store = PgStore::new(pool.clone());
    let work = if config.mode == ControllerMode::Adapter {
        let path = PathBuf::from(env::var("RUST_CONTROLLER_SYNTHETIC_JOB")?);
        ensure!(
            std::fs::metadata(&path)?.len() <= 65536,
            "fixture too large"
        );
        let plan = normalize_job(&JobEnvelope::from_json_str(&std::fs::read_to_string(
            path,
        )?)?)?;
        let registry =
            AdapterRegistry::local(&PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."))?;
        registry.validate(&plan)?;
        let cap: u32 = env::var("RUST_CONTROLLER_SYNTHETIC_CAP")?.parse()?;
        ensure!((1..=32).contains(&cap), "invalid cap");
        Some(AdapterWork {
            registry,
            plan,
            cap,
            scheduler: Scheduler::new(
                store.clone(),
                ExecutorKind::Rust,
                generation,
                env::var("RUST_CONTROLLER_WORKER_ID")?,
            )?,
        })
    } else {
        None
    };
    let state = HealthState {
        store,
        progress: Arc::new(RwLock::new(Progress::default())),
        mode: config.mode.as_str(),
        generation,
        pve_transport,
        observation: Arc::default(),
        infrastructure: Arc::default(),
        adapter_versions: if work.is_some() {
            vec![SYNTHETIC_LONG_SLEEP_V1.adapter_identity]
        } else {
            vec![]
        },
    };
    let listener = tokio::net::TcpListener::bind(listen).await?;
    let (stop, receiver) = watch::channel(false);
    let observer = observation.map(|observation| {
        tokio::spawn(observation_sweeps(
            observation,
            state.observation.clone(),
            receiver.clone(),
        ))
    });
    let infrastructure_observer = infrastructure.map(|observation| {
        tokio::spawn(infrastructure_sweeps(
            observation,
            state.infrastructure.clone(),
            receiver.clone(),
        ))
    });
    let worker = tokio::spawn(sweeps(state.clone(), pool, work, receiver));
    let server = axum::serve(listener, crate::health::router(state))
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            let _ = stop.send(true);
        })
        .await;
    worker.await?;
    if let Some(observer) = observer {
        observer.await?;
    }
    if let Some(observer) = infrastructure_observer {
        observer.await?;
    }
    server?;
    Ok(())
}

// This loop owns only a GET capability and sanitized in-memory progress. It has
// no scheduler, store, evidence-ingestion or process-runner capability.
async fn observation_sweeps(
    observation: PveObservation,
    progress: Arc<RwLock<ObservationProgress>>,
    mut stop: watch::Receiver<bool>,
) {
    let mut interval = tokio::time::interval(Duration::from_secs(5));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        if *stop.borrow() {
            break;
        }
        tokio::select! { biased; _ = stop.changed() => break, _ = interval.tick() => {} }
        let result = tokio::select! { biased; _ = stop.changed() => break, result = observation.collect_once() => result };
        progress.write().await.record(result);
    }
}

// This independent loop owns only the selected-node two-GET capability.
async fn infrastructure_sweeps(
    observation: InfrastructureObservation,
    progress: Arc<RwLock<InfrastructureProgress>>,
    mut stop: watch::Receiver<bool>,
) {
    let mut interval = tokio::time::interval(Duration::from_secs(10));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        if *stop.borrow() {
            break;
        }
        tokio::select! { biased; _ = stop.changed() => break, _ = interval.tick() => {} }
        let result = tokio::select! { biased; _ = stop.changed() => break, result = observation.collect_once() => result };
        progress.write().await.record(result);
    }
}

async fn sweeps(
    state: HealthState,
    pool: PgPool,
    work: Option<AdapterWork>,
    mut stop: watch::Receiver<bool>,
) {
    let mut active: Option<JoinHandle<AdapterResult>> = None;
    let mut interval = tokio::time::interval(Duration::from_secs(1));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = stop.changed() => break,
            _ = interval.tick() => {}
        }
        if active.as_ref().is_some_and(JoinHandle::is_finished)
            && let Some(done) = active.take()
        {
            let result = done.await;
            record_completion(&mut *state.progress.write().await, result);
        }
        let cancellation = stop.clone();
        let sweep = async {
            if let Some(work) = &work {
                work.scheduler.reap_expired().await?;
                if active.is_none() && state.progress.read().await.operational_failure.is_none() {
                    let fingerprint = work.plan.fingerprint()?;
                    if let Some(grant) = work
                        .scheduler
                        .claim_next_bound(
                            WorkflowKind::SyntheticLongSleep,
                            work.cap,
                            work.plan.contract_version(),
                            fingerprint.as_hex(),
                        )
                        .await?
                    {
                        let invocation = match work.registry.validate(&work.plan) {
                            Ok(invocation) => invocation,
                            Err(error) => {
                                record_completion(
                                    &mut *state.progress.write().await,
                                    Ok(Err(error)),
                                );
                                anyhow::bail!("adapter preparation unavailable");
                            }
                        };
                        let runner = AdapterRunner::new(work.scheduler.clone());
                        let cancellation = cancellation.clone();
                        active = Some(tokio::spawn(runner.run(invocation, grant, cancellation)));
                    }
                }
            } else {
                // The observer has no scheduler or adapter capability.
                observe::observe_once(&pool).await?;
            }
            state.store.health_snapshot().await?;
            Ok::<(), anyhow::Error>(())
        };
        let outcome = tokio::select! { biased; _ = stop.changed() => break, result = tokio::time::timeout(Duration::from_secs(4), sweep) => result };
        let ok = matches!(outcome, Ok(Ok(())));
        let mut progress = state.progress.write().await;
        if ok {
            progress.succeeded();
        } else {
            progress.last_sweep_ok = false;
        }
    }
    if let Some(mut active) = active {
        let result = match tokio::time::timeout(Duration::from_secs(12), &mut active).await {
            Ok(result) => result,
            Err(_) => {
                active.abort();
                active.await
            }
        };
        record_completion(&mut *state.progress.write().await, result);
    }
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = terminate.recv() => {} }
    }
    #[cfg(not(unix))]
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
#[path = "runtime_tests.rs"]
mod tests;
