use artifact_index::*;
use sha2::{Digest, Sha256};
#[test]
fn immutable_exports_and_rollback_window_never_authorize_cutover() {
    let bytes: [&[u8]; 5] = [
        b"source",
        b"image",
        b"proof",
        b"rollback-image",
        b"rollback-proof",
    ];
    let hashes = bytes.map(|b| hex::encode(Sha256::digest(b)));
    let manifest = ExportIntegrityV1::new(hashes.clone(), 1_000, 2_000).unwrap();
    assert_eq!(
        manifest.assess(&manifest, bytes, 1_500),
        Ok(ExportGateRefusal::CutoverAndRollbackAuthorityUnavailable)
    );
    assert_eq!(
        manifest.assess(&manifest, bytes, 2_000),
        Ok(ExportGateRefusal::RollbackWindowClosed)
    );
    assert!(manifest.assess(&manifest, bytes, 999).is_err());
    for i in 0..5 {
        let mut corrupted = bytes;
        corrupted[i] = b"modified";
        assert_eq!(
            manifest.assess(&manifest, corrupted, 1_500),
            Err(ExportGateError::DigestMismatch)
        );
    }
    let shifted = ExportIntegrityV1::new(hashes, 1_000, 3_000).unwrap();
    assert_eq!(
        manifest.assess(&shifted, bytes, 1_500),
        Err(ExportGateError::IdentityMismatch)
    );
    assert!(
        ExportIntegrityV1::new(
            [
                "opaque".into(),
                "a".repeat(64),
                "b".repeat(64),
                "c".repeat(64),
                "d".repeat(64)
            ],
            1_000,
            2_000
        )
        .is_err()
    );
}
