#![cfg(unix)]
#[allow(dead_code)]
#[path = "../src/fixture_support/durable_fixture_log.rs"]
mod durable_fixture_log;
#[cfg(not(feature = "fixture-ipc"))]
#[path = "../src/fixture_support/fixture_daemon.rs"]
mod fixture_daemon;
#[cfg(not(feature = "fixture-ipc"))]
use durable_fixture_log::VmState;
#[cfg(feature = "fixture-ipc")]
use pve_port::fixture_support as fixture_daemon;
#[cfg(feature = "fixture-ipc")]
use pve_port::fixture_support::VmState;

use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{FileTypeExt, PermissionsExt},
        net::UnixStream,
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;
#[cfg(feature = "fixture-ipc")]
mod provisioning_seed_support;

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn bound_late_clone_consumes_exact_authorization_once() {
    bound_late_clone_case(false).await;
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn bound_late_clone_persistence_failure_creates_no_effect() {
    bound_late_clone_case(true).await;
}

#[cfg(feature = "fixture-ipc")]
async fn bound_late_clone_case(fail_consumption: bool) {
    use provisioning_seed_support::support as s;
    use pve_port::{fixture_ipc::FixtureCloneRequest, fixture_support::*, *};
    let mut daemon = Daemon::start();
    let socket = daemon.directory.join("client.sock");
    let supervisor = FixtureCheckpointClient::new(
        daemon.directory.join("supervisor.sock"),
        Duration::from_secs(1),
    )
    .unwrap();
    let worker = FixtureCheckpointClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
    let mutation = FixtureMutationClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
    let reads = FixtureReadClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
    let request = FixtureCloneRequest::new(
        Uuid::from_u128(1),
        CloneProvisioningRequestV1::new(
            s::binding(ProvisioningActionV1::Clone, 10, 0),
            s::plan(ProvisioningActionV1::Clone),
            s::clone_request(),
            s::before(s::source(), false),
            s::time(),
            30,
        )
        .unwrap(),
    )
    .unwrap();
    let vm = request.request().clone_request().vm();
    let identity = FixtureReadIdentity {
        fixture_id: request.fixture_id(),
        operation: request.request().binding().operation_id().as_uuid(),
        node: vm.node().as_str().into(),
        source_vmid: vm.source_vmid().get(),
        target_vmid: vm.target_vmid().get(),
    };
    let binding = CheckpointBinding {
        generation: supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .generation,
        owner: Uuid::now_v7(),
        operation: identity.operation,
        point: CheckpointPoint::DispatchCommitted,
    };
    assert!(
        supervisor
            .request(CheckpointRequest::ArmLate {
                binding,
                timeout_ms: 5000,
                identity: identity.clone()
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        worker
            .request(CheckpointRequest::Enter { binding })
            .await
            .unwrap()
            .ok
    );
    assert!(mutation.clone_vm_late(binding, &request).await.is_err());
    let proposal = LateCloneAuthorizationV1 {
        version: 1,
        binding,
        identity: identity.clone(),
        request_sha256: request.request_sha256(),
        request: request.encode().unwrap(),
        after: VmState {
            disk_bytes: 4096,
            pe_configured: false,
        },
    };
    assert!(
        supervisor
            .request(CheckpointRequest::AuthorizeRelease {
                proposal: proposal.clone(),
                committed_request: request.encode().unwrap()
            })
            .await
            .unwrap()
            .ok
    );
    for wrong in [
        CheckpointBinding {
            owner: Uuid::now_v7(),
            ..binding
        },
        CheckpointBinding {
            generation: Uuid::now_v7(),
            ..binding
        },
        CheckpointBinding {
            operation: Uuid::now_v7(),
            ..binding
        },
    ] {
        assert!(mutation.clone_vm_late(wrong, &request).await.is_err());
    }
    let altered = FixtureCloneRequest::new(Uuid::now_v7(), request.request().clone()).unwrap();
    assert!(mutation.clone_vm_late(binding, &altered).await.is_err());
    // The legacy command cannot consume a late authorization.
    assert!(mutation.clone_vm(&request).await.is_err());
    assert_eq!(reads.status().await.unwrap().attempts, 0);
    if fail_consumption {
        fs::rename(
            daemon.directory.join("checkpoint.json"),
            daemon.directory.join("checkpoint.saved.json"),
        )
        .unwrap();
        fs::create_dir(daemon.directory.join("checkpoint.json")).unwrap();
        assert!(mutation.clone_vm_late(binding, &request).await.is_err());
        daemon.await_failure();
        // The effect ledger remains empty because no seed capability escaped
        // the failed synchronized consumption transition.
        let ledger =
            durable_fixture_log::FixtureLog::recover(&daemon.directory.join("fixture.log"))
                .unwrap();
        assert!(ledger.records().is_empty());
        return;
    }
    let port = FixtureProvisioningPort::new_late(socket, Duration::from_secs(1), identity)
        .unwrap()
        .with_checkpoint(worker, binding)
        .unwrap();
    let receipt = port
        .submit_provisioning(&ProvisioningMutationRequestV1::Clone(
            request.request().clone(),
        ))
        .await
        .unwrap();
    let effect = reads
        .accepted_effect(binding.operation, &request.request_sha256())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        request
            .decode_receipt(effect.receipt().unwrap())
            .unwrap()
            .receipt(),
        &receipt
    );
    let persisted: serde_json::Value =
        serde_json::from_slice(&fs::read(daemon.directory.join("checkpoint.json")).unwrap())
            .unwrap();
    assert!(persisted["authorization"].is_null());
    assert_eq!(reads.status().await.unwrap().attempts, 1);
    assert_eq!(reads.status().await.unwrap().effects, 1);
    assert!(mutation.clone_vm_late(binding, &request).await.is_err());
    assert!(
        !supervisor
            .request(CheckpointRequest::AuthorizeRelease {
                proposal,
                committed_request: request.encode().unwrap()
            })
            .await
            .unwrap()
            .ok
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    assert!(mutation.clone_vm_late(binding, &request).await.is_err());
    assert_eq!(
        reads
            .accepted_effect(binding.operation, &request.request_sha256())
            .await
            .unwrap()
            .unwrap(),
        effect
    );
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn late_authorization_is_supervisor_owned_atomic_and_invalidated_on_restart() {
    use provisioning_seed_support::support as s;
    use pve_port::{fixture_ipc::FixtureCloneRequest, fixture_support::*, *};
    let mut daemon = Daemon::start();
    let supervisor = FixtureCheckpointClient::new(
        daemon.directory.join("supervisor.sock"),
        Duration::from_secs(1),
    )
    .unwrap();
    let worker =
        FixtureCheckpointClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    let request = FixtureCloneRequest::new(
        Uuid::from_u128(1),
        CloneProvisioningRequestV1::new(
            s::binding(ProvisioningActionV1::Clone, 10, 0),
            s::plan(ProvisioningActionV1::Clone),
            s::clone_request(),
            s::before(s::source(), false),
            s::time(),
            30,
        )
        .unwrap(),
    )
    .unwrap();
    let vm = request.request().clone_request().vm();
    let identity = FixtureReadIdentity {
        fixture_id: request.fixture_id(),
        operation: request.request().binding().operation_id().as_uuid(),
        node: vm.node().as_str().into(),
        source_vmid: vm.source_vmid().get(),
        target_vmid: vm.target_vmid().get(),
    };
    let binding = CheckpointBinding {
        generation: supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .generation,
        owner: Uuid::now_v7(),
        operation: identity.operation,
        point: CheckpointPoint::DispatchCommitted,
    };
    let arm = || CheckpointRequest::ArmLate {
        binding,
        timeout_ms: 5000,
        identity: identity.clone(),
    };
    assert!(!worker.request(arm()).await.unwrap().ok);
    assert!(supervisor.request(arm()).await.unwrap().ok);
    assert!(
        worker
            .request(CheckpointRequest::Enter { binding })
            .await
            .unwrap()
            .ok
    );
    assert!(
        !supervisor
            .request(CheckpointRequest::Release { binding })
            .await
            .unwrap()
            .ok
    );
    let proposal = LateCloneAuthorizationV1 {
        version: 1,
        binding,
        identity,
        request_sha256: request.request_sha256(),
        request: request.encode().unwrap(),
        after: VmState {
            disk_bytes: 4096,
            pe_configured: false,
        },
    };
    let mutation =
        FixtureMutationClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    assert!(mutation.clone_vm_late(binding, &request).await.is_err());
    let authorize = |proposal| CheckpointRequest::AuthorizeRelease {
        proposal,
        committed_request: request.encode().unwrap(),
    };
    assert!(
        !worker
            .request(authorize(proposal.clone()))
            .await
            .unwrap()
            .ok
    );
    let mut altered = proposal.clone();
    altered.request_sha256 = "0".repeat(64);
    assert!(!supervisor.request(authorize(altered)).await.unwrap().ok);
    assert_eq!(
        supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .phase,
        CheckpointPhase::Entered
    );
    let released = supervisor
        .request(authorize(proposal.clone()))
        .await
        .unwrap();
    assert!(released.ok);
    assert_eq!(released.state.phase, CheckpointPhase::Released);
    let persisted: serde_json::Value =
        serde_json::from_slice(&fs::read(daemon.directory.join("checkpoint.json")).unwrap())
            .unwrap();
    assert_eq!(persisted["state"]["phase"], "released");
    assert_eq!(
        persisted["authorization"]["request_sha256"],
        request.request_sha256()
    );
    assert!(
        !supervisor
            .request(authorize(proposal.clone()))
            .await
            .unwrap()
            .ok
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    assert!(mutation.clone_vm_late(binding, &request).await.is_err());
    assert!(
        !worker
            .request(CheckpointRequest::Poll { binding })
            .await
            .unwrap()
            .ok
    );
    assert!(
        !supervisor
            .request(authorize(proposal.clone()))
            .await
            .unwrap()
            .ok
    );
    let persisted: serde_json::Value =
        serde_json::from_slice(&fs::read(daemon.directory.join("checkpoint.json")).unwrap())
            .unwrap();
    assert!(persisted["authorization"].is_null());
    let fresh_binding = CheckpointBinding {
        generation: supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .generation,
        ..binding
    };
    assert!(
        supervisor
            .request(CheckpointRequest::ArmLate {
                binding: fresh_binding,
                timeout_ms: 20,
                identity: proposal.identity.clone(),
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        worker
            .request(CheckpointRequest::Enter {
                binding: fresh_binding
            })
            .await
            .unwrap()
            .ok
    );
    tokio::time::sleep(Duration::from_millis(30)).await;
    let mut expired = proposal;
    expired.binding = fresh_binding;
    assert!(
        !supervisor
            .request(authorize(expired.clone()))
            .await
            .unwrap()
            .ok
    );
    assert_eq!(
        supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .phase,
        CheckpointPhase::Expired
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    expired.binding.generation = supervisor
        .request(CheckpointRequest::Status)
        .await
        .unwrap()
        .state
        .generation;
    assert!(
        supervisor
            .request(CheckpointRequest::ArmLate {
                binding: expired.binding,
                timeout_ms: 5000,
                identity: expired.identity.clone(),
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        worker
            .request(CheckpointRequest::Enter {
                binding: expired.binding
            })
            .await
            .unwrap()
            .ok
    );
    // Replace only this test-owned persistence destination with a directory:
    // the release rename must fail before acknowledgement and terminate daemon.
    fs::rename(
        daemon.directory.join("checkpoint.json"),
        daemon.directory.join("checkpoint.before-failure.json"),
    )
    .unwrap();
    fs::create_dir(daemon.directory.join("checkpoint.json")).unwrap();
    assert!(supervisor.request(authorize(expired)).await.is_err());
    daemon.await_failure();
}

#[cfg(feature = "fixture-ipc")]
#[test]
fn missing_lock_or_invalid_coverage_prevents_daemon_startup() {
    for field in ["locked", "coverage"] {
        let mut daemon = Daemon::start();
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        let mut seed = serde_json::to_value(provisioning_seed_support::target_present()).unwrap();
        if field == "locked" {
            seed["source_power"]["value"]
                .as_object_mut()
                .unwrap()
                .remove("locked");
        } else {
            seed["target_coverage"]["value"] = serde_json::json!("unknown");
        }
        fs::write(
            daemon.directory.join("provisioning_reads.json"),
            serde_json::to_vec(&seed).unwrap(),
        )
        .unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_failure();
    }
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn target_presence_and_power_survive_restart_without_mutation() {
    use pve_port::fixture_support::FixtureReadClient;
    let mut daemon = Daemon::start();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    let mut previous = None;
    for seed in [
        provisioning_seed_support::target_present(),
        provisioning_seed_support::target_absent(),
    ] {
        fs::write(
            daemon.directory.join("provisioning_reads.json"),
            serde_json::to_vec(&seed).unwrap(),
        )
        .unwrap();
        assert_eq!(
            client.provisioning_reads(&seed.identity).await.unwrap(),
            previous
        );
        for _ in 0..2 {
            daemon.child.kill().unwrap();
            daemon.child.wait().unwrap();
            daemon.clear_stale_sockets().unwrap();
            daemon.child = Daemon::spawn(&daemon.directory);
            daemon.await_ready();
            assert_eq!(
                client.provisioning_reads(&seed.identity).await.unwrap(),
                Some(seed.clone())
            );
            assert_eq!(client.status().await.unwrap().attempts, 0);
        }
        previous = Some(seed);
    }
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn provisioning_reads_preserve_populated_facts_errors_and_restart_identity() {
    use pve_port::fixture_support::FixtureReadClient;
    let mut daemon = Daemon::start();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    let seed = provisioning_seed_support::populated();
    assert_eq!(
        client.provisioning_reads(&seed.identity).await.unwrap(),
        None
    );
    let path = daemon.directory.join("provisioning_reads.json");
    fs::write(&path, serde_json::to_vec(&seed).unwrap()).unwrap();
    assert_eq!(
        client.provisioning_reads(&seed.identity).await.unwrap(),
        None
    );
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(
            client.provisioning_reads(&seed.identity).await.unwrap(),
            Some(seed.clone())
        );
        let mut mismatches = vec![seed.identity.clone(); 6];
        mismatches[0].fixture_id = Uuid::now_v7();
        mismatches[1].operation = Uuid::now_v7();
        mismatches[2].request_sha256 = "b".repeat(64);
        mismatches[3].node = "other-node".into();
        mismatches[4].source_vmid = 901;
        mismatches[5].target_vmid = 102;
        for identity in mismatches {
            assert!(client.provisioning_reads(&identity).await.is_err());
        }
        assert_eq!(client.status().await.unwrap().attempts, 0);
    }
    fs::write(path, b"{}").unwrap();
    assert_eq!(
        client.provisioning_reads(&seed.identity).await.unwrap(),
        Some(seed)
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn supervisor_checkpoint_release_timeout_and_restart_are_owned() {
    use fixture_daemon::{
        CheckpointBinding, CheckpointPhase, CheckpointPoint, CheckpointRequest as R,
        FixtureCheckpointClient as Client,
    };
    let mut daemon = Daemon::start();
    let supervisor = Client::new(
        daemon.directory.join("supervisor.sock"),
        Duration::from_secs(2),
    )
    .unwrap();
    let worker = Client::new(daemon.directory.join("client.sock"), Duration::from_secs(2)).unwrap();
    // Socket path creation precedes listen readiness; use the protocol handshake.
    let ready_deadline = Instant::now() + Duration::from_secs(2);
    let state = loop {
        match supervisor.request(R::Status).await {
            Ok(reply) => break reply.state,
            Err(error)
                if error.kind() == std::io::ErrorKind::ConnectionRefused
                    && Instant::now() < ready_deadline =>
            {
                tokio::time::sleep(Duration::from_millis(5)).await
            }
            Err(error) => panic!("checkpoint readiness failed: {error}"),
        }
    };
    let binding = CheckpointBinding {
        generation: state.generation,
        owner: Uuid::now_v7(),
        operation: Uuid::now_v7(),
        point: CheckpointPoint::DispatchCommitted,
    };
    assert!(
        !worker
            .request(R::Arm {
                binding,
                timeout_ms: 1000
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        supervisor
            .request(R::Arm {
                binding,
                timeout_ms: 1000
            })
            .await
            .unwrap()
            .ok
    );
    let port = fixture_daemon::FixtureProvisioningPort::new(
        daemon.directory.join("client.sock"),
        Duration::from_secs(2),
        fixture_daemon::FixtureProvisioningIdentity {
            fixture_id: Uuid::now_v7(),
            operation: binding.operation,
            request_sha256: "a".repeat(64),
            node: "fixture-node".into(),
            source_vmid: 100,
            target_vmid: 101,
        },
    )
    .unwrap()
    .with_checkpoint(worker, binding)
    .unwrap();
    let port: std::sync::Arc<dyn pve_port::fixture_ipc::ControllerFixturePort> =
        std::sync::Arc::new(port);
    let pending = tokio::spawn(async move {
        port.controller_checkpoint(pve_port::FakeControllerCheckpoint::DispatchCommitted)
            .await
    });
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if supervisor.request(R::Status).await.unwrap().state.phase == CheckpointPhase::Entered {
            break;
        }
        assert!(Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(!pending.is_finished());
    let persisted: fixture_daemon::CheckpointState = serde_json::from_value(
        serde_json::from_slice::<serde_json::Value>(
            &fs::read(daemon.directory.join("checkpoint.json")).unwrap(),
        )
        .unwrap()["state"]
            .clone(),
    )
    .unwrap();
    assert_eq!(persisted.phase, CheckpointPhase::Entered);
    let stale = CheckpointBinding {
        owner: Uuid::now_v7(),
        ..binding
    };
    assert!(
        !supervisor
            .request(R::Release { binding: stale })
            .await
            .unwrap()
            .ok
    );
    assert!(supervisor.request(R::Release { binding }).await.unwrap().ok);
    pending.await.unwrap().unwrap();
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    assert!(!supervisor.request(R::Release { binding }).await.unwrap().ok);
    let fresh = supervisor.request(R::Status).await.unwrap().state;
    assert_ne!(fresh.generation, binding.generation);
    assert_eq!(fresh.phase, CheckpointPhase::Idle);
    let fresh_binding = CheckpointBinding {
        generation: fresh.generation,
        ..binding
    };
    assert!(
        supervisor
            .request(R::Arm {
                binding: fresh_binding,
                timeout_ms: 20
            })
            .await
            .unwrap()
            .ok
    );
    let worker = Client::new(daemon.directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert!(worker.checkpoint(fresh_binding).await.is_err());
    assert_eq!(
        supervisor.request(R::Status).await.unwrap().state.phase,
        CheckpointPhase::Expired
    );
    let status = daemon.request("client.sock", br#"{"command":"status"}"#);
    assert_eq!((status.attempts, status.effects), (0, 0));
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn stable_v2_collection_is_versioned_and_survives_restart_without_request_digest() {
    use pve_port::fixture_support::{
        FixtureProvisioningPort, FixtureProvisioningReadsV2, FixtureReadClient, FixtureReadIdentity,
    };
    use pve_port::{NodeName, ProvisioningFakePort, Vmid};
    let mut daemon = Daemon::start();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    let old = provisioning_seed_support::populated();
    let identity = FixtureReadIdentity {
        fixture_id: old.identity.fixture_id,
        operation: old.identity.operation,
        node: old.identity.node.clone(),
        source_vmid: old.identity.source_vmid,
        target_vmid: old.identity.target_vmid,
    };
    let mut value = serde_json::to_value(&old).unwrap();
    value["version"] = 2.into();
    value["identity"] = serde_json::to_value(&identity).unwrap();
    let bytes = serde_json::to_vec(&value).unwrap();
    let seed = FixtureProvisioningReadsV2::decode_v2(&bytes, &identity).unwrap();
    assert!(
        pve_port::fixture_support::FixtureProvisioningReads::decode(&bytes, &old.identity).is_err()
    );
    assert!(
        FixtureProvisioningReadsV2::decode_v2(&serde_json::to_vec(&old).unwrap(), &identity)
            .is_err()
    );
    let mut injected = value.clone();
    injected["identity"]["request_sha256"] = "a".repeat(64).into();
    assert!(
        FixtureProvisioningReadsV2::decode_v2(&serde_json::to_vec(&injected).unwrap(), &identity)
            .is_err()
    );
    assert_eq!(client.provisioning_reads_v2(&identity).await.unwrap(), None);
    fs::write(daemon.directory.join("provisioning_reads_v2.json"), bytes).unwrap();
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(
            client.provisioning_reads_v2(&identity).await.unwrap(),
            Some(seed.clone())
        );
        assert_eq!(
            client.provisioning_reads(&old.identity).await.unwrap(),
            None
        );
        let mut mismatches = vec![identity.clone(); 5];
        mismatches[0].fixture_id = Uuid::now_v7();
        mismatches[1].operation = Uuid::now_v7();
        mismatches[2].node = "other-node".into();
        mismatches[3].source_vmid = 901;
        mismatches[4].target_vmid = 102;
        for mismatch in mismatches {
            assert!(client.provisioning_reads_v2(&mismatch).await.is_err());
        }
        let port = FixtureProvisioningPort::new_late(
            daemon.directory.join("client.sock"),
            Duration::from_secs(1),
            identity.clone(),
        )
        .unwrap();
        let config = port
            .provisioning_vm_config(
                &NodeName::parse(&identity.node).unwrap(),
                Vmid::new(identity.source_vmid).unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(config.vmid().get(), identity.source_vmid);
        assert_eq!(client.status().await.unwrap().attempts, 0);
    }
}

struct Daemon {
    directory: PathBuf,
    child: Child,
}
impl Daemon {
    fn spawn(directory: &std::path::Path) -> Child {
        Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "fixture_daemon_child"])
            .env("PVE_TEST_FIXTURE_DIR", directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap()
    }

    /// Only the supervisor holding the original child handle may remove endpoints.
    /// An unresponsive socket alone never proves the old process has exited.
    fn clear_stale_sockets(&mut self) -> std::io::Result<()> {
        if self.child.try_wait()?.is_none() {
            return Err(std::io::Error::other("fixture process is still alive"));
        }
        let paths = [
            self.directory.join("client.sock"),
            self.directory.join("supervisor.sock"),
        ];
        // Validate both before deleting either; do not follow symlinks.
        for path in &paths {
            if !fs::symlink_metadata(path)?.file_type().is_socket() {
                return Err(std::io::Error::other("fixture endpoint is not a socket"));
            }
        }
        for path in paths {
            fs::remove_file(path)?;
        }
        Ok(())
    }

    fn await_ready(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while UnixStream::connect(self.directory.join("client.sock")).is_err()
            || UnixStream::connect(self.directory.join("supervisor.sock")).is_err()
        {
            assert!(
                self.child.try_wait().unwrap().is_none(),
                "fixture exited during startup"
            );
            assert!(Instant::now() < deadline, "fixture startup timed out");
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn await_failure(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(!status.success());
                return;
            }
            assert!(
                Instant::now() < deadline,
                "expected startup failure timed out"
            );
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn start() -> Self {
        // macOS temp_dir paths can exceed sockaddr_un's small pathname limit.
        let directory = PathBuf::from("/tmp").join(format!("pve-ipc-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let child = Self::spawn(&directory);
        let mut daemon = Self { directory, child };
        daemon.await_ready();
        daemon
    }
    fn request(&self, socket: &str, payload: &[u8]) -> fixture_daemon::Reply {
        let mut stream = UnixStream::connect(self.directory.join(socket)).unwrap();
        // macOS may reject SO_RCVTIMEO for Unix-domain streams with EINVAL;
        // the daemon still enforces the authoritative bounded read deadline.
        if let Err(error) = stream.set_read_timeout(Some(Duration::from_secs(1))) {
            assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        }
        stream
            .write_all(&(payload.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(payload).unwrap();
        let mut header = [0; 4];
        stream.read_exact(&mut header).unwrap();
        let length = u32::from_be_bytes(header) as usize;
        assert!(length <= 1024);
        let mut bytes = vec![0; length];
        stream.read_exact(&mut bytes).unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.directory);
    }
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn snapshot_is_daemon_owned_and_survives_restart() {
    use pve_port::fixture_support::{
        FixtureInventory, FixtureReadClient, FixtureSnapshot, FixtureVmConfig,
    };
    let mut daemon = Daemon::start();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    assert_eq!(
        client.snapshot().await.unwrap(),
        FixtureSnapshot::Unavailable {}
    );
    let inventory = FixtureSnapshot::Inventory {
        inventory: FixtureInventory {
            version: 1,
            fixture_id: Uuid::now_v7(),
            node: "fixture-node".into(),
            observed_unix_ms: 123,
            vms: vec![FixtureVmConfig {
                vmid: 900,
                name: "template".into(),
                template: true,
                disk_bytes: 1024,
            }],
        },
    };
    fs::write(
        daemon.directory.join("inventory.json"),
        serde_json::to_vec(&inventory).unwrap(),
    )
    .unwrap();
    // The daemon owns its startup observation; changing the seed is not a live update.
    assert_eq!(
        client.snapshot().await.unwrap(),
        FixtureSnapshot::Unavailable {}
    );
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(client.snapshot().await.unwrap(), inventory);
    }
    fs::write(daemon.directory.join("inventory.json"), b"{}").unwrap();
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
}

#[test]
#[ignore = "subprocess entry point only"]
fn fixture_daemon_child() {
    let path = PathBuf::from(std::env::var_os("PVE_TEST_FIXTURE_DIR").expect("fixture directory"));
    fixture_daemon::run(&path, Duration::from_secs(5)).unwrap();
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn clone_reads_preserve_seed_errors_identity_and_restart_observations() {
    use pve_port::fixture_support::{FixtureCloneReads, FixtureReadClient};
    let mut daemon = Daemon::start();
    let id = Uuid::now_v7();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    assert!(client.clone_reads(id).await.unwrap().is_none());
    assert!(client.clone_reads(Uuid::nil()).await.is_err());
    let seed = serde_json::json!({
        "version":1,"fixture_id":id,"node":"fixture-node",
        "node_status":{"state":"observed","observed_unix_ms":123,"value":{"online":true,"uptime_seconds":4}},
        "storage":{"state":"error","observed_unix_ms":124,"error":"forbidden"},
        "bridges":{"state":"observed","observed_unix_ms":125,"value":[]},
        "cluster_inventory":{"state":"observed","observed_unix_ms":126,"value":[{
            "node":"other-node","vmid":777,"name":"unrelated","template":false,
            "config_sha256":"a".repeat(64),"uuid":Uuid::from_u128(777),
            "mac":"02:00:00:00:00:77","primary_storage":"local-lvm","primary_volume":"vm-777-disk-0",
            "coverage":{"state":"observed","observed_unix_ms":126,"value":"partial"},
            "status":{"state":"observed","observed_unix_ms":126,"value":"running"}
        }]}
    });
    let bytes = serde_json::to_vec(&seed).unwrap();
    let expected = FixtureCloneReads::decode(&bytes, id).unwrap();
    fs::write(daemon.directory.join("clone_reads.json"), &bytes).unwrap();
    assert!(client.clone_reads(id).await.unwrap().is_none());
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(
            client.clone_reads(id).await.unwrap(),
            Some(expected.clone())
        );
        assert!(client.clone_reads(Uuid::now_v7()).await.is_err());
        let request = serde_json::to_vec(
            &serde_json::json!({"command":"clone_reads","fixture_id":id,"extra":true}),
        )
        .unwrap();
        assert!(!daemon.request("client.sock", &request).ok);
        assert_eq!(client.status().await.unwrap().attempts, 0);
    }
    fs::write(daemon.directory.join("clone_reads.json"), b"{}").unwrap();
    assert_eq!(client.clone_reads(id).await.unwrap(), Some(expected));
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn read_client_observes_daemon_owned_effect_without_writing() {
    let daemon = Daemon::start();
    let client = pve_port::fixture_support::FixtureReadClient::new(
        daemon.directory.join("client.sock"),
        Duration::from_secs(1),
    )
    .unwrap();
    let operation = Uuid::now_v7();
    let digest = "a".repeat(64);
    assert_eq!(client.status().await.unwrap().attempts, 0);
    assert!(client.world(100).await.unwrap().is_none());
    assert!(
        client
            .accepted_effect(operation, &digest)
            .await
            .unwrap()
            .is_none()
    );
    let attempt = serde_json::to_vec(
        &serde_json::json!({"command":"attempt", "operation":operation, "request_sha256":digest}),
    )
    .unwrap();
    assert!(daemon.request("client.sock", &attempt).ok);
    let accepted = daemon.request("client.sock", br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#);
    assert!(accepted.ok);
    let ledger_before = fs::read(daemon.directory.join("fixture.log")).unwrap();
    assert_eq!(client.world(100).await.unwrap(), accepted.vm);
    assert!(
        client
            .accepted_effect(operation, &digest)
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(client.status().await.unwrap().effects, 1);
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        ledger_before
    );
}

#[test]
fn delayed_payload_is_read_within_the_frame_deadline() {
    let daemon = Daemon::start();
    let mut stream = UnixStream::connect(daemon.directory.join("client.sock")).unwrap();
    let payload = br#"{"command":"status"}"#;
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .unwrap();
    // Allow the daemon to accept and consume the header before the payload
    // arrives. An inherited nonblocking stream would close this valid request.
    thread::sleep(Duration::from_millis(20));
    stream.write_all(payload).unwrap();
    let mut header = [0; 4];
    stream.read_exact(&mut header).unwrap();
    let length = u32::from_be_bytes(header) as usize;
    assert!(length <= 1024);
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).unwrap();
    let reply: fixture_daemon::Reply = serde_json::from_slice(&bytes).unwrap();
    assert!(reply.ok);
    assert_eq!(reply.attempts, 0);
}

#[test]
fn separate_process_enforces_control_boundary_and_durable_duplicate_ledger() {
    let mut daemon = Daemon::start();
    assert!(
        !daemon
            .request("client.sock", br#"{"command":"shutdown"}"#)
            .ok
    );
    assert!(
        !daemon
            .request("supervisor.sock", br#"{"command":"status"}"#)
            .ok
    );
    assert!(
        !daemon
            .request("client.sock", br#"{"command":"status","extra":true}"#)
            .ok
    );
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    let first = daemon.request("client.sock", &attempt);
    assert!(first.ok);
    assert_eq!(first.duplicate, Some(false));
    let second = daemon.request("client.sock", &attempt);
    assert!(second.ok);
    assert_eq!(second.duplicate, Some(true));
    assert_eq!(second.attempts, 2);
    assert!(
        daemon
            .request("supervisor.sock", br#"{"command":"shutdown"}"#)
            .ok
    );
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if let Some(status) = daemon.child.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline, "shutdown timed out");
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        durable_fixture_log::FixtureLog::recover(&daemon.directory.join("fixture.log"))
            .unwrap()
            .records()
            .len(),
        2
    );
}

#[test]
fn oversized_and_partial_requests_cannot_mutate_or_stall_daemon() {
    let daemon = Daemon::start();
    for header in [2048_u32, 12] {
        let mut stream = UnixStream::connect(daemon.directory.join("client.sock")).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        stream.write_all(&header.to_be_bytes()).unwrap();
        let mut byte = [0];
        assert!(matches!(stream.read(&mut byte), Ok(0) | Err(_)));
    }
    let status = daemon.request("client.sock", br#"{"command":"status"}"#);
    assert!(status.ok);
    assert_eq!(status.attempts, 0);
}

#[test]
fn killed_daemon_rebind_requires_supervisor_cleanup_and_preserves_ledger() {
    let mut daemon = Daemon::start();
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(false)
    );
    assert!(daemon.clear_stale_sockets().is_err());
    assert_eq!(
        daemon
            .request("client.sock", br#"{"command":"status"}"#)
            .attempts,
        1
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    let original = fs::read(daemon.directory.join("fixture.log")).unwrap();
    // The daemon cannot infer that existing endpoints are safe to unlink.
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        original
    );
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    assert_eq!(
        daemon
            .request("client.sock", br#"{"command":"status"}"#)
            .attempts,
        1
    );
    let duplicate = daemon.request("client.sock", &attempt);
    assert_eq!(duplicate.duplicate, Some(true));
    assert_eq!(duplicate.attempts, 2);
}

#[test]
fn endpoint_substitution_and_corrupt_restart_fail_closed() {
    let mut daemon = Daemon::start();
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    let endpoint = daemon.directory.join("supervisor.sock");
    fs::remove_file(&endpoint).unwrap();
    fs::write(&endpoint, b"not a socket").unwrap();
    assert!(daemon.clear_stale_sockets().is_err());
    assert!(daemon.directory.join("client.sock").exists());
    assert_eq!(fs::read(&endpoint).unwrap(), b"not a socket");
    fs::remove_file(&endpoint).unwrap();
    let _socket = std::os::unix::net::UnixListener::bind(&endpoint).unwrap();
    daemon.clear_stale_sockets().unwrap();
    fs::write(daemon.directory.join("fixture.log"), b"partial").unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        b"partial"
    );
    assert!(!daemon.directory.join("client.sock").exists());
    assert!(!daemon.directory.join("supervisor.sock").exists());
}

#[test]
fn effects_commit_replay_and_reject_duplicates_over_ipc() {
    let mut daemon = Daemon::start();
    let effect = br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#;
    assert!(!daemon.request("client.sock", effect).ok);
    assert!(!daemon.request("supervisor.sock", effect).ok);
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(false)
    );
    let accepted = daemon.request("client.sock", effect);
    assert!(accepted.ok);
    assert_eq!(accepted.effects, 1);
    assert_eq!(
        accepted.vm,
        Some(VmState {
            disk_bytes: 80,
            pe_configured: false
        })
    );
    assert!(!daemon.request("client.sock", effect).ok);
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(true)
    );
    let duplicate_effect = br#"{"command":"effect","attempt_sequence":2,"vmid":100,"before":{"disk_bytes":80,"pe_configured":false},"after":{"disk_bytes":120,"pe_configured":true}}"#;
    assert!(!daemon.request("client.sock", duplicate_effect).ok);
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    let world = daemon.request("client.sock", br#"{"command":"world","vmid":100}"#);
    assert!(world.ok);
    assert_eq!(world.attempts, 2);
    assert_eq!(world.effects, 1);
    assert_eq!(world.vm, accepted.vm);
    assert!(!daemon.request("client.sock", effect).ok);
    assert!(
        !daemon
            .request("supervisor.sock", br#"{"command":"world","vmid":100}"#)
            .ok
    );
    // A new operation may change only the exact recovered prior state.
    let next = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"b".repeat(64)})).unwrap();
    assert_eq!(daemon.request("client.sock", &next).attempts, 3);
    let stale = br#"{"command":"effect","attempt_sequence":3,"vmid":100,"before":null,"after":{"disk_bytes":120,"pe_configured":true}}"#;
    assert!(!daemon.request("client.sock", stale).ok);
    let transition = br#"{"command":"effect","attempt_sequence":3,"vmid":100,"before":{"disk_bytes":80,"pe_configured":false},"after":{"disk_bytes":120,"pe_configured":true}}"#;
    let updated = daemon.request("client.sock", transition);
    assert!(updated.ok);
    assert_eq!(updated.effects, 2);
    assert_eq!(
        updated.vm,
        Some(VmState {
            disk_bytes: 120,
            pe_configured: true
        })
    );
}

#[test]
fn accepted_effect_lookup_binds_request_and_survives_restart() {
    let mut daemon = Daemon::start();
    let operation = Uuid::now_v7();
    let digest = "a".repeat(64);
    let query = serde_json::to_vec(&serde_json::json!({"command":"accepted_effect", "operation":operation, "request_sha256":digest})).unwrap();
    let absent = daemon.request("client.sock", &query);
    assert!(absent.ok);
    assert!(absent.accepted_effect.is_none());
    assert!(!daemon.request("supervisor.sock", &query).ok);
    let attempt = serde_json::to_vec(
        &serde_json::json!({"command":"attempt", "operation":operation, "request_sha256":digest}),
    )
    .unwrap();
    assert!(daemon.request("client.sock", &attempt).ok);
    assert!(
        daemon
            .request("client.sock", &query)
            .accepted_effect
            .is_none()
    );
    let wrong = serde_json::to_vec(&serde_json::json!({"command":"accepted_effect", "operation":operation, "request_sha256":"b".repeat(64)})).unwrap();
    assert!(!daemon.request("client.sock", &wrong).ok);
    let effect = br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#;
    assert!(daemon.request("client.sock", effect).ok);
    let accepted = daemon.request("client.sock", &query);
    assert!(accepted.ok);
    let facts = serde_json::to_value(accepted.accepted_effect.as_ref().unwrap()).unwrap();
    assert_eq!(facts["operation"], operation.to_string());
    assert_eq!(facts["request_sha256"], digest);
    assert_eq!(facts["vmid"], 100);
    assert_eq!(facts["attempt_sequence"], 1);
    assert_eq!(facts["after"]["disk_bytes"], 80);
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    let recovered = daemon.request("client.sock", &query);
    assert!(recovered.ok);
    assert_eq!(recovered.accepted_effect, accepted.accepted_effect);
    assert_eq!(recovered.attempts, 1);
    assert_eq!(recovered.effects, 1);
    assert!(!daemon.request("client.sock", &wrong).ok);
}
