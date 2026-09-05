use artifact_index::{
    ArtifactError, DeclaredArtifact, DeclaredArtifactInput, compare_supplied_bytes,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use uuid::Uuid;

fn artifact_for(iso: &[u8], wim: &[u8]) -> DeclaredArtifact {
    artifact_with(iso, wim, 0)
}

fn artifact_with(iso: &[u8], wim: &[u8], change: u8) -> DeclaredArtifact {
    let iso_hash = hex::encode(Sha256::digest(iso));
    let wim_hash = hex::encode(Sha256::digest(wim));
    let mut input = DeclaredArtifactInput {
        artifact_id: Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap(),
        architecture: "amd64",
        build_label: "20260905090000",
        iso_sha256: &iso_hash,
        wim_sha256: &wim_hash,
        source_image_index: 4,
        output_image_index: Some(1),
    };
    match change {
        0 => (),
        1 => input.artifact_id = Uuid::from_u128(1),
        2 => input.build_label = "20260905090001",
        3 => input.source_image_index = 5,
        4 => input.output_image_index = None,
        5 => input.output_image_index = Some(2),
        _ => unreachable!(),
    }
    DeclaredArtifact::new(input).unwrap()
}

#[test]
fn matching_bytes_report_only_bound_fingerprint_lengths_and_limited_evidence() {
    let artifact = artifact_for(b"iso", b"wim bytes\0");
    let report = compare_supplied_bytes(&artifact, b"iso", b"wim bytes\0").unwrap();
    assert_eq!(
        report.descriptor_fingerprint(),
        artifact.fingerprint().unwrap()
    );
    assert_eq!(report.iso_size_bytes(), 3);
    assert_eq!(report.wim_size_bytes(), 10);
    assert_eq!(report.clone(), report);
    assert_eq!(
        serde_json::to_value(&report).unwrap(),
        json!({
            "descriptor_fingerprint": artifact.fingerprint().unwrap(),
            "iso_size_bytes": 3,
            "wim_size_bytes": 10,
            "evidence_level": "supplied_bytes_matched",
            "publication_verified": false
        })
    );
}

#[test]
fn empty_iso_fails_even_when_its_declared_digest_matches() {
    assert_eq!(
        compare_supplied_bytes(&artifact_for(b"", b"wim"), b"", b"wim").unwrap_err(),
        ArtifactError::EmptyIso
    );
}

#[test]
fn empty_wim_fails_even_when_its_declared_digest_matches() {
    assert_eq!(
        compare_supplied_bytes(&artifact_for(b"iso", b""), b"iso", b"").unwrap_err(),
        ArtifactError::EmptyWim
    );
}

#[test]
fn iso_mismatch_is_rejected_independently() {
    assert_eq!(
        compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"bad", b"wim").unwrap_err(),
        ArtifactError::IsoMismatch
    );
}

#[test]
fn wim_mismatch_is_rejected_independently() {
    assert_eq!(
        compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"iso", b"bad").unwrap_err(),
        ArtifactError::WimMismatch
    );
}

#[test]
fn swapped_distinct_inputs_are_rejected() {
    assert_eq!(
        compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"wim", b"iso").unwrap_err(),
        ArtifactError::IsoMismatch
    );
}

#[test]
fn matching_same_content_for_another_descriptor_changes_the_report_binding() {
    let baseline = compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"iso", b"wim").unwrap();
    for change in 1..=5 {
        let artifact = artifact_with(b"iso", b"wim", change);
        let report = compare_supplied_bytes(&artifact, b"iso", b"wim").unwrap();
        assert_ne!(
            baseline.descriptor_fingerprint(),
            report.descriptor_fingerprint()
        );
        assert_eq!(
            report.descriptor_fingerprint(),
            artifact.fingerprint().unwrap()
        );
    }
}
