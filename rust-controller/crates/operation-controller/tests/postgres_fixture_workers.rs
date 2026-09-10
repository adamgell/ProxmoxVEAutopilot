//! Owned worker address-space loss after durable Clone dispatch and receipt.
//! Workers use real lease expiry and scheduler reconciliation; collection is supervisor-assisted.
#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
#[path = "../../postgres-store/tests/osdeploy_execution_support/mod.rs"]
mod osdeploy_execution_support;
#[allow(dead_code)]
#[path = "../../postgres-store/tests/osdeploy_support/mod.rs"]
mod osdeploy_support;

use controller_domain::{EventId, OperationId};
use osdeploy_execution_support::Scenario;
use postgres_store::{ExecutorKind, PgStore, Scheduler};
use pve_port::{fixture_ipc::*, fixture_support::*, *};
use sqlx::ConnectOptions;
use std::{fs, os::unix::fs::PermissionsExt, path::Path, time::Duration};

async fn wait_file(path: &Path) -> Vec<u8> {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(bytes) = fs::read(path) {
                return bytes;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("worker protocol file exceeded bound")
}

fn publish(path: &Path, bytes: &[u8]) {
    let temporary = path.with_extension("pending");
    fs::write(&temporary, bytes).unwrap();
    fs::rename(temporary, path).unwrap();
}

fn child(directory: &Path, dsn: &str, entry: &str) -> tokio::process::Child {
    tokio::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", entry, "--ignored", "--nocapture"])
        .env("PVA_WORKER_PROOF_DIRECTORY", directory)
        .env("PVA_WORKER_PROOF_DSN", dsn)
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap()
}

async fn child_context() -> (std::path::PathBuf, PgStore, OperationId, String) {
    let directory = std::path::PathBuf::from(
        std::env::var_os("PVA_WORKER_PROOF_DIRECTORY").expect("owned worker directory required"),
    );
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .acquire_timeout(Duration::from_secs(1))
        .connect(&std::env::var("PVA_WORKER_PROOF_DSN").unwrap())
        .await
        .unwrap();
    let operation =
        serde_json::from_slice(&fs::read(directory.join("operation.json")).unwrap()).unwrap();
    let workflow = fs::read_to_string(directory.join("workflow.txt")).unwrap();
    (directory, PgStore::new(pool), operation, workflow)
}

#[tokio::test]
async fn worker_loss_preserves_original_attempt_and_exact_receipt() {
    prove_worker_loss(false).await;
}

#[tokio::test]
async fn accepted_effect_without_journal_receipt_stays_unknown_after_worker_loss() {
    prove_worker_loss(true).await;
}

async fn prove_worker_loss(before_receipt: bool) {
    let s = Scenario::new(300, true).await;
    let operation = s.ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let fixture_id = controller_domain::RunId::new().as_uuid();
    let directory = std::path::PathBuf::from("/tmp").join(format!("pgfw-{fixture_id}"));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    if before_receipt {
        publish(
            &directory.join("pause-before-receipt"),
            b"owned test boundary",
        );
    }
    publish(
        &directory.join("operation.json"),
        &serde_json::to_vec(&operation).unwrap(),
    );
    publish(
        &directory.join("workflow.txt"),
        s.ids.workflow_sha256().as_bytes(),
    );
    publish(
        &directory.join("fixture.json"),
        &serde_json::to_vec(&fixture_id).unwrap(),
    );
    let dsn = s.db.pool.connect_options().to_url_lossy().to_string();
    let mut worker_a = child(&directory, &dsn, "dispatching_worker_a");
    wait_file(&directory.join("started")).await;
    let started = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    let attempt = started.attempt_id().unwrap();
    let context =
        s.db.other
            .load_osdeploy_pve_context(
                operation,
                started.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    let event =
        s.db.other
            .record_osdeploy_pve_evidence(operation, attempt, started.revision(), &evidence)
            .await
            .unwrap();
    let revision =
        s.db.other
            .load_osdeploy_operation(operation)
            .await
            .unwrap()
            .revision();
    publish(
        &directory.join("evidence.json"),
        &serde_json::to_vec(&(event, revision)).unwrap(),
    );
    let envelope =
        FixtureCloneRequest::decode(&wait_file(&directory.join("request.json")).await).unwrap();
    let after = VmState {
        disk_bytes: 4096,
        pe_configured: false,
    };
    publish(
        &directory.join("clone.json"),
        &FixtureCloneSeed::new(&envelope, after)
            .unwrap()
            .encode()
            .unwrap(),
    );
    let daemon_directory = directory.clone();
    let daemon =
        std::thread::spawn(move || run(&daemon_directory, Duration::from_secs(8)).unwrap());
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while reader
            .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
            .await
            .is_err()
        {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    publish(&directory.join("go"), b"ready");
    // The kill gate depends on independent durable reads, never a child marker.
    tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            if let Some(effect) = reader
                .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
                .await
                .unwrap()
            {
                // Read PostgreSQL after the effect observation: an earlier read
                // could legitimately precede the child's dispatch commit.
                let snapshot = s.db.other.load_osdeploy_operation(operation).await.unwrap();
                let original = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
                assert_eq!(snapshot.attempt_id(), Some(attempt));
                assert_eq!(snapshot.state(), controller_domain::ExecutionState::Running);
                assert!(snapshot.dispatch().is_some());
                if before_receipt {
                    assert!(snapshot.receipt().is_none());
                    assert_eq!(original.submission_sequence(), 1);
                    break;
                }
                if let Some(receipt) = snapshot.receipt() {
                    assert_eq!(receipt.receipt(), original.receipt());
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("worker A durable effect/receipt boundary exceeded bound");
    assert!(
        worker_a.try_wait().unwrap().is_none(),
        "worker A exited before forced loss"
    );
    worker_a.kill().await.unwrap();
    assert!(!worker_a.wait().await.unwrap().success());
    publish(
        &directory.join("attempt.json"),
        &serde_json::to_vec(&attempt).unwrap(),
    );
    let mut worker_b = child(&directory, &dsn, "recovering_worker_b");
    tokio::time::timeout(Duration::from_secs(35), async {
        loop {
            if s.db
                .other
                .load_osdeploy_operation(operation)
                .await
                .unwrap()
                .state()
                == controller_domain::ExecutionState::Unknown
            {
                break;
            }
            assert!(
                worker_b.try_wait().unwrap().is_none(),
                "worker B exited before reaping"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("worker B natural lease expiry exceeded bound");
    daemon.join().unwrap();
    fs::remove_file(directory.join("client.sock")).unwrap();
    fs::remove_file(directory.join("supervisor.sock")).unwrap();
    let daemon_directory = directory.clone();
    let daemon =
        std::thread::spawn(move || run(&daemon_directory, Duration::from_secs(4)).unwrap());
    tokio::time::timeout(Duration::from_secs(1), async {
        while reader
            .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
            .await
            .is_err()
        {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let snapshot = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(snapshot.attempt_id(), Some(attempt));
    let context =
        s.db.other
            .load_osdeploy_pve_context(
                operation,
                snapshot.revision(),
                ProvisioningEvaluationModeV1::Reconciliation,
            )
            .await
            .unwrap();
    // This supervisor fake has no task observation for the IPC-generated UPID.
    // The supported evaluator must retain uncertainty rather than invent success.
    let evidence = s.collect(&context).await;
    let event =
        s.db.other
            .record_osdeploy_pve_evidence(operation, attempt, snapshot.revision(), &evidence)
            .await
            .unwrap();
    let revision =
        s.db.other
            .load_osdeploy_operation(operation)
            .await
            .unwrap()
            .revision();
    publish(
        &directory.join("reconciliation.json"),
        &serde_json::to_vec(&(event, revision)).unwrap(),
    );
    let result = tokio::time::timeout(Duration::from_secs(3), worker_b.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(result.success());
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(count, 1);
    let final_snapshot = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(
        final_snapshot.state(),
        controller_domain::ExecutionState::Unknown
    );
    assert_eq!(final_snapshot.receipt().is_none(), before_receipt);
    assert_eq!(
        s.db.other
            .load_osdeploy_operation(operation)
            .await
            .unwrap()
            .attempt_id(),
        Some(attempt)
    );
    daemon.join().unwrap();
}

#[tokio::test]
#[ignore = "owned worker A subprocess entry point"]
async fn dispatching_worker_a() {
    let (directory, store, operation, workflow) = child_context().await;
    let scheduler =
        Scheduler::new(store.clone(), ExecutorKind::Rust, 1, "process-worker-a").unwrap();
    let grant = scheduler
        .claim_osdeploy_bound(operation, &workflow, 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&grant, &workflow)
        .await
        .unwrap();
    publish(&directory.join("started"), b"started");
    let (event, revision): (EventId, i64) =
        serde_json::from_slice(&wait_file(&directory.join("evidence.json")).await).unwrap();
    let request = store
        .prepare_osdeploy_pve_request(operation, revision, event)
        .await
        .unwrap();
    let ProvisioningMutationRequestV1::Clone(clone) = &request else {
        panic!("expected Clone")
    };
    let fixture_id =
        serde_json::from_slice(&fs::read(directory.join("fixture.json")).unwrap()).unwrap();
    let envelope = FixtureCloneRequest::new(fixture_id, clone.clone()).unwrap();
    publish(&directory.join("request.json"), &envelope.encode().unwrap());
    wait_file(&directory.join("go")).await;
    let vm = clone.clone_request().vm();
    let port = FixtureProvisioningPort::new(
        directory.join("client.sock"),
        Duration::from_secs(1),
        FixtureProvisioningIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            request_sha256: envelope.request_sha256(),
            node: vm.node().as_str().into(),
            source_vmid: vm.source_vmid().get(),
            target_vmid: vm.target_vmid().get(),
        },
    )
    .unwrap();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(&port).await.unwrap();
    // Explicit test-process fault seam: the receipt remains only in worker A's
    // address space. The supervisor observes durable effect + absent journal
    // independently before killing us; no marker claims effect durability.
    if directory.join("pause-before-receipt").exists() {
        tokio::time::sleep(Duration::from_secs(30)).await;
        panic!("supervisor failed to terminate worker before receipt persistence");
    }
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_secs(30)).await;
    panic!("supervisor failed to terminate worker A within bound");
}

#[tokio::test]
#[ignore = "owned worker B subprocess entry point"]
async fn recovering_worker_b() {
    let (directory, store, operation, workflow) = child_context().await;
    let attempt: controller_domain::AttemptId =
        serde_json::from_slice(&fs::read(directory.join("attempt.json")).unwrap()).unwrap();
    let snapshot = store.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(snapshot.attempt_id(), Some(attempt));
    assert!(snapshot.dispatch().is_some());
    let envelope =
        FixtureCloneRequest::decode(&fs::read(directory.join("request.json")).unwrap()).unwrap();
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    let effect = reader
        .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
        .await
        .unwrap()
        .unwrap();
    let original = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
    assert_eq!(original.submission_sequence(), 1);
    let before_receipt = directory.join("pause-before-receipt").exists();
    assert_eq!(snapshot.receipt().is_none(), before_receipt);
    if let Some(receipt) = snapshot.receipt() {
        assert_eq!(receipt.receipt(), original.receipt());
    }
    let scheduler =
        Scheduler::new(store.clone(), ExecutorKind::Rust, 1, "process-worker-b").unwrap();
    assert!(
        scheduler
            .claim_osdeploy_bound(operation, &workflow, 1)
            .await
            .unwrap()
            .is_none()
    );
    let before = snapshot.revision();
    assert!(
        scheduler
            .resume_osdeploy_bound(operation, attempt, before, &workflow, 1)
            .await
            .unwrap()
            .is_none()
    );
    let observer = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(1))
        .connect(&std::env::var("PVA_WORKER_PROOF_DSN").unwrap())
        .await
        .unwrap();
    scheduler.reap_osdeploy_expired().await.unwrap();
    assert_eq!(
        store
            .load_osdeploy_operation(operation)
            .await
            .unwrap()
            .revision(),
        before
    );
    // Observe database time against the actual persisted deadline. No lease rows,
    // timestamps, or production durations are rewritten by this proof.
    tokio::time::timeout(Duration::from_secs(33), async {
        loop {
            let expired: bool = sqlx::query_scalar("SELECT lease_expires_at <= clock_timestamp() FROM rust_controller.worker_leases WHERE operation_id=$1")
                .bind(operation.as_uuid()).fetch_one(&observer).await.unwrap();
            if expired { break; }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }).await.expect("actual worker lease did not expire");
    scheduler.reap_osdeploy_expired().await.unwrap();
    let recovered = store.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(
        recovered.state(),
        controller_domain::ExecutionState::Unknown
    );
    assert_eq!(recovered.attempt_id(), Some(attempt));
    assert_eq!(recovered.receipt(), snapshot.receipt());
    let (event, revision): (EventId, i64) =
        serde_json::from_slice(&wait_file(&directory.join("reconciliation.json")).await).unwrap();
    assert_eq!(
        scheduler
            .reconcile_osdeploy_unknown(operation, attempt, revision, event, &workflow,)
            .await
            .unwrap(),
        if before_receipt {
            // A task-based dispatch without its journal receipt is explicitly
            // ineligible for reconciliation; scheduler returns without progress.
            postgres_store::OsDeployProgress::Idle
        } else {
            postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
        }
    );
    let reconciled = store.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(
        reconciled.state(),
        controller_domain::ExecutionState::Unknown
    );
    if before_receipt {
        assert_eq!(reconciled.revision(), revision);
    }
    let after = reader
        .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
        .await
        .unwrap()
        .unwrap();
    let after = envelope.decode_receipt(after.receipt().unwrap()).unwrap();
    assert_eq!(after.submission_sequence(), 1);
    assert_eq!(after.receipt(), original.receipt());
    assert_eq!(reconciled.attempt_id(), Some(attempt));
    assert_eq!(reconciled.receipt(), snapshot.receipt());
    assert!(
        scheduler
            .claim_osdeploy_bound(operation, &workflow, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        scheduler
            .resume_osdeploy_bound(operation, attempt, reconciled.revision(), &workflow, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        reader
            .accepted_effect(operation.as_uuid(), &"0".repeat(64))
            .await
            .is_err()
    );
}
