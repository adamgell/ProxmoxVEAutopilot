use pve_port::{
    PveAccessMode, PveBaseUrl, PveObserverBuildError, PveObserverConfig, PveRequestAudit,
    ReqwestPveObserver,
};

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

    assert_eq!(error, PveObserverBuildError::ProductionReadDenied);
    assert_eq!(requests.request_count(), 0);
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
        PveObserverBuildError::ProductionReadDenied
    );
    assert!(ReqwestPveObserver::new(allowed).is_ok());
}
