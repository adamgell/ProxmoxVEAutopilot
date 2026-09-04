mod canonical;
mod event;

pub use canonical::{canonical_json_bytes, payload_digest};
pub use event::{AppendDecision, EventKind, EventValidationError, JournalEvent, decide_append};

#[cfg(test)]
mod tests {
    use chrono::Utc;
    use controller_domain::{EventId, ExecutionState, OperationId};
    use serde_json::json;

    use super::{
        AppendDecision, EventKind, JournalEvent, canonical_json_bytes, decide_append,
        payload_digest,
    };

    fn valid_event(semantic_key: &str, payload: serde_json::Value) -> JournalEvent {
        let payload_digest = payload_digest(&payload).unwrap();

        JournalEvent::new(
            EventId::new(),
            OperationId::new(),
            None,
            1,
            semantic_key,
            payload_digest,
            EventKind::ExecutionStateChanged(ExecutionState::Running),
            payload,
            Utc::now(),
        )
        .unwrap()
    }

    #[test]
    fn object_key_order_does_not_change_digest() {
        let left = serde_json::json!({"b": 2, "a": 1});
        let right = serde_json::json!({"a": 1, "b": 2});

        assert_eq!(
            payload_digest(&left).unwrap(),
            payload_digest(&right).unwrap()
        );
    }

    #[test]
    fn payload_digest_is_the_lowercase_sha256_of_compact_canonical_json() {
        assert_eq!(
            payload_digest(&json!({"a": 1})).unwrap(),
            "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
        );
    }

    #[test]
    fn arrays_preserve_order() {
        assert_ne!(
            payload_digest(&serde_json::json!([1, 2])).unwrap(),
            payload_digest(&serde_json::json!([2, 1])).unwrap()
        );
    }

    #[test]
    fn canonical_json_sorts_nested_object_keys_without_whitespace() {
        let payload = serde_json::json!({"z": {"b": true, "a": false}, "a": [2, 1]});

        assert_eq!(
            canonical_json_bytes(&payload).unwrap(),
            br#"{"a":[2,1],"z":{"a":false,"b":true}}"#
        );
    }

    #[test]
    fn journal_event_rejects_invalid_append_only_invariants() {
        let payload = json!({"state": "running"});
        let digest = payload_digest(&payload).unwrap();

        assert!(
            JournalEvent::new(
                EventId::new(),
                OperationId::new(),
                None,
                1,
                " ",
                digest.clone(),
                EventKind::ExecutionStateChanged(ExecutionState::Running),
                payload.clone(),
                Utc::now(),
            )
            .is_err()
        );
        assert!(
            JournalEvent::new(
                EventId::new(),
                OperationId::new(),
                None,
                0,
                "state:running",
                digest.clone(),
                EventKind::ExecutionStateChanged(ExecutionState::Running),
                payload.clone(),
                Utc::now(),
            )
            .is_err()
        );
        assert!(
            JournalEvent::new(
                EventId::new(),
                OperationId::new(),
                None,
                1,
                "state:running",
                "not-the-canonical-digest",
                EventKind::ExecutionStateChanged(ExecutionState::Running),
                payload,
                Utc::now(),
            )
            .is_err()
        );
    }

    #[test]
    fn journal_event_deserialization_cannot_bypass_append_only_invariants() {
        let event = valid_event("state:running", json!({"state": "running"}));
        let encoded = serde_json::to_value(event).unwrap();

        for (field, value) in [
            ("semantic_key", json!(" ")),
            ("aggregate_revision", json!(0)),
            ("payload_digest", json!("not-the-canonical-digest")),
        ] {
            let mut invalid = encoded.clone();
            invalid[field] = value;

            assert!(serde_json::from_value::<JournalEvent>(invalid).is_err());
        }
    }

    #[test]
    fn matching_duplicate_is_already_present_but_different_digest_conflicts() {
        let existing = valid_event("callback:ready", json!({"ready": true}));
        let duplicate = JournalEvent::new(
            EventId::new(),
            existing.operation_id(),
            None,
            2,
            "callback:ready",
            existing.payload_digest(),
            EventKind::EvidenceRecorded,
            existing.payload().clone(),
            Utc::now(),
        )
        .unwrap();
        let conflicting_payload = json!({"ready": false});
        let conflicting = JournalEvent::new(
            EventId::new(),
            existing.operation_id(),
            None,
            2,
            "callback:ready",
            payload_digest(&conflicting_payload).unwrap(),
            EventKind::EvidenceRecorded,
            conflicting_payload,
            Utc::now(),
        )
        .unwrap();

        assert_eq!(
            decide_append(Some(&existing), &duplicate),
            AppendDecision::AlreadyPresent(existing.event_id())
        );
        assert_eq!(
            decide_append(Some(&existing), &conflicting),
            AppendDecision::Conflict {
                existing: existing.event_id()
            }
        );
    }

    #[test]
    fn same_semantic_key_in_a_different_operation_is_a_new_append() {
        let existing = valid_event("callback:ready", json!({"ready": true}));
        let incoming = JournalEvent::new(
            EventId::new(),
            OperationId::new(),
            None,
            1,
            "callback:ready",
            existing.payload_digest(),
            EventKind::EvidenceRecorded,
            existing.payload().clone(),
            Utc::now(),
        )
        .unwrap();

        assert_eq!(
            decide_append(Some(&existing), &incoming),
            AppendDecision::Append
        );
    }
}
