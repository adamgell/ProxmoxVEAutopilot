use pve_port::{
    PveAccessMode, PveApiToken, PveBaseUrl, PveObserverBuildError, PveObserverConfig,
    PveRequestAudit, ReqwestPveObserver,
};

#[test]
fn credentials_never_authorize_denied_targets_or_send_requests() {
    // Break: supplying a credential bypasses the target/mode permission check.
    for target in [
        "https://192.0.2.1:8006",
        "https://[::ffff:192.0.2.1]:8006",
        "https://denied-pve.invalid:8006",
    ] {
        for (mode, allow) in [
            (PveAccessMode::Observe, false),
            (PveAccessMode::Adapter, false),
            (PveAccessMode::Adapter, true),
            (PveAccessMode::Native, false),
            (PveAccessMode::Native, true),
        ] {
            let requests = PveRequestAudit::new();
            let config = PveObserverConfig::new(PveBaseUrl::parse(target).unwrap(), mode, allow)
                .with_api_token(
                    PveApiToken::parse("observer@pve!local-proof", "synthetic-secret-123").unwrap(),
                )
                .with_request_audit(requests.clone());
            let error = ReqwestPveObserver::new(config).unwrap_err();
            assert_eq!(error, PveObserverBuildError::TargetReadDenied);
            assert_eq!(requests.request_count(), 0);
            assert!(!format!("{error:?} {error}").contains("synthetic-secret-123"));
        }
    }
}

#[test]
fn production_address_is_rejected_before_http_client_construction() {
    let requests = PveRequestAudit::new();
    let config = PveObserverConfig::new(
        PveBaseUrl::parse("https://192.168.2.4:8006").unwrap(),
        PveAccessMode::Adapter,
        false,
    )
    .with_request_audit(requests.clone());

    let error = ReqwestPveObserver::new(config).unwrap_err();

    assert_eq!(error, PveObserverBuildError::TargetReadDenied);
    assert_eq!(requests.request_count(), 0);
}

#[test]
fn every_non_loopback_target_is_denied_without_explicit_observe_permission() {
    for target in [
        "https://192.168.2.4:8006",
        "https://[::ffff:192.168.2.4]:8006",
        "https://pve-production.invalid:8006",
    ] {
        let requests = PveRequestAudit::new();
        let config = PveObserverConfig::new(
            PveBaseUrl::parse(target).unwrap(),
            PveAccessMode::Observe,
            false,
        )
        .with_request_audit(requests.clone());

        assert!(ReqwestPveObserver::new(config).is_err(), "allowed {target}");
        assert_eq!(requests.request_count(), 0, "requested {target}");
    }
}

#[test]
fn adapter_permission_cannot_authorize_a_non_loopback_target() {
    let config = PveObserverConfig::new(
        PveBaseUrl::parse("https://pve-production.invalid:8006").unwrap(),
        PveAccessMode::Adapter,
        true,
    );

    assert!(ReqwestPveObserver::new(config).is_err());
}

#[test]
fn production_observe_requires_explicit_read_permission() {
    let denied = PveObserverConfig::new(
        PveBaseUrl::parse("https://192.168.2.4:8006").unwrap(),
        PveAccessMode::Observe,
        false,
    );
    let allowed = PveObserverConfig::new(
        PveBaseUrl::parse("https://192.168.2.4:8006").unwrap(),
        PveAccessMode::Observe,
        true,
    );

    assert_eq!(
        ReqwestPveObserver::new(denied).unwrap_err(),
        PveObserverBuildError::TargetReadDenied
    );
    assert!(ReqwestPveObserver::new(allowed).is_ok());
}

#[test]
fn normalized_literal_loopback_targets_remain_local_test_targets() {
    for target in [
        "http://127.0.0.1:8006",
        "http://[::1]:8006",
        "http://[::ffff:127.0.0.1]:8006",
    ] {
        let config = PveObserverConfig::new(
            PveBaseUrl::parse(target).unwrap(),
            PveAccessMode::Observe,
            false,
        );

        assert!(ReqwestPveObserver::new(config).is_ok(), "rejected {target}");
    }
}
