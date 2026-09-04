mod command;
mod evidence;
mod id;
mod state;
mod transition;

pub use command::{
    CommandEnvelope, CommandValidationError, IdempotencyDecision, OperationKind, PersistedCommand,
    check_idempotency,
};
pub use evidence::{OperationAggregate, ReadinessRecordError};
pub use id::{
    AttemptId, EventId, OperationId, RunId, SemanticOperationKey, ValidationError, WorkflowKind,
};
pub use state::{ExecutionState, ObservationHealth, ReadinessMilestone};
pub use transition::{DomainSignal, Transition, TransitionError, decide_transition};

#[cfg(test)]
mod tests {
    use super::{
        AttemptId, CommandEnvelope, DomainSignal, EventId, ExecutionState, IdempotencyDecision,
        ObservationHealth, OperationAggregate, OperationId, PersistedCommand, ReadinessMilestone,
        RunId, SemanticOperationKey, WorkflowKind, check_idempotency, decide_transition,
    };
    use proptest::prelude::*;

    #[test]
    fn deadline_from_running_is_unknown_not_failed() {
        let transition =
            decide_transition(ExecutionState::Running, DomainSignal::DeadlineElapsed).unwrap();

        assert_eq!(transition.next, ExecutionState::Unknown);
    }

    #[test]
    fn readiness_recording_preserves_execution_state() {
        let mut aggregate = OperationAggregate::running_fixture();

        aggregate
            .record_readiness(ReadinessMilestone::AgentConnected)
            .unwrap();

        assert_eq!(aggregate.execution_state(), ExecutionState::Running);
    }

    #[test]
    fn opaque_ids_are_uuid_v7_values() {
        assert_eq!(RunId::new().as_uuid().get_version_num(), 7);
        assert_eq!(OperationId::new().as_uuid().get_version_num(), 7);
        assert_eq!(AttemptId::new().as_uuid().get_version_num(), 7);
        assert_eq!(EventId::new().as_uuid().get_version_num(), 7);
    }

    #[test]
    fn operation_attempt_and_event_ids_reject_v4_and_nil_json() {
        let v4 = serde_json::json!("550e8400-e29b-41d4-a716-446655440000");
        let nil = serde_json::json!(uuid::Uuid::nil());

        for value in [v4, nil] {
            assert!(serde_json::from_value::<OperationId>(value.clone()).is_err());
            assert!(serde_json::from_value::<AttemptId>(value.clone()).is_err());
            assert!(serde_json::from_value::<EventId>(value).is_err());
        }
    }

    #[test]
    fn uuid_v7_identity_ids_round_trip_through_json() {
        let operation_id = OperationId::new();
        let attempt_id = AttemptId::new();
        let event_id = EventId::new();

        assert_eq!(
            serde_json::from_value::<OperationId>(serde_json::to_value(operation_id).unwrap())
                .unwrap(),
            operation_id
        );
        assert_eq!(
            serde_json::from_value::<AttemptId>(serde_json::to_value(attempt_id).unwrap()).unwrap(),
            attempt_id
        );
        assert_eq!(
            serde_json::from_value::<EventId>(serde_json::to_value(event_id).unwrap()).unwrap(),
            event_id
        );
    }

    #[test]
    fn run_id_deserialization_accepts_an_existing_workflow_uuid() {
        let existing_workflow_id =
            uuid::Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();

        let run_id =
            serde_json::from_value::<RunId>(serde_json::json!(existing_workflow_id)).unwrap();

        assert_eq!(run_id.as_uuid(), existing_workflow_id);
        assert!(serde_json::from_value::<RunId>(serde_json::json!(uuid::Uuid::nil())).is_err());
        assert_eq!(
            serde_json::from_value::<RunId>(serde_json::to_value(RunId::new()).unwrap())
                .unwrap()
                .as_uuid()
                .get_version_num(),
            7
        );
    }

