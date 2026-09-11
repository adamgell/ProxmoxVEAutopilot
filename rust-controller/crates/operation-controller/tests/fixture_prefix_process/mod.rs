//! Owned subprocess transport for the actual controller prefix test.
use pve_port::{fixture_ipc::*, fixture_support::*};
use serde::{Deserialize, Serialize};
use sqlx::types as uuid;
use std::{fs, path::Path, sync::Arc, time::Duration};

#[derive(Serialize, Deserialize)]
pub enum Setup {
    Clone(CheckpointBinding),
    Resize {
        generation: uuid::Uuid,
        owner: uuid::Uuid,
        predecessor: Vec<u8>,
        receipt: Vec<u8>,
    },
    Configure {
        generation: uuid::Uuid,
        owner: uuid::Uuid,
        predecessor_identity: FixtureStageIdentity,
        predecessor: Vec<u8>,
        receipt: Vec<u8>,
    },
}

#[derive(Serialize, Deserialize)]
struct Input {
    identity: FixtureReadIdentity,
    setup: Setup,
}

pub fn spawn(
    directory: &Path,
    dsn: String,
    identity: FixtureReadIdentity,
    setup: Setup,
) -> tokio::task::JoinHandle<Result<postgres_store::OsDeployProgress, String>> {
    let directory = directory.to_owned();
    let input = directory.join(format!("worker-{}.json", identity.operation));
    let output = input.with_extension("result");
    fs::write(
        &input,
        serde_json::to_vec(&Input { identity, setup }).unwrap(),
    )
    .unwrap();
    tokio::spawn(async move {
        let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "fixture_prefix_worker",
                "--ignored",
                "--nocapture",
            ])
            .env("PVA_PREFIX_INPUT", &input)
            .env("PVA_PREFIX_DSN", dsn)
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| e.to_string())?;
        let kill = input.with_extension("kill");
        let status = tokio::time::timeout(Duration::from_secs(26), async {
            loop {
                if kill.exists() {
                    assert!(
                        child.try_wait()?.is_none(),
                        "worker exited before forced death"
                    );
                    child.kill().await?;
                    let status = child.wait().await?;
                    assert!(!status.success());
                    return Ok::<_, std::io::Error>(status);
                }
                if let Some(status) = child.try_wait()? {
                    return Ok(status);
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
        if !status.success() {
            return Err(format!("prefix worker exited: {status}"));
        }
        let state = serde_json::from_slice(&fs::read(output).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        Ok(postgres_store::OsDeployProgress::Decided(state))
    })
}

pub async fn recover(directory: &Path, dsn: String, operation: controller_domain::OperationId) {
    let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "fixture_prefix_recovery_worker",
            "--ignored",
            "--nocapture",
        ])
        .env(
            "PVA_PREFIX_INPUT",
            directory.join(format!("worker-{}.json", operation.as_uuid())),
        )
        .env("PVA_PREFIX_DSN", dsn)
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    assert!(
        tokio::time::timeout(Duration::from_secs(44), child.wait())
            .await
            .unwrap()
            .unwrap()
            .success()
    );
}

