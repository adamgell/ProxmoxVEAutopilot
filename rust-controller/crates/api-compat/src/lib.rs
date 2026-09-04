mod job;
mod plan;

pub use job::{JobEnvelope, JobValidationError};
pub use plan::{
    NormalizationError, NormalizedPlan, PlanFingerprint, SanitizedValue, normalize_job,
};

#[cfg(test)]
mod tests {
    use std::{collections::BTreeSet, fs, path::PathBuf};

    use controller_domain::OperationKind;
    use sha2::{Digest, Sha256};

    use super::{JobEnvelope, normalize_job};

    const BASELINE_JOB: &str = r#"{
        "id":"synthetic-job-0001",
        "job_type":"test_long_sleep",
        "playbook":"/app/playbooks/_test_long_sleep.yml",
        "cmd":["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=5"],
        "args":{"duration":"5"},
        "status":"pending"
    }"#;

    #[test]
    fn sanitized_job_accepts_only_safe_scalar_values_and_loopback_addresses() {
        let loopback = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            "\"duration\":\"5\",\"callback\":\"http://127.0.0.1:5000\"",
        );

        assert!(JobEnvelope::from_json_str(BASELINE_JOB).is_ok());
        assert!(JobEnvelope::from_json_str(&loopback).is_ok());
    }

    #[test]
    fn sanitizer_rejects_secret_shaped_keys_and_values_before_construction() {
        let cases = [
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"token\":\"synthetic\""),
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"note\":\"Bearer synthetic-value\""),
            BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                "\"note\":\"looks-like-a-token-value\"",
            ),
            BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                "\"note\":\"-----BEGIN PRIVATE KEY-----\"",
            ),
            BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                "\"note\":\"-----BEGIN OPENSSH PRIVATE KEY-----\"",
            ),
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"password\":\"not-a-real-password\""),
        ];

        for unsafe_json in cases {
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn sanitizer_rejects_tenant_and_application_uuid_fields() {
        for key in [
            "tenant_id",
            "tenant_uuid",
            "application_id",
            "application_uuid",
            "client_id",
            "app_id",
        ] {
            let unsafe_json = BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                &format!("\"{key}\":\"550e8400-e29b-41d4-a716-446655440000\""),
            );
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn sanitizer_rejects_non_loopback_ipv4_and_ipv6_addresses() {
        for value in ["192.168.2.4", "10.0.0.8:5000", "https://[2001:db8::1]/"] {
            let unsafe_json =
                BASELINE_JOB.replace("\"duration\":\"5\"", &format!("\"endpoint\":\"{value}\""));
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn parser_rejects_duplicate_keys_before_a_json_map_can_collapse_them() {
        let duplicate_top_level = BASELINE_JOB.replace(
            "\"status\":\"pending\"",
            "\"status\":\"pending\",\"status\":\"running\"",
        );
        let duplicate_nested = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            "\"duration\":\"5\",\"duration\":\"6\"",
        );

        assert!(JobEnvelope::from_json_str(&duplicate_top_level).is_err());
        assert!(JobEnvelope::from_json_str(&duplicate_nested).is_err());
    }

    #[test]
    fn raw_bytes_use_the_same_duplicate_aware_boundary() {
        let duplicate = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            "\"duration\":\"5\",\"duration\":\"6\"",
        );

        assert!(JobEnvelope::from_json_bytes(BASELINE_JOB.as_bytes()).is_ok());
        assert!(JobEnvelope::from_json_bytes(duplicate.as_bytes()).is_err());
    }

    #[test]
    fn normalization_emits_the_closed_synthetic_plan_and_canonical_fingerprint() {
        let job = JobEnvelope::from_json_str(BASELINE_JOB).unwrap();

        let plan = normalize_job(&job).unwrap();

        assert_eq!(plan.job_id(), "synthetic-job-0001");
        assert_eq!(plan.operation_kind(), OperationKind::SyntheticLongSleep);
        assert_eq!(plan.contract_version(), 1);
        assert_eq!(
            plan.adapter_identity(),
            "ansible:/app/playbooks/_test_long_sleep.yml@1"
        );
        assert_eq!(plan.parameter_i64("duration"), Some(5));
        assert_eq!(
            plan.required_capabilities(),
            &BTreeSet::from(["ansible_local".to_owned()])
        );
        assert_eq!(
            plan.expected_events(),
            ["adapter_started", "adapter_completed"]
        );
        assert_eq!(plan.postconditions(), ["synthetic_sleep_completed"]);
        assert_eq!(
            plan.fingerprint().unwrap().as_hex(),
            "31476a1bade27e2f04b3cc1d0495cffb8ac7cf5dffb07728ed9756f6218672d2"
        );
    }

    #[test]
    fn normalization_fails_closed_for_unknown_execution_contract_fields() {
        let cases = [
            BASELINE_JOB.replace("test_long_sleep", "provision_clone"),
            BASELINE_JOB.replacen("ansible-playbook", "sh", 1),
            BASELINE_JOB.replace("_test_long_sleep.yml", "provision_clone.yml"),
            BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                "\"duration\":\"5\",\"inventory\":\"synthetic\"",
            ),
            BASELINE_JOB.replace("\"status\":\"pending\"", "\"status\":\"running\""),
        ];

        for json in cases {
            let job = JobEnvelope::from_json_str(&json).unwrap();
            assert!(normalize_job(&job).is_err());
        }
    }

    #[test]
    fn normalization_rejects_traversal_mismatched_arguments_and_unsafe_types() {
        let cases = [
            BASELINE_JOB.replace("_test_long_sleep.yml", "../_test_long_sleep.yml"),
            BASELINE_JOB.replacen("duration=5", "duration=6", 1),
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"duration\":5.5"),
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"duration\":null"),
            BASELINE_JOB.replace("\"duration\":\"5\"", "\"duration\":\"21\""),
        ];

        for json in cases {
            let job = JobEnvelope::from_json_str(&json).unwrap();
            assert!(normalize_job(&job).is_err());
        }
    }

    #[test]
    fn baseline_python_shape_maps_to_the_rust_synthetic_kind() {
        let job = JobEnvelope::from_json_str(BASELINE_JOB).unwrap();
        let plan = normalize_job(&job).unwrap();

        assert_eq!(plan.operation_kind(), OperationKind::SyntheticLongSleep);
        assert_eq!(plan.parameter_i64("duration"), Some(5));
        assert!(
            JobEnvelope::from_json_str(
                &BASELINE_JOB.replace("test_long_sleep", "synthetic_long_sleep")
            )
            .is_ok()
        );
        let old_shape = BASELINE_JOB.replace("test_long_sleep", "synthetic_long_sleep");
        assert!(normalize_job(&JobEnvelope::from_json_str(&old_shape).unwrap()).is_err());
    }

    #[test]
    fn sanitizer_rejects_encoded_confusable_nested_and_integer_ip_bypasses() {
        let cases = [
            "tok\\u200ben",
            "tоken",
            "%42%65%61%72%65%72%20value",
            "QmVhcmVyIHNlbnNpdGl2ZS12YWx1ZQ==",
            "QmVhcmVyIHNlbnNpdGl2ZS12YWx1ZQ",
            "192%2E168%2E2%2E4",
            "3232236036",
            "0xC0A80204",
            "0XC0A80204",
            "::ffff:192.168.2.4",
        ];
        for value in cases {
            let unsafe_json = BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                &format!("\"duration\":\"5\",\"note\":\"{value}\""),
            );
            assert!(
                JobEnvelope::from_json_str(&unsafe_json).is_err(),
                "accepted {value}"
            );
        }

        let nested = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            "\"duration\":\"5\",\"nested\":[{\"endpoint\":\"192.168.2.4\"}]",
        );
        assert!(JobEnvelope::from_json_str(&nested).is_err());

        for key in [
            "service_principal_object_id",
            "azure_application_object_id",
            "directory_id",
        ] {
            let unsafe_json = BASELINE_JOB.replace(
                "\"duration\":\"5\"",
                &format!("\"duration\":\"5\",\"{key}\":\"synthetic\""),
            );
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn sanitizer_bounds_string_length_and_container_depth() {
        let oversized = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            &format!("\"duration\":\"5\",\"note\":\"{}\"", "a".repeat(1_025)),
        );
        let deeply_nested = BASELINE_JOB.replace(
            "\"duration\":\"5\"",
            "\"duration\":\"5\",\"nested\":[[[[[[[[[\"safe\"]]]]]]]]]",
        );

        assert!(JobEnvelope::from_json_str(&oversized).is_err());
        assert!(JobEnvelope::from_json_str(&deeply_nested).is_err());
    }

    #[test]
    fn synthetic_fixture_manifest_is_complete_and_hash_self_consistent() {
        let fixture_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../fixtures");
        let manifest_bytes = fs::read(fixture_root.join("manifest.json")).unwrap();
        let manifest: serde_json::Value = serde_json::from_slice(&manifest_bytes).unwrap();
        let entries = manifest["fixtures"].as_array().unwrap();
        assert_eq!(entries.len(), 1);
        let entry = &entries[0];
        assert_eq!(entry["name"], "jobs/synthetic-long-sleep.json");
        assert_eq!(entry["baseline_version"], "v2026.09.2");
        assert_eq!(entry["baseline_git_sha"], "c8ab4b2");
        assert_eq!(entry["contract_version"], 1);
        assert_eq!(entry["sanitizer_version"], 1);

        let fixture_bytes = fs::read(fixture_root.join(entry["name"].as_str().unwrap())).unwrap();
        assert_eq!(entry["sha256"], hex::encode(Sha256::digest(&fixture_bytes)));
        let fixture_text = String::from_utf8(fixture_bytes).unwrap();
        let job = JobEnvelope::from_json_str(&fixture_text).unwrap();
        assert!(job.validate_sanitized().is_ok());
        assert!(normalize_job(&job).is_ok());
    }
}