    #[test]
    fn semantic_operation_key_rejects_blank_operation_key_and_zero_contract_version() {
        let run_id = RunId::new();
        assert!(
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, run_id, " ", 1).is_err()
        );
        assert!(
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, run_id, "run", 0).is_err()
        );
    }

    #[test]
    fn semantic_operation_key_deserialization_rejects_blank_key_and_zero_version() {
        let run_id = RunId::new();
        let blank_key = serde_json::json!({
            "workflow_kind": "synthetic_long_sleep",
            "run_id": run_id,
            "operation_key": " ",
            "contract_version": 1,
        });
        let zero_version = serde_json::json!({
            "workflow_kind": "synthetic_long_sleep",
            "run_id": run_id,
            "operation_key": "run",
            "contract_version": 0,
        });

        assert!(serde_json::from_value::<SemanticOperationKey>(blank_key).is_err());
        assert!(serde_json::from_value::<SemanticOperationKey>(zero_version).is_err());
    }

    #[test]
    fn command_envelope_deserialization_rejects_blank_idempotency_key_and_digest() {
        let semantic_key =
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, RunId::new(), "run", 1)
                .unwrap();
        let blank_idempotency_key = serde_json::json!({
            "idempotency_key": " ",
            "semantic_key": semantic_key,
            "payload_digest": "opaque-digest",
        });
        let blank_digest = serde_json::json!({
            "idempotency_key": "capture-1",
            "semantic_key": semantic_key,
            "payload_digest": " ",
        });

        assert!(serde_json::from_value::<CommandEnvelope>(blank_idempotency_key).is_err());
        assert!(serde_json::from_value::<CommandEnvelope>(blank_digest).is_err());
    }

    #[test]
    fn validated_domain_values_round_trip_through_the_existing_serde_shape() {
        let envelope = CommandEnvelope::new(
            "capture-1",
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, RunId::new(), "run", 1)
                .unwrap(),
            "opaque-digest",
        )
        .unwrap();

        let encoded = serde_json::to_value(&envelope).unwrap();
        let decoded = serde_json::from_value::<CommandEnvelope>(encoded).unwrap();

        assert_eq!(decoded, envelope);
        assert_eq!(decoded.idempotency_key(), "capture-1");
        assert_eq!(decoded.semantic_key().operation_key(), "run");
        assert_eq!(decoded.payload_digest(), "opaque-digest");
    }

    #[test]
    fn state_dimensions_serialize_only_as_their_documented_snake_case_values() {
        let execution = [
            (ExecutionState::Pending, "pending"),
            (ExecutionState::Leased, "leased"),
            (ExecutionState::Running, "running"),
            (ExecutionState::Waiting, "waiting"),
            (ExecutionState::Cancelling, "cancelling"),
            (ExecutionState::Satisfied, "satisfied"),
            (ExecutionState::Failed, "failed"),
            (ExecutionState::Blocked, "blocked"),
            (ExecutionState::Unknown, "unknown"),
            (ExecutionState::Conflicted, "conflicted"),
        ];
        let health = [
            (ObservationHealth::Fresh, "fresh"),
            (ObservationHealth::Stale, "stale"),
            (ObservationHealth::Unavailable, "unavailable"),
            (ObservationHealth::Unauthorized, "unauthorized"),
            (ObservationHealth::TimedOut, "timed_out"),
            (ObservationHealth::Contradicted, "contradicted"),
        ];
        let readiness = [
            (ReadinessMilestone::VmCreated, "vm_created"),
            (ReadinessMilestone::PeRegistered, "pe_registered"),
            (ReadinessMilestone::OsInstalled, "os_installed"),
            (ReadinessMilestone::AgentConnected, "agent_connected"),
            (ReadinessMilestone::QgaVerified, "qga_verified"),
            (ReadinessMilestone::VerifiedOobe, "verified_oobe"),
            (ReadinessMilestone::Enrolled, "enrolled"),
            (ReadinessMilestone::EspComplete, "esp_complete"),
            (ReadinessMilestone::UsableEndpoint, "usable_endpoint"),
        ];

        for (value, expected) in execution {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
        for (value, expected) in health {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
        for (value, expected) in readiness {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
    }

    #[test]
    fn idempotency_returns_existing_operation_only_for_matching_digest() {
        let incoming = CommandEnvelope::new(
            "capture-1",
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, RunId::new(), "run", 1)
                .unwrap(),
            "opaque-validated-digest",
        )
        .unwrap();
        let existing =
            PersistedCommand::new(OperationId::new(), "opaque-validated-digest").unwrap();

        assert_eq!(
            check_idempotency(Some(&existing), &incoming),
            IdempotencyDecision::ReturnExisting(existing.operation_id())
        );

        let conflicting = CommandEnvelope::new(
            "capture-1",
            incoming.semantic_key().clone(),
            "a-different-opaque-digest",
        )
        .unwrap();
        assert_eq!(
            check_idempotency(Some(&existing), &conflicting),
            IdempotencyDecision::Conflict {
                existing: existing.operation_id()
            }
        );
    }

    #[test]
    fn terminal_execution_states_cannot_regress() {
        for terminal in [
            ExecutionState::Satisfied,
            ExecutionState::Failed,
            ExecutionState::Blocked,
            ExecutionState::Unknown,
            ExecutionState::Conflicted,
        ] {
            assert!(decide_transition(terminal, DomainSignal::Started).is_err());
        }
    }

    #[test]
    fn cancellation_from_running_becomes_cancelling() {
        let transition =
            decide_transition(ExecutionState::Running, DomainSignal::CancellationRequested)
                .unwrap();

        assert_eq!(transition.next, ExecutionState::Cancelling);
    }

    proptest! {
        #[test]
        fn idempotency_without_an_existing_command_always_accepts(
            digest in "[a-z0-9]{1,64}"
        ) {
            let incoming = CommandEnvelope::new(
                "capture-1",
                SemanticOperationKey::new(
                    WorkflowKind::SyntheticLongSleep,
                    RunId::new(),
                    "run",
                    1,
                ).unwrap(),
                digest,
            ).unwrap();

            prop_assert_eq!(check_idempotency(None, &incoming), IdempotencyDecision::AcceptNew);
        }

        #[test]
        fn idempotency_returns_the_stable_operation_for_equal_nonempty_digests(
            digest in "[a-z0-9]{1,64}"
        ) {
            let incoming = CommandEnvelope::new(
                "capture-1",
                SemanticOperationKey::new(
                    WorkflowKind::SyntheticLongSleep,
                    RunId::new(),
                    "run",
                    1,
                ).unwrap(),
                &digest,
            ).unwrap();
            let existing = PersistedCommand::new(OperationId::new(), digest).unwrap();

            prop_assert_eq!(
                check_idempotency(Some(&existing), &incoming),
                IdempotencyDecision::ReturnExisting(existing.operation_id()),
            );
        }

        #[test]
        fn idempotency_conflicts_for_unequal_nonempty_digests(
            existing_digest in "[a-z0-9]{1,64}",
            incoming_digest in "[a-z0-9]{1,64}"
        ) {
            prop_assume!(existing_digest != incoming_digest);
            let incoming = CommandEnvelope::new(
                "capture-1",
                SemanticOperationKey::new(
                    WorkflowKind::SyntheticLongSleep,
                    RunId::new(),
                    "run",
                    1,
                ).unwrap(),
                incoming_digest,
            ).unwrap();
            let existing = PersistedCommand::new(OperationId::new(), existing_digest).unwrap();

            prop_assert_eq!(
                check_idempotency(Some(&existing), &incoming),
                IdempotencyDecision::Conflict { existing: existing.operation_id() },
            );
        }

        #[test]
        fn deadline_from_running_or_waiting_never_becomes_failed(
            state in prop_oneof![Just(ExecutionState::Running), Just(ExecutionState::Waiting)]
        ) {
            let transition = decide_transition(state, DomainSignal::DeadlineElapsed).unwrap();
            prop_assert_eq!(transition.next, ExecutionState::Unknown);
            prop_assert_ne!(transition.next, ExecutionState::Failed);
        }

        #[test]
        fn readiness_history_never_changes_execution_state(
            state in prop_oneof![
                Just(ExecutionState::Pending), Just(ExecutionState::Leased),
                Just(ExecutionState::Running), Just(ExecutionState::Waiting),
                Just(ExecutionState::Cancelling), Just(ExecutionState::Satisfied),
                Just(ExecutionState::Failed), Just(ExecutionState::Blocked),
                Just(ExecutionState::Unknown), Just(ExecutionState::Conflicted),
            ],
            milestones in proptest::collection::vec(prop_oneof![
                Just(ReadinessMilestone::VmCreated), Just(ReadinessMilestone::PeRegistered),
                Just(ReadinessMilestone::OsInstalled), Just(ReadinessMilestone::AgentConnected),
                Just(ReadinessMilestone::QgaVerified), Just(ReadinessMilestone::VerifiedOobe),
                Just(ReadinessMilestone::Enrolled), Just(ReadinessMilestone::EspComplete),
                Just(ReadinessMilestone::UsableEndpoint),
            ], 0..64)
        ) {
            let mut aggregate = OperationAggregate::new(state);
            for milestone in milestones {
                aggregate.record_readiness(milestone).unwrap();
            }
            prop_assert_eq!(aggregate.execution_state(), state);
        }
    }
}
