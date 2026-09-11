use osdeploy_adapter::*;
use uuid::Uuid;
#[test]
fn package_and_predecessor_claims_cannot_authorize_agent_installation() {
    let predecessor = WatchdogBindingV1::new(
        [1, 2, 3, 4, 5, 6].map(Uuid::from_u128),
        ["a".repeat(64), "b".repeat(64)],
        "node1".into(),
        109,
    )
    .unwrap();
    let binding = InstallAgentBindingV1::new(
        predecessor.clone(),
        [7, 8, 9].map(Uuid::from_u128),
        ["c".repeat(64), "d".repeat(64)],
    )
    .unwrap();
    assert_eq!(binding.assess(None), Ok(InstallAgentRefusal::MissingReport));
    let report = InstallAgentReportV1 {
        version: 1,
        binding: binding.clone(),
        claimed_installed: true,
    };
    assert_eq!(
        binding.assess(Some(&report)),
        Ok(InstallAgentRefusal::AuthenticatedInstallationUnavailable)
    );
    for i in 0..3 {
        let mut ids = [7, 8, 9].map(Uuid::from_u128);
        ids[i] = Uuid::from_u128(99);
        let wrong = InstallAgentReportV1 {
            binding: InstallAgentBindingV1::new(
                predecessor.clone(),
                ids,
                ["c".repeat(64), "d".repeat(64)],
            )
            .unwrap(),
            ..report.clone()
        };
        assert!(binding.assess(Some(&wrong)).is_err());
    }
    for i in 0..2 {
        let mut digests = ["c".repeat(64), "d".repeat(64)];
        digests[i] = "e".repeat(64);
        let wrong = InstallAgentReportV1 {
            binding: InstallAgentBindingV1::new(
                predecessor.clone(),
                [7, 8, 9].map(Uuid::from_u128),
                digests,
            )
            .unwrap(),
            ..report.clone()
        };
        assert!(binding.assess(Some(&wrong)).is_err());
    }
    assert!(
        binding
            .assess(Some(&InstallAgentReportV1 {
                version: 2,
                ..report.clone()
            }))
            .is_err()
    );
    assert_eq!(
        binding.assess(Some(&InstallAgentReportV1 {
            claimed_installed: false,
            ..report
        })),
        Ok(InstallAgentRefusal::IncompleteReport)
    );
}
