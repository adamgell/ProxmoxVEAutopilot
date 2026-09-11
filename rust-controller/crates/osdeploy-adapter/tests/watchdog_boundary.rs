use osdeploy_adapter::*;
use uuid::Uuid;
#[test]
fn watchdog_claims_cannot_replace_authenticated_predecessor_or_agent() {
    let binding = WatchdogBindingV1::new(
        [1, 2, 3, 4, 5, 6].map(Uuid::from_u128),
        ["a".repeat(64), "b".repeat(64)],
        "node1".into(),
        109,
    )
    .unwrap();
    assert_eq!(binding.assess(None), Ok(WatchdogRefusal::MissingReport));
    let report = WatchdogReportV1 {
        version: 1,
        binding: binding.clone(),
        claimed_installed: true,
    };
    for _ in 0..2 {
        assert_eq!(
            binding.assess(Some(&report)),
            Ok(WatchdogRefusal::AuthenticatedPredecessorAndAgentUnavailable)
        );
    }
    let mut wrong = report.clone();
    wrong.version = 2;
    assert!(binding.assess(Some(&wrong)).is_err());
    for i in 0..6 {
        let mut ids = [1, 2, 3, 4, 5, 6].map(Uuid::from_u128);
        ids[i] = Uuid::from_u128(99);
        wrong = report.clone();
        wrong.binding =
            WatchdogBindingV1::new(ids, ["a".repeat(64), "b".repeat(64)], "node1".into(), 109)
                .unwrap();
        assert!(binding.assess(Some(&wrong)).is_err());
    }
    wrong = report.clone();
    wrong.binding = WatchdogBindingV1::new(
        [1, 2, 3, 4, 5, 6].map(Uuid::from_u128),
        ["a".repeat(64), "c".repeat(64)],
        "node1".into(),
        110,
    )
    .unwrap();
    assert!(binding.assess(Some(&wrong)).is_err());
    wrong = report;
    wrong.claimed_installed = false;
    assert_eq!(
        binding.assess(Some(&wrong)),
        Ok(WatchdogRefusal::IncompleteReport)
    );
}
