use osdeploy_adapter::{
    ContractError, DeploymentNames, DiskCapacity, PhasePolicy, PhasePolicyInput,
    normalize_legacy_windows_name,
};
use pve_port::NativeVmName;
use serde_json::json;

#[test]
fn disk_capacity_is_grow_only_and_preserves_exact_template_bytes() {
    for (requested, template, effective, growth) in [
        (80, 85_899_345_920, 85_899_345_920, false),
        (120, 85_899_345_920, 128_849_018_880, true),
        (120, 171_798_691_840, 171_798_691_840, false),
        (120, 128_849_018_880, 128_849_018_880, false),
        (120, 128_849_018_881, 128_849_018_881, false),
        (80, 1, 85_899_345_920, true),
        (80, u64::MAX, u64::MAX, false),
    ] {
        let disk = DiskCapacity::new(requested, template).unwrap();
        assert_eq!(disk.requested_gib(), requested);
        assert_eq!(disk.template_bytes(), template);
        assert_eq!(disk.effective_bytes(), effective);
        assert_eq!(disk.growth_required(), growth);
        assert_eq!(
            serde_json::to_value(&disk).unwrap(),
            json!({
                "requested_gib":requested, "template_bytes":template,
                "effective_bytes":effective, "growth_required":growth,
            })
        );
    }
}

#[test]
fn disk_capacity_rejects_subminimum_requests_and_empty_templates() {
    for requested in [0, 79] {
        assert_eq!(
            DiskCapacity::new(requested, 85_899_345_920),
            Err(ContractError::InvalidRequestedDiskCapacity)
        );
    }
    assert_eq!(
        DiskCapacity::new(80, 0),
        Err(ContractError::InvalidTemplateCapacity)
    );
}

#[test]
fn disk_capacity_checks_binary_gib_overflow_before_comparing_template() {
    for (requested, bytes) in [
        (17_179_869_182, 18_446_744_071_562_067_968),
        (17_179_869_183, 18_446_744_072_635_809_792),
    ] {
        assert_eq!(
            DiskCapacity::new(requested, 1).unwrap().effective_bytes(),
            bytes
        );
    }
    for requested in [17_179_869_184, 17_179_869_185, u64::MAX] {
        assert_eq!(
            DiskCapacity::new(requested, u64::MAX),
            Err(ContractError::DiskCapacityOverflow)
        );
    }
}

#[test]
fn legacy_normalization_uses_literal_ascii_golden_examples() {
    for (input, expected) in [
        ("  --Lab VM_01--  ", "LabVM01"),
        ("  --Láb東京-VM_é01🙂--  ", "Lb-VM01"),
        ("", ""),
        (" _!@#$%^&*()--. ", ""),
        ("AbCdEfGhIjKlMnOpQr", "AbCdEfGhIjKlMnO"),
        ("abcdefghijklmn-op", "abcdefghijklmn-"),
        ("---A--b---", "A--b"),
        ("  123456  ", "123456"),
        ("a\tb\nc", "abc"),
    ] {
        assert_eq!(normalize_legacy_windows_name(input), expected, "{input:?}");
    }
}

fn pve_name() -> NativeVmName {
    NativeVmName::parse("independent-pve-name").unwrap()
}

#[test]
fn names_preserve_requested_and_independent_pve_identity_and_derive_agent_id() {
    let names = DeploymentNames::new("  --Lab VM_01--  ", pve_name()).unwrap();
    assert_eq!(names.requested_name(), "  --Lab VM_01--  ");
    assert_eq!(names.pve_name().as_str(), "independent-pve-name");
    assert_eq!(names.windows_name(), "LabVM01");
    assert_eq!(names.expected_agent_id(), "agent-labvm01");
    assert_eq!(
        serde_json::to_value(names).unwrap(),
        json!({
            "requested_name":"  --Lab VM_01--  ", "pve_name":"independent-pve-name",
            "windows_name":"LabVM01", "expected_agent_id":"agent-labvm01",
        })
    );
}

#[test]
fn names_reject_invalid_original_utf8_byte_length_and_controls() {
    let valid = format!("A{}x", "é".repeat(127));
    assert_eq!(valid.len(), 256);
    assert_eq!(
        DeploymentNames::new(&valid, pve_name())
            .unwrap()
            .requested_name(),
        valid
    );
    assert!(DeploymentNames::new("A", pve_name()).is_ok());
    for input in [
        String::new(),
        "a".repeat(257),
        format!("{valid}x"),
        "a\nb".into(),
        "a\tb".into(),
        "a\0b".into(),
        "a\u{7f}b".into(),
        "a\u{85}b".into(),
    ] {
        assert_eq!(
            DeploymentNames::new(&input, pve_name()),
            Err(ContractError::InvalidRequestedName)
        );
    }
}

#[test]
fn native_names_reject_unsupported_normalized_outputs() {
    for input in [
        "   ",
        "東京🙂",
        "--__!",
        "123456",
        "--12_34--",
        "abcdefghijklmn-op",
        "123456789012345A",
    ] {
        assert_eq!(
            DeploymentNames::new(input, pve_name()),
            Err(ContractError::InvalidWindowsName),
            "{input:?}"
        );
    }
}

