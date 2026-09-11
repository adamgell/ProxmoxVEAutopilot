use osdeploy_adapter::*;
use uuid::Uuid;
#[test]
fn reports_cannot_forge_host_authentication_or_replace_receipt_binding() {
    let binding = VerifyQgaBindingV1::new(
        [1, 2, 3, 4, 5].map(Uuid::from_u128),
        "a".repeat(64),
        "node1".into(),
        109,
    )
    .unwrap();
    assert_eq!(binding.assess(None), Ok(VerifyQgaRefusal::MissingReport));
    let report = VerifyQgaReportV1 {
        version: 1,
        binding: binding.clone(),
        reported_responsive: true,
    };
    assert_eq!(
        binding.assess(Some(&report)),
        Ok(VerifyQgaRefusal::AuthenticatedHostEvidenceUnavailable)
    );
    let mut wrong = report.clone();
    wrong.version = 2;
    assert!(binding.assess(Some(&wrong)).is_err());
    wrong = report.clone();
    wrong.binding = VerifyQgaBindingV1::new(
        [1, 2, 3, 4, 6].map(Uuid::from_u128),
        "a".repeat(64),
        "node1".into(),
        109,
    )
    .unwrap();
    assert!(binding.assess(Some(&wrong)).is_err());
    wrong = report.clone();
    wrong.binding = VerifyQgaBindingV1::new(
        [1, 2, 3, 4, 5].map(Uuid::from_u128),
        "b".repeat(64),
        "node1".into(),
        109,
    )
    .unwrap();
    assert!(binding.assess(Some(&wrong)).is_err());
    wrong = report;
    wrong.reported_responsive = false;
    assert_eq!(
        binding.assess(Some(&wrong)),
        Ok(VerifyQgaRefusal::NotResponsive)
    );
    assert!(
        VerifyQgaBindingV1::new(
            [1, 2, 3, 4, 5].map(Uuid::from_u128),
            "opaque".into(),
            "node1".into(),
            109
        )
        .is_err()
    );
}
