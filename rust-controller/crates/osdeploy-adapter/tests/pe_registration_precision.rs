use osdeploy_adapter::{PeRegistrationAnchorV1, PeRegistrationAnchorV2};
use serde_json::json;
use uuid::Uuid;

#[test]
fn microsecond_anchor_preserves_original_deadline_and_refuses_lossy_v1_conversion() {
    let op = Uuid::from_u128(1);
    let event = Uuid::from_u128(2);
    let precise = PeRegistrationAnchorV2::new(op, event, 1_000_123, 60).unwrap();
    assert_eq!(precise.opened_unix_micros(), 1_000_123);
    assert_eq!(precise.deadline_unix_micros(), 61_000_123);
    assert!(precise.try_into_v1().is_err());
    let wire = serde_json::to_vec(&precise).unwrap();
    assert_eq!(
        PeRegistrationAnchorV2::decode(&wire, &precise).unwrap(),
        precise
    );
    for (field, value) in [
        ("opened_unix_micros", json!(1_000_124)),
        ("deadline_unix_micros", json!(61_000_124)),
        ("version", json!(1)),
        ("dispatch_event", json!(Uuid::from_u128(3))),
    ] {
        let mut wrong = serde_json::to_value(&precise).unwrap();
        wrong[field] = value;
        assert!(
            PeRegistrationAnchorV2::decode(&serde_json::to_vec(&wrong).unwrap(), &precise).is_err()
        );
    }
    let old = PeRegistrationAnchorV1::new(op, event, 1000, 60).unwrap();
    let upgraded = PeRegistrationAnchorV2::from_v1(&old).unwrap();
    assert_eq!(upgraded.deadline_unix_micros(), 61_000_000);
    assert_eq!(upgraded.try_into_v1().unwrap(), old);
    assert!(PeRegistrationAnchorV2::new(op, event, u64::MAX, 1).is_err());
    assert!(PeRegistrationAnchorV2::new(op, event, 1, 0).is_err());
    let large = PeRegistrationAnchorV1::new(op, event, u64::MAX / 1000, 60).unwrap();
    assert!(PeRegistrationAnchorV2::from_v1(&large).is_err());
    let mut malformed = serde_json::to_value(&precise).unwrap();
    malformed["deadline_unix_ms"] = json!(61000);
    assert!(
        PeRegistrationAnchorV2::decode(&serde_json::to_vec(&malformed).unwrap(), &precise).is_err()
    );
    assert!(PeRegistrationAnchorV2::decode(&vec![b' '; 4097], &precise).is_err());
    assert!(PeRegistrationAnchorV2::decode(b"{}", &precise).is_err());
    assert!(PeRegistrationAnchorV2::new(Uuid::nil(), event, 1, 60).is_err());
    assert!(PeRegistrationAnchorV2::new(op, event, 0, 60).is_err());
    let mut corrupted = serde_json::to_value(&old).unwrap();
    corrupted["deadline_unix_ms"] = json!(62000);
    let corrupted: PeRegistrationAnchorV1 = serde_json::from_value(corrupted).unwrap();
    assert!(PeRegistrationAnchorV2::from_v1(&corrupted).is_err());
}
