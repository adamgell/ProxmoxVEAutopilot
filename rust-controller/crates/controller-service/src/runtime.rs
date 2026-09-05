use crate::{
    config::{ControllerConfig, ControllerMode},
    health::{HealthState, Progress},
    observe,
};
use ansible_adapter::{AdapterRegistry, AdapterRunner, SYNTHETIC_LONG_SLEEP_V1};
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

pub async fn serve(config: ControllerConfig) -> Result<()> {
    ensure!(
        config.mode != ControllerMode::Native,
        "native executor unavailable"
    );
    let generation: i64 = env::var("RUST_CONTROLLER_AUTHORITY_GENERATION")?.parse()?;
    ensure!(generation > 0, "authority generation must be positive");
    let listen: SocketAddr = env::var("RUST_CONTROLLER_LISTEN")
        .unwrap_or_else(|_| "127.0.0.1:9090".into())
        .parse()?;
    ensure!(
        listen.ip().is_loopback(),
        "health listener must be loopback"
    );
    // This release has only a constructed in-memory fake transport. There is
    // no runtime selector capable of activating a real PVE client.
    ensure!(
        env::var("RUST_CONTROLLER_PVE_TRANSPORT").unwrap_or_else(|_| "fake".into()) == "fake",
        "real transport unavailable"
    );
    let _pve = pve_port::FakePve::new();
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
        adapter_versions: if work.is_some() {
            vec![SYNTHETIC_LONG_SLEEP_V1.adapter_identity]
        } else {
            vec![]
        },
    };
    let listener = tokio::net::TcpListener::bind(listen).await?;
    let (stop, receiver) = watch::channel(false);
    let worker = tokio::spawn(sweeps(state.clone(), pool, work, receiver));
    let server = axum::serve(listener, crate::health::router(state))
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            let _ = stop.send(true);
        })
        .await;
    worker.await?;
    server?;
    Ok(())
}

async fn sweeps(
    state: HealthState,
    pool: PgPool,
    work: Option<AdapterWork>,
    mut stop: watch::Receiver<bool>,
) {
    let mut active: Option<JoinHandle<()>> = None;
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
            let _ = done.await;
        }
        let sweep = async {
            if let Some(work) = &work {
                work.scheduler.reap_expired().await?;
                if active.is_none() {
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
                        let invocation = work.registry.validate(&work.plan)?;
                        let runner = AdapterRunner::new(work.scheduler.clone());
                        let cancellation = stop.clone();
                        active = Some(tokio::spawn(async move {
                            let _ = runner.run(invocation, grant, cancellation).await;
                        }));
                    }
                }
            } else {
                // The observer has no scheduler or adapter capability.
                observe::observe_once(&pool).await?;
            }
            state.store.health_snapshot().await?;
            Ok::<(), anyhow::Error>(())
        };
        let ok = matches!(
            tokio::time::timeout(Duration::from_secs(4), sweep).await,
            Ok(Ok(()))
        );
        let mut progress = state.progress.write().await;
        if ok {
            progress.succeeded();
        } else {
            progress.last_sweep_ok = false;
        }
    }
    if let Some(mut active) = active
        && tokio::time::timeout(Duration::from_secs(12), &mut active)
            .await
            .is_err()
    {
        active.abort();
        let _ = active.await;
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
