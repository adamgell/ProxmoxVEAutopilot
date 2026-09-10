#![cfg(all(unix, feature = "fixture-ipc"))]
use pve_port::fixture_support::FixtureReadClient;
use std::{io, path::PathBuf, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixListener,
};
use uuid::Uuid;

struct Endpoint(PathBuf);
impl Drop for Endpoint {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn reply_test(payload: Vec<u8>, expected: Option<io::ErrorKind>) {
    let endpoint = Endpoint(PathBuf::from(format!("/tmp/pve-read-{}", Uuid::now_v7())));
    let listener = UnixListener::bind(&endpoint.0).unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let size = stream.read_u32().await.unwrap();
        let mut request = vec![0; size as usize];
        stream.read_exact(&mut request).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&request).unwrap(),
            serde_json::json!({"command":"status"})
        );
        stream.write_all(&payload).await.unwrap();
    });
    let client = FixtureReadClient::new(endpoint.0.clone(), Duration::from_secs(1)).unwrap();
    let result = client.status().await;
    match expected {
        Some(kind) => assert_eq!(result.unwrap_err().kind(), kind),
        None => {
            let status = result.unwrap();
            assert_eq!((status.attempts, status.effects), (2, 1));
        }
    }
    server.await.unwrap();
}

fn frame(value: serde_json::Value) -> Vec<u8> {
    let body = serde_json::to_vec(&value).unwrap();
    let mut bytes = (body.len() as u32).to_be_bytes().to_vec();
    bytes.extend(body);
    bytes
}

#[tokio::test]
async fn status_and_invalid_replies() {
    let valid = serde_json::json!({"ok":true,"attempts":2,"effects":1,"duplicate":null,"vm":null,"accepted_effect":null});
    reply_test(frame(valid.clone()), None).await;
    let mut denied = valid.clone();
    denied["ok"] = false.into();
    reply_test(frame(denied), Some(io::ErrorKind::PermissionDenied)).await;
    let mut extra = valid.clone();
    extra["unknown"] = true.into();
    reply_test(frame(extra), Some(io::ErrorKind::InvalidData)).await;
    let mut wrong_shape = valid;
    wrong_shape["duplicate"] = false.into();
    reply_test(frame(wrong_shape), Some(io::ErrorKind::InvalidData)).await;
    reply_test(
        4097_u32.to_be_bytes().to_vec(),
        Some(io::ErrorKind::InvalidData),
    )
    .await;
    reply_test(
        0_u32.to_be_bytes().to_vec(),
        Some(io::ErrorKind::InvalidData),
    )
    .await;
    reply_test(vec![0, 0, 0, 10, b'{'], Some(io::ErrorKind::UnexpectedEof)).await;
}

#[tokio::test]
async fn stalled_peer_and_input_bounds() {
    let endpoint = Endpoint(PathBuf::from(format!("/tmp/pve-read-{}", Uuid::now_v7())));
    let listener = UnixListener::bind(&endpoint.0).unwrap();
    let client = FixtureReadClient::new(endpoint.0.clone(), Duration::from_millis(30)).unwrap();
    let server = tokio::spawn(async move {
        let (_stream, _) = listener.accept().await.unwrap();
        std::future::pending::<()>().await;
    });
    assert_eq!(
        client.status().await.unwrap_err().kind(),
        io::ErrorKind::TimedOut
    );
    server.abort();
    assert_eq!(
        client.world(0).await.unwrap_err().kind(),
        io::ErrorKind::InvalidInput
    );
    assert_eq!(
        client
            .accepted_effect(Uuid::nil(), &"a".repeat(64))
            .await
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidInput
    );
    assert!(FixtureReadClient::new(endpoint.0.clone(), Duration::ZERO).is_err());
    assert!(FixtureReadClient::new(endpoint.0.clone(), Duration::from_secs(11)).is_err());
}
