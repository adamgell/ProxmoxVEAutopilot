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

    const SAFE_JOB: &str = r#"{
        "id":"synthetic-job-0001",
        "job_type":"synthetic_long_sleep",
        "playbook":"_test_long_sleep.yml",
        "cmd":["ansible-playbook","_test_long_sleep.yml","-e","sleep_seconds=5"],
        "args":{"sleep_seconds":5},
        "status":"pending"
    }"#;

    #[test]
    fn sanitized_job_accepts_only_safe_scalar_values_and_loopback_addresses() {
        let loopback = SAFE_JOB.replace(
            "\"sleep_seconds\":5",
            "\"sleep_seconds\":5,\"callback\":\"http://127.0.0.1:5000\"",
        );

        assert!(JobEnvelope::from_json_str(SAFE_JOB).is_ok());
        assert!(JobEnvelope::from_json_str(&loopback).is_ok());
    }

    #[test]
    fn sanitizer_rejects_secret_shaped_keys_and_values_before_construction() {
        let cases = [
            SAFE_JOB.replace("\"sleep_seconds\":5", "\"token\":\"synthetic\""),
            SAFE_JOB.replace("\"sleep_seconds\":5", "\"note\":\"Bearer synthetic-value\""),
            SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                "\"note\":\"looks-like-a-token-value\"",
            ),
            SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                "\"note\":\"-----BEGIN PRIVATE KEY-----\"",
            ),
            SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                "\"note\":\"-----BEGIN OPENSSH PRIVATE KEY-----\"",
            ),
            SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                "\"password\":\"not-a-real-password\"",
            ),
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
            let unsafe_json = SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                &format!("\"{key}\":\"550e8400-e29b-41d4-a716-446655440000\""),
            );
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn sanitizer_rejects_non_loopback_ipv4_and_ipv6_addresses() {
        for value in ["192.168.2.4", "10.0.0.8:5000", "https://[2001:db8::1]/"] {
            let unsafe_json =
                SAFE_JOB.replace("\"sleep_seconds\":5", &format!("\"endpoint\":\"{value}\""));
            assert!(JobEnvelope::from_json_str(&unsafe_json).is_err());
        }
    }

    #[test]
    fn parser_rejects_duplicate_keys_before_a_json_map_can_collapse_them() {
        let duplicate_top_level = SAFE_JOB.replace(
            "\"status\":\"pending\"",
            "\"status\":\"pending\",\"status\":\"running\"",
        );
        let duplicate_nested = SAFE_JOB.replace(
            "\"sleep_seconds\":5",
            "\"sleep_seconds\":5,\"sleep_seconds\":6",
        );

        assert!(JobEnvelope::from_json_str(&duplicate_top_level).is_err());
        assert!(JobEnvelope::from_json_str(&duplicate_nested).is_err());
    }

    #[test]
    fn public_serde_deserialization_cannot_bypass_the_validated_wire_boundary() {
        let duplicate = SAFE_JOB.replace(
            "\"status\":\"pending\"",
            "\"status\":\"pending\",\"status\":\"running\"",
        );

        assert!(serde_json::from_str::<JobEnvelope>(SAFE_JOB).is_ok());
        assert!(serde_json::from_str::<JobEnvelope>(&duplicate).is_err());
    }

    #[test]
    fn normalization_emits_the_closed_synthetic_plan_and_canonical_fingerprint() {
        let job = JobEnvelope::from_json_str(SAFE_JOB).unwrap();

        let plan = normalize_job(&job).unwrap();

        assert_eq!(plan.job_id(), "synthetic-job-0001");
        assert_eq!(plan.operation_kind(), OperationKind::SyntheticLongSleep);
        assert_eq!(plan.contract_version(), 1);
        assert_eq!(plan.adapter_identity(), "ansible:_test_long_sleep.yml@1");
        assert_eq!(plan.parameter_i64("sleep_seconds"), Some(5));
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
            "e7cfdb9b71c50b5dc87680c88e9412b3d00cabe94ea89ffa72f6c7c49acd1fee"
        );
    }

    #[test]
    fn normalization_fails_closed_for_unknown_execution_contract_fields() {
        let cases = [
            SAFE_JOB.replace("synthetic_long_sleep", "provision_clone"),
            SAFE_JOB.replacen("ansible-playbook", "sh", 1),
            SAFE_JOB.replace("_test_long_sleep.yml", "provision_clone.yml"),
            SAFE_JOB.replace(
                "\"sleep_seconds\":5",
                "\"sleep_seconds\":5,\"inventory\":\"synthetic\"",
            ),
            SAFE_JOB.replace("\"status\":\"pending\"", "\"status\":\"running\""),
        ];

        for json in cases {
            let job = JobEnvelope::from_json_str(&json).unwrap();
            assert!(normalize_job(&job).is_err());
        }
    }

    #[test]
    fn normalization_rejects_traversal_mismatched_arguments_and_unsafe_types() {
        let cases = [
            SAFE_JOB.replace("_test_long_sleep.yml", "../_test_long_sleep.yml"),
            SAFE_JOB.replacen("sleep_seconds=5", "sleep_seconds=6", 1),
            SAFE_JOB.replace("\"sleep_seconds\":5", "\"sleep_seconds\":5.5"),
            SAFE_JOB.replace("\"sleep_seconds\":5", "\"sleep_seconds\":null"),
            SAFE_JOB.replace("\"sleep_seconds\":5", "\"sleep_seconds\":21"),
        ];

        for json in cases {
            let job = JobEnvelope::from_json_str(&json).unwrap();
            assert!(normalize_job(&job).is_err());
        }
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
