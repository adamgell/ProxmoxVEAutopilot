use artifact_index::{ArtifactError, DeclaredArtifact, DeclaredArtifactInput, ImageIndexSource};
use serde_json::json;
use uuid::Uuid;

const ISO_HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const WIM_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn fixture() -> DeclaredArtifactInput<'static> {
    DeclaredArtifactInput {
        artifact_id: Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap(),
        architecture: "amd64",
        build_label: "20260905090000",
        iso_sha256: ISO_HASH,
        wim_sha256: WIM_HASH,
        source_image_index: 4,
        output_image_index: Some(1),
    }
}

#[test]
fn valid_fixture_preserves_declared_identity_and_prefers_output_index() {
    let artifact = DeclaredArtifact::new(fixture()).unwrap();
    assert_eq!(artifact.artifact_id(), fixture().artifact_id);
    assert_eq!(artifact.architecture(), "amd64");
    assert_eq!(artifact.build_label(), "20260905090000");
    assert_eq!(artifact.iso_sha256(), ISO_HASH);
    assert_eq!(artifact.wim_sha256(), WIM_HASH);
    assert_eq!(artifact.source_image_index(), 4);
    assert_eq!(artifact.output_image_index(), Some(1));
    assert_eq!(artifact.apply_image_index(), 1);
    assert_eq!(
        artifact.image_index_source(),
        ImageIndexSource::OutputManifest
    );
    assert_eq!(artifact.clone(), artifact);
}

#[test]
fn missing_output_falls_back_to_source_with_distinct_provenance() {
    let mut input = fixture();
    input.output_image_index = None;
    let fallback = DeclaredArtifact::new(input).unwrap();
    assert_eq!(fallback.output_image_index(), None);
    assert_eq!(fallback.apply_image_index(), 4);
    assert_eq!(
        fallback.image_index_source(),
        ImageIndexSource::SourceFallback
    );
    let mut input = fixture();
    input.output_image_index = Some(4);
    let explicit = DeclaredArtifact::new(input).unwrap();
    assert_eq!(explicit.apply_image_index(), fallback.apply_image_index());
    assert_ne!(
        explicit.fingerprint().unwrap(),
        fallback.fingerprint().unwrap()
    );
    assert_eq!(
        serde_json::to_value(fallback).unwrap()["image_index_source"],
        "source_fallback"
    );
}

#[test]
fn descriptor_serialization_pins_the_complete_declared_only_contract() {
    let artifact = DeclaredArtifact::new(fixture()).unwrap();
    let expected = json!({
        "contract_version": 1,
        "artifact_id": "11111111-1111-4111-8111-111111111111",
        "architecture": "amd64",
        "build_label": "20260905090000",
        "iso_sha256": ISO_HASH,
        "wim_sha256": WIM_HASH,
        "source_image_index": 4,
        "output_image_index": 1,
        "apply_image_index": 1,
        "image_index_source": "output_manifest",
        "evidence_level": "declared_only"
    });
    assert_eq!(serde_json::to_value(&artifact).unwrap(), expected);
    assert_eq!(
        artifact.fingerprint().unwrap(),
        event_journal::payload_digest(&expected).unwrap()
    );
}

#[test]
fn nil_uuid_is_rejected_but_non_version_seven_opaque_ids_are_accepted() {
    let mut input = fixture();
    input.artifact_id = Uuid::nil();
    assert_eq!(
        DeclaredArtifact::new(input).unwrap_err(),
        ArtifactError::InvalidArtifactId
    );
    let mut input = fixture();
    input.artifact_id = Uuid::from_u128(u128::MAX);
    assert!(DeclaredArtifact::new(input).is_ok());
}

#[test]
fn architecture_accepts_only_literal_amd64() {
    for value in [
        "",
        "AMD64",
        "x86_64",
        "arm64",
        " amd64",
        "amd64 ",
        "amd64\n",
        "amd64\0",
        "ａmd64",
        "https://amd64",
        "/amd64",
    ] {
        let mut input = fixture();
        input.architecture = value;
        assert_eq!(
            DeclaredArtifact::new(input).unwrap_err(),
            ArtifactError::InvalidArchitecture
        );
    }
}

#[test]
fn build_label_accepts_only_fourteen_ascii_digits_without_calendar_authority() {
    for value in [
        "",
        " ",
        "2026090509000",
        "202609050900000",
        "2026090509000a",
        "2026090509000\n",
        "2026090509000\0",
        "２０２６０９０５０９００００",
        "2026090509000١",
        " 0260905090000",
        "2026090509000 ",
        "https://x.test",
        "/2026090509000",
    ] {
        let mut input = fixture();
        input.build_label = value;
        assert_eq!(
            DeclaredArtifact::new(input).unwrap_err(),
            ArtifactError::InvalidBuildLabel
        );
    }
    for value in ["00000000000000", "99999999999999"] {
        let mut input = fixture();
        input.build_label = value;
        assert_eq!(DeclaredArtifact::new(input).unwrap().build_label(), value);
    }
}

