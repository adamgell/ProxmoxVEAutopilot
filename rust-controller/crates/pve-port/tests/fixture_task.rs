#![cfg(all(unix, feature = "fixture-ipc"))]
use pve_port::fixture_support::{
    FixtureReadClient, FixtureTaskIdentity, FixtureTaskObservation, FixtureTaskState, run,
};
use std::{os::unix::fs::PermissionsExt, time::Duration};
use uuid::Uuid;

fn observation() -> FixtureTaskObservation {
    FixtureTaskObservation {
        version: 1,
        identity: FixtureTaskIdentity {
            fixture_id: Uuid::now_v7(),
            node: "fixture".into(),
            operation: Uuid::now_v7(),
            request_sha256: "a".repeat(64),
            upid: "UPID:fixture:0001:0002:0003:qmclone:101:root@pam:".into(),
        },
        observed_unix_ms: 123,
        result: FixtureTaskState::Running {},
    }
}

#[test]
fn task_envelope_rejects_unknown_and_invalid_data() {
    let base = observation();
    for state in [
        FixtureTaskState::Absent {},
        FixtureTaskState::Running {},
        FixtureTaskState::Succeeded {},
        FixtureTaskState::Failed {
            reason: "clone failed".into(),
        },
    ] {
        let mut value = base.clone();
        value.result = state;
        assert_eq!(
            FixtureTaskObservation::decode(&serde_json::to_vec(&value).unwrap()).unwrap(),
            value
        );
    }
    let original = serde_json::to_value(base).unwrap();
    for (path, value) in [
        ("version", serde_json::json!(2)),
        ("observed_unix_ms", serde_json::json!(0)),
        ("extra", serde_json::json!(true)),
    ] {
        let mut bad = original.clone();
        bad[path] = value;
        assert!(FixtureTaskObservation::decode(&serde_json::to_vec(&bad).unwrap()).is_err());
    }
    for field in [
        "fixture_id",
        "node",
        "operation",
        "request_sha256",
        "upid",
        "extra",
    ] {
        let mut bad = original.clone();
        bad["identity"][field] = "invalid".into();
        assert!(FixtureTaskObservation::decode(&serde_json::to_vec(&bad).unwrap()).is_err());
    }
    assert!(FixtureTaskObservation::decode(&vec![b' '; 4097]).is_err());
}

#[tokio::test]
async fn task_observation_survives_restart_and_binds_all_identity_fields() {
    let directory = std::path::PathBuf::from("/tmp").join(format!("pve-task-{}", Uuid::now_v7()));
    std::fs::create_dir(&directory).unwrap();
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    let value = observation();
    std::fs::write(
        directory.join("task.json"),
        serde_json::to_vec(&value).unwrap(),
    )
    .unwrap();
    for _ in 0..2 {
        let root = directory.clone();
        let server = std::thread::spawn(move || run(&root, Duration::from_millis(500)).unwrap());
        let socket = directory.join("client.sock");
        for _ in 0..100 {
            if socket.exists() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        let client = FixtureReadClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        assert_eq!(client.task(&value.identity).await.unwrap(), value);
        for field in 0..5 {
            let mut wrong = value.identity.clone();
            match field {
                0 => wrong.fixture_id = Uuid::now_v7(),
                1 => {
                    wrong.node = "other".into();
                    wrong.upid = wrong.upid.replace("fixture", "other");
                }
                2 => wrong.operation = Uuid::now_v7(),
                3 => wrong.request_sha256 = "b".repeat(64),
                _ => wrong.upid = wrong.upid.replace("0001", "0009"),
            }
            assert!(client.task(&wrong).await.is_err());
        }
        assert_eq!(client.status().await.unwrap().attempts, 0);
        server.join().unwrap();
        std::fs::remove_file(socket).unwrap();
        std::fs::remove_file(directory.join("supervisor.sock")).unwrap();
    }
    std::fs::remove_dir_all(directory).unwrap();
}