#[test]
fn equivalent_normalized_names_share_agent_id_without_claiming_uniqueness() {
    let first = DeploymentNames::new("Lab VM_01", pve_name()).unwrap();
    let second =
        DeploymentNames::new("lABvm01", NativeVmName::parse("another-pve").unwrap()).unwrap();
    assert_ne!(first.windows_name(), second.windows_name());
    assert_eq!(first.expected_agent_id(), "agent-labvm01");
    assert_eq!(first.expected_agent_id(), second.expected_agent_id());
    let truncation = DeploymentNames::new("AbCdEfGhIjKlMnOpQr", pve_name()).unwrap();
    assert_eq!(truncation.windows_name(), "AbCdEfGhIjKlMnO");
    assert_eq!(truncation.expected_agent_id(), "agent-abcdefghijklmno");
}

fn synthetic_policy() -> PhasePolicyInput {
    PhasePolicyInput {
        registration_seconds: 1,
        pe_seconds: 2,
        shutdown_grace_seconds: 3,
        full_os_seconds: 4,
        mutation_seconds: 300,
        evidence_freshness_seconds: 30,
        allow_force_stop: false,
    }
}

#[test]
fn policy_retains_short_phase_budgets_independently_of_freshness() {
    let policy = PhasePolicy::new(synthetic_policy()).unwrap();
    assert_eq!(policy.registration_seconds(), 1);
    assert_eq!(policy.pe_seconds(), 2);
    assert_eq!(policy.shutdown_grace_seconds(), 3);
    assert_eq!(policy.full_os_seconds(), 4);
    assert_eq!(policy.mutation_seconds(), 300);
    assert_eq!(policy.evidence_freshness_seconds(), 30);
    assert!(!policy.allow_force_stop());
    assert_eq!(
        serde_json::to_value(policy).unwrap(),
        json!({
            "registration_seconds":1, "pe_seconds":2, "shutdown_grace_seconds":3,
            "full_os_seconds":4, "mutation_seconds":300, "evidence_freshness_seconds":30,
            "allow_force_stop":false,
        })
    );
}

#[test]
fn production_defaults_pin_all_budgets_and_preserve_force_stop_choice() {
    for allow_force_stop in [false, true] {
        let policy = PhasePolicy::production_defaults(allow_force_stop);
        assert_eq!(
            policy,
            PhasePolicy::new(PhasePolicyInput {
                registration_seconds: 2400,
                pe_seconds: 7200,
                shutdown_grace_seconds: 300,
                full_os_seconds: 7200,
                mutation_seconds: 300,
                evidence_freshness_seconds: 30,
                allow_force_stop,
            })
            .unwrap()
        );
        assert_eq!(
            serde_json::to_value(policy).unwrap(),
            json!({
                "registration_seconds":2400, "pe_seconds":7200, "shutdown_grace_seconds":300,
                "full_os_seconds":7200, "mutation_seconds":300, "evidence_freshness_seconds":30,
                "allow_force_stop":allow_force_stop,
            })
        );
        assert_eq!(
            PhasePolicy::new(PhasePolicyInput {
                allow_force_stop,
                ..synthetic_policy()
            })
            .unwrap()
            .allow_force_stop(),
            allow_force_stop
        );
    }
}

#[test]
fn every_phase_duration_independently_enforces_inclusive_bounds() {
    for field in 0..5 {
        for value in [0, 1, 86_400, 86_401, u32::MAX] {
            let mut input = synthetic_policy();
            input.evidence_freshness_seconds = 1;
            match field {
                0 => input.registration_seconds = value,
                1 => input.pe_seconds = value,
                2 => input.shutdown_grace_seconds = value,
                3 => input.full_os_seconds = value,
                4 => input.mutation_seconds = value,
                _ => unreachable!(),
            }
            let result = PhasePolicy::new(input);
            if matches!(value, 1 | 86_400) {
                assert!(result.is_ok(), "field {field}, value {value}: {result:?}");
            } else {
                assert_eq!(
                    result,
                    Err(ContractError::InvalidPhaseDuration),
                    "field {field}, value {value}"
                );
            }
        }
    }
}

#[test]
fn freshness_is_positive_at_most_300_and_no_larger_than_mutation_budget() {
    for value in [0, 1, 300, 301, u32::MAX] {
        let result = PhasePolicy::new(PhasePolicyInput {
            evidence_freshness_seconds: value,
            ..synthetic_policy()
        });
        if matches!(value, 1 | 300) {
            assert!(result.is_ok());
        } else {
            assert_eq!(result, Err(ContractError::InvalidEvidenceFreshness));
        }
    }
    assert!(
        PhasePolicy::new(PhasePolicyInput {
            mutation_seconds: 30,
            ..synthetic_policy()
        })
        .is_ok()
    );
    assert_eq!(
        PhasePolicy::new(PhasePolicyInput {
            mutation_seconds: 29,
            ..synthetic_policy()
        }),
        Err(ContractError::InvalidEvidenceFreshness)
    );
}
