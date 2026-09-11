mod support;

use pve_port::{
    NodeName, PveAccessMode, PveApiToken, PveObserverConfig, PveReadError, PveReadPort,
    PveRequestAudit, ReqwestPveObserver, Vmid,
};
use std::time::Duration;
use support::Server;

#[test]
fn token_validation_and_debug_never_echo_input() {
    let token = PveApiToken::parse("observer@pve!local-proof", "synthetic-secret-123").unwrap();
    let debug = format!("{token:?}");
    assert_eq!(debug, "PveApiToken(<redacted>)");
    for (id, secret) in [
        ("observer@pve!local-proof\r\nInjected: yes", "valid"),
        ("observer@pve!local-proof", "secret\nvalue"),
        ("observer@pve!local-proof", ""),
        ("observer@pve!local-proof", "contains space"),
        ("observer@pve!local-proof", "value=other"),
        ("observer@pve!local-proof", "non-ascii-λ"),
        ("observer@pve!one!two", "valid"),
        ("observer!token", "valid"),
    ] {
        let error = PveApiToken::parse(id, secret).unwrap_err();
        assert_eq!(error.to_string(), "invalid PVE API token");
    }
}

#[tokio::test]
async fn token_is_sent_once_only_in_sensitive_authorization_header() {
    let server = Server::json(serde_json::json!({})).await;
    let audit = PveRequestAudit::new();
    let config = PveObserverConfig::new(server.base.clone(), PveAccessMode::Observe, false)
        .with_api_token(
            PveApiToken::parse("observer@pve!local-proof", "synthetic-secret-123").unwrap(),
        )
        .with_request_audit(audit.clone());
    assert!(!format!("{config:?}").contains("synthetic-secret-123"));
    let observer = ReqwestPveObserver::new(config).unwrap();
    assert!(!format!("{observer:?}").contains("synthetic-secret-123"));
    observer
        .vm_config(
            &NodeName::parse("pve-test").unwrap(),
            Vmid::new(9010).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(audit.request_count(), 1);
    {
        let requests = server.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let lines: Vec<_> = requests[0].split("\r\n").collect();
        assert!(lines[0] == "GET /api2/json/nodes/pve-test/qemu/9010/config HTTP/1.1");
        let auth: Vec<_> = lines
            .iter()
            .filter(|line| line.to_ascii_lowercase().starts_with("authorization:"))
            .collect();
        assert_eq!(auth.len(), 1);
        assert!(
            auth[0].split_once(':').unwrap().1.trim()
                == "PVEAPIToken=observer@pve!local-proof=synthetic-secret-123"
        );
        assert!(
            lines
                .iter()
                .filter(|line| !line.to_ascii_lowercase().starts_with("authorization:"))
                .all(|line| !line.contains("synthetic-secret-123"))
        );
    }
    server.finish().await;
}

#[tokio::test]
async fn redirect_cannot_forward_token_or_issue_second_request() {
    let second = Server::json(serde_json::json!({})).await;
    let first = Server::start(
        302,
        "{}".into(),
        format!("Location: {}\r\n", second.url),
        Duration::ZERO,
    )
    .await;
    let observer = ReqwestPveObserver::new(
        PveObserverConfig::new(first.base.clone(), PveAccessMode::Observe, false).with_api_token(
            PveApiToken::parse("observer@pve!local-proof", "synthetic-secret-123").unwrap(),
        ),
    )
    .unwrap();
    let result = observer
        .vm_config(
            &NodeName::parse("pve-test").unwrap(),
            Vmid::new(9010).unwrap(),
        )
        .await;
    assert_eq!(result.unwrap_err(), PveReadError::TransportUnavailable);
    assert_eq!(first.requests.lock().unwrap().len(), 1);
    assert!(second.requests.lock().unwrap().is_empty());
    first.finish().await;
    second.finish().await;
}

#[tokio::test]
async fn default_observer_sends_no_authorization() {
    let server = Server::json(serde_json::json!({})).await;
    server
        .observer()
        .vm_config(
            &NodeName::parse("pve-test").unwrap(),
            Vmid::new(9010).unwrap(),
        )
        .await
        .unwrap();
    assert!(
        !server.requests.lock().unwrap()[0]
            .to_ascii_lowercase()
            .contains("authorization:")
    );
    server.finish().await;
}