pub async fn run_recovery() {
    let input = std::path::PathBuf::from(std::env::var_os("PVA_PREFIX_INPUT").unwrap());
    let Input { identity, setup } = serde_json::from_slice(&fs::read(&input).unwrap()).unwrap();
    let operation =
        serde_json::from_value(serde_json::to_value(identity.operation).unwrap()).unwrap();
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(&std::env::var("PVA_PREFIX_DSN").unwrap())
        .await
        .unwrap();
    let store = postgres_store::PgStore::new(pool.clone());
    let scheduler = postgres_store::Scheduler::new(
        store.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        "prefix-recovery",
    )
    .unwrap();
    let before = store.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(before.state(), controller_domain::ExecutionState::Running);
    assert!(before.dispatch().is_some());
    tokio::time::timeout(Duration::from_secs(33), async {
        loop {
            let expired: bool = sqlx::query_scalar("SELECT lease_expires_at <= clock_timestamp() FROM rust_controller.worker_leases WHERE operation_id=$1").bind(identity.operation).fetch_one(&pool).await.unwrap();
            if expired { break; }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }).await.unwrap();
    scheduler.reap_osdeploy_expired().await.unwrap();
    let after = store.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(after.state(), controller_domain::ExecutionState::Unknown);
    assert_eq!(after.attempt_id(), before.attempt_id());
    assert_eq!(after.dispatch(), before.dispatch());
    assert_eq!(after.receipt(), before.receipt());
    assert!(
        scheduler
            .claim_osdeploy_bound(operation, after.plan().workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1")
            .bind(identity.operation)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(count, 1);
    if before.receipt().is_some() {
        let Setup::Configure {
            generation,
            owner,
            predecessor_identity,
            predecessor,
            receipt,
        } = setup
        else {
            panic!("post-receipt recovery requires ConfigurePe prefix")
        };
        let request = FixtureStageRequest::new(
            identity.fixture_id,
            before.dispatch().unwrap().request().clone(),
        )
        .unwrap();
        let stage_identity = FixtureStageIdentity {
            operation: identity.operation,
            stage: FixtureLedgerStage::ConfigurePe,
            attempt: before.attempt_id().unwrap().as_uuid(),
            generation,
            owner,
            request_sha256: request.request_sha256(),
        };
        let directory = input.parent().unwrap();
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut stream = tokio::net::UnixStream::connect(directory.join("client.sock"))
            .await
            .unwrap();
        let message = serde_json::to_vec(
            &serde_json::json!({"command":"accepted_stage_effect","identity":stage_identity}),
        )
        .unwrap();
        stream.write_u32(message.len() as u32).await.unwrap();
        stream.write_all(&message).await.unwrap();
        let size = stream.read_u32().await.unwrap();
        assert!(size < 100_000);
        let mut bytes = vec![0; size as usize];
        stream.read_exact(&mut bytes).await.unwrap();
        let effect: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        let exact_receipt: Vec<u8> =
            serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
        assert_eq!(
            request.decode_receipt(&exact_receipt).unwrap().receipt(),
            before.receipt().unwrap().receipt()
        );
        let port = FixtureProvisioningPort::new_late(
            directory.join("client.sock"),
            Duration::from_secs(2),
            identity.clone(),
        )
        .unwrap()
        .with_late_configure_after_resize(
            FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
                .unwrap(),
            generation,
            owner,
            predecessor_identity,
            FixtureStageRequest::decode(&predecessor).unwrap(),
            receipt,
        )
        .unwrap()
        .with_late_configure_receipt(request.request(), exact_receipt)
        .unwrap();
        let due = tokio::time::timeout(Duration::from_secs(7), async {
            loop {
                if let Some(due) = scheduler
                    .discover_osdeploy_due()
                    .await
                    .unwrap()
                    .into_iter()
                    .find(|d| d.operation_id() == operation)
                {
                    break due;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
        })
        .await
        .unwrap();
        let controller = operation_controller::OsDeployController::new_fixture(
            store.clone(),
            scheduler,
            Arc::new(port),
            1,
        )
        .unwrap();
        fs::write(
            input.with_extension("await-publication"),
            b"reaped original attempt and ready for current observation",
        )
        .unwrap();
        tokio::time::timeout(Duration::from_secs(3), async {
            while !input.with_extension("publication-ready").exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        controller.open_send_admission().await.unwrap();
        assert_eq!(
            controller.run_due_once(&due).await.unwrap(),
            postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
        );
        let terminal = store.load_osdeploy_operation(operation).await.unwrap();
        assert_eq!(terminal.attempt_id(), before.attempt_id());
        assert_eq!(terminal.dispatch(), before.dispatch());
        assert_eq!(terminal.receipt(), before.receipt());
        let attempts: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1",
        )
        .bind(identity.operation)
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(attempts, 1);
        controller
            .close_and_drain(Duration::from_secs(1))
            .await
            .unwrap();
    }
}

pub async fn run() {
    let input =
        std::path::PathBuf::from(std::env::var_os("PVA_PREFIX_INPUT").expect("owned prefix input"));
    let directory = input.parent().unwrap();
    let Input { identity, setup } = serde_json::from_slice(&fs::read(&input).unwrap()).unwrap();
    let operation =
        serde_json::from_value(serde_json::to_value(identity.operation).unwrap()).unwrap();
    let port = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        identity,
    )
    .unwrap();
    let checkpoint =
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap();
    let port = match setup {
        Setup::Clone(binding) => port.with_checkpoint(checkpoint, binding).unwrap(),
        Setup::Resize {
            generation,
            owner,
            predecessor,
            receipt,
        } => port
            .with_late_resize_after_legacy_clone(
                checkpoint,
                generation,
                owner,
                FixtureCloneRequest::decode(&predecessor).unwrap(),
                receipt,
            )
            .unwrap(),
        Setup::Configure {
            generation,
            owner,
            predecessor_identity,
            predecessor,
            receipt,
        } => port
            .with_late_configure_after_resize(
                checkpoint,
                generation,
                owner,
                predecessor_identity,
                FixtureStageRequest::decode(&predecessor).unwrap(),
                receipt,
            )
            .unwrap(),
    };
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(4)
        .acquire_timeout(Duration::from_secs(2))
        .connect(&std::env::var("PVA_PREFIX_DSN").unwrap())
        .await
        .unwrap();
    let store = postgres_store::PgStore::new(pool.clone());
    let scheduler = postgres_store::Scheduler::new(
        store.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        "prefix-process",
    )
    .unwrap();
    let controller =
        operation_controller::OsDeployController::new_fixture(store, scheduler, Arc::new(port), 1)
            .unwrap();
    controller.open_send_admission().await.unwrap();
    let outcome = controller.run_osdeploy_once(operation).await;
    if !matches!(
        &outcome,
        Ok(postgres_store::OsDeployProgress::Decided(
            controller_domain::ExecutionState::Satisfied
        ))
    ) {
        // Only fixed, constrained journal metadata crosses into CI output. Never
        // print payloads, SQL error messages, connection options, or credentials.
        let diagnostic = tokio::time::timeout(Duration::from_millis(250), async {
            sqlx::query_as::<_, (i64, String, Option<String>)>(
                "SELECT aggregate_revision, event_kind, execution_state FROM rust_controller.journal_events WHERE operation_id=$1 ORDER BY aggregate_revision DESC LIMIT 12",
            )
            .bind(operation.as_uuid())
            .fetch_all(&pool)
            .await
        })
        .await;
        let context = match diagnostic {
            Ok(Ok(rows)) => format!("journal={rows:?}"),
            Ok(Err(sqlx::Error::PoolTimedOut)) => "diagnostic=pool_timeout".to_owned(),
            Ok(Err(sqlx::Error::PoolClosed)) => "diagnostic=pool_closed".to_owned(),
            Ok(Err(sqlx::Error::Database(_))) => "diagnostic=database_error".to_owned(),
            Ok(Err(sqlx::Error::Io(_))) => "diagnostic=io_error".to_owned(),
            Ok(Err(_)) => "diagnostic=other_storage_error".to_owned(),
            Err(_) => "diagnostic=read_deadline".to_owned(),
        };
        eprintln!("prefix operation={operation:?} outcome={outcome:?} {context}");
    }
    let postgres_store::OsDeployProgress::Decided(state) = outcome.unwrap() else {
        panic!("prefix worker did not decide")
    };
    controller
        .close_and_drain(Duration::from_secs(1))
        .await
        .unwrap();
    fs::write(
        input.with_extension("result"),
        serde_json::to_vec(&state).unwrap(),
    )
    .unwrap();
}
