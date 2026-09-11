use osdeploy_adapter::*;
use uuid::Uuid;

#[test]
fn host_claims_never_authenticate_agent_or_authorize_installation() {
    let scope =
        InstallQgaScopeV1::new([1, 2, 3, 4, 5].map(Uuid::from_u128), "node1".into(), 109).unwrap();
    assert_eq!(
        scope.assess(&scope, None, 2_000_000),
        Ok(InstallQgaRefusal::MissingHostEvidence)
    );
    let claim = HostQgaClaimV1 {
        node: "node1".into(),
        vmid: 109,
        responsive: true,
        observed_unix_micros: 1_000_000,
    };
    for _ in 0..2 {
        assert_eq!(
            scope.assess(&scope, Some(&claim), 2_000_000),
            Ok(InstallQgaRefusal::AuthenticatedPredecessorAndAgentUnavailable)
        );
    }
    let mut wrong = claim.clone();
    wrong.vmid = 110;
    assert!(scope.assess(&scope, Some(&wrong), 2_000_000).is_err());
    wrong = claim.clone();
    wrong.node = "node2".into();
    assert!(scope.assess(&scope, Some(&wrong), 2_000_000).is_err());
    wrong = claim.clone();
    wrong.responsive = false;
    assert_eq!(
        scope.assess(&scope, Some(&wrong), 2_000_000),
        Ok(InstallQgaRefusal::HostQgaUnavailable)
    );
    assert!(scope.assess(&scope, Some(&claim), 999_999).is_err());
    let replacement =
        InstallQgaScopeV1::new([1, 2, 3, 4, 6].map(Uuid::from_u128), "node1".into(), 109).unwrap();
    assert!(scope.assess(&replacement, Some(&claim), 2_000_000).is_err());
}