#[test]
fn hashes_reject_non_ascii_nonhex_and_wrong_lengths_independently() {
    let invalid = [
        String::new(),
        " ".into(),
        "a".repeat(63),
        "a".repeat(65),
        "g".repeat(64),
        "é".repeat(32),
        format!("{}\n", "a".repeat(63)),
        format!("{}\0", "a".repeat(63)),
        format!("{} ", "a".repeat(63)),
        format!("0x{}", "a".repeat(62)),
        format!("https://{}", "a".repeat(56)),
        format!("/{}", "a".repeat(63)),
    ];
    for value in &invalid {
        let mut input = fixture();
        input.iso_sha256 = value;
        assert_eq!(
            DeclaredArtifact::new(input).unwrap_err(),
            ArtifactError::InvalidIsoSha256
        );
        let mut input = fixture();
        input.wim_sha256 = value;
        assert_eq!(
            DeclaredArtifact::new(input).unwrap_err(),
            ArtifactError::InvalidWimSha256
        );
    }
}

#[test]
fn hashes_normalize_all_hex_letters_and_digits_without_changing_fingerprint() {
    let upper = "0123456789ABCDEF".repeat(4);
    let lower = "0123456789abcdef".repeat(4);
    let mut input = fixture();
    input.iso_sha256 = &upper;
    input.wim_sha256 = &upper;
    let normalized = DeclaredArtifact::new(input).unwrap();
    assert_eq!(normalized.iso_sha256(), lower);
    assert_eq!(normalized.wim_sha256(), lower);
    let mut input = fixture();
    input.iso_sha256 = &lower;
    input.wim_sha256 = &lower;
    let lowercase = DeclaredArtifact::new(input).unwrap();
    assert_eq!(normalized, lowercase);
    assert_eq!(
        normalized.fingerprint().unwrap(),
        lowercase.fingerprint().unwrap()
    );
}

#[test]
fn indices_reject_zero_and_preserve_positive_u32_bounds() {
    let mut input = fixture();
    input.source_image_index = 0;
    assert_eq!(
        DeclaredArtifact::new(input).unwrap_err(),
        ArtifactError::InvalidSourceImageIndex
    );
    let mut input = fixture();
    input.output_image_index = Some(0);
    assert_eq!(
        DeclaredArtifact::new(input).unwrap_err(),
        ArtifactError::InvalidOutputImageIndex
    );
    for index in [1, u32::MAX] {
        for output in [None, Some(index)] {
            let mut input = fixture();
            input.source_image_index = index;
            input.output_image_index = output;
            let artifact = DeclaredArtifact::new(input).unwrap();
            assert_eq!(artifact.source_image_index(), index);
            assert_eq!(artifact.output_image_index(), output);
            assert_eq!(artifact.apply_image_index(), index);
            assert_eq!(
                serde_json::to_value(&artifact).unwrap()["apply_image_index"],
                index
            );
            assert!(artifact.fingerprint().is_ok());
        }
    }
}

#[test]
fn every_variable_descriptor_field_is_bound_into_the_fingerprint() {
    let baseline = DeclaredArtifact::new(fixture())
        .unwrap()
        .fingerprint()
        .unwrap();
    assert_eq!(
        baseline,
        DeclaredArtifact::new(fixture())
            .unwrap()
            .fingerprint()
            .unwrap()
    );
    for change in 0..7 {
        let mut input = fixture();
        match change {
            0 => input.artifact_id = Uuid::from_u128(1),
            1 => input.build_label = "20260905090001",
            2 => input.iso_sha256 = WIM_HASH,
            3 => input.wim_sha256 = ISO_HASH,
            4 => input.source_image_index = 5,
            5 => input.output_image_index = None,
            6 => input.output_image_index = Some(2),
            _ => unreachable!(),
        }
        assert_ne!(
            baseline,
            DeclaredArtifact::new(input).unwrap().fingerprint().unwrap()
        );
    }
}

#[test]
fn all_error_variants_have_fixed_payload_free_messages() {
    for (error, message, debug) in [
        (
            ArtifactError::InvalidArtifactId,
            "artifact ID must be non-nil",
            "InvalidArtifactId",
        ),
        (
            ArtifactError::InvalidArchitecture,
            "architecture must be amd64",
            "InvalidArchitecture",
        ),
        (
            ArtifactError::InvalidBuildLabel,
            "build label must contain exactly 14 ASCII digits",
            "InvalidBuildLabel",
        ),
        (
            ArtifactError::InvalidIsoSha256,
            "ISO SHA-256 must contain exactly 64 ASCII hexadecimal characters",
            "InvalidIsoSha256",
        ),
        (
            ArtifactError::InvalidWimSha256,
            "WIM SHA-256 must contain exactly 64 ASCII hexadecimal characters",
            "InvalidWimSha256",
        ),
        (
            ArtifactError::InvalidSourceImageIndex,
            "source image index must be positive",
            "InvalidSourceImageIndex",
        ),
        (
            ArtifactError::InvalidOutputImageIndex,
            "output image index must be positive when supplied",
            "InvalidOutputImageIndex",
        ),
        (
            ArtifactError::FingerprintFailed,
            "descriptor fingerprint could not be computed",
            "FingerprintFailed",
        ),
        (
            ArtifactError::EmptyIso,
            "supplied ISO bytes must be nonempty",
            "EmptyIso",
        ),
        (
            ArtifactError::EmptyWim,
            "supplied WIM bytes must be nonempty",
            "EmptyWim",
        ),
        (
            ArtifactError::IsoMismatch,
            "supplied ISO bytes do not match the declared digest",
            "IsoMismatch",
        ),
        (
            ArtifactError::WimMismatch,
            "supplied WIM bytes do not match the declared digest",
            "WimMismatch",
        ),
    ] {
        assert_eq!(error.to_string(), message);
        assert_eq!(format!("{error:?}"), debug);
        assert!(std::error::Error::source(&error).is_none());
    }
}
