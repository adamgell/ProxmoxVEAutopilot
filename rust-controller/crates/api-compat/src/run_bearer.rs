//! Verification of the canonical bearer emitted by Python `winpe_token.sign`.
//! This proves possession of a run credential only. It provides no VM identity,
//! session, attempt, scheduler authority, or permission to dispatch StartPe.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;

/// Verified claims cannot be constructed from callback JSON.
///
/// ```compile_fail
/// let _: api_compat::run_bearer::VerifiedRunBearer =
///     serde_json::from_str(r#"{"run_id":"run-fixture","exp":1000}"#).unwrap();
/// ```
///
/// ```compile_fail
/// let _ = api_compat::run_bearer::VerifiedRunBearer {
///     run_id: "run-fixture".into(), expires_at: 1000,
/// };
/// ```
#[derive(Debug, Eq, PartialEq)]
pub struct VerifiedRunBearer {
    run_id: String,
    expires_at: i64,
}

impl VerifiedRunBearer {
    pub fn run_id(&self) -> &str {
        &self.run_id
    }
    pub fn expires_at(&self) -> i64 {
        self.expires_at
    }
}

#[derive(Debug, Eq, PartialEq, thiserror::Error)]
pub enum BearerError {
    #[error("missing bearer")]
    Missing,
    #[error("invalid token")]
    Invalid,
    #[error("token expired")]
    Expired,
    #[error("token/run mismatch")]
    WrongRun,
    #[error("token secret unavailable")]
    SecretUnavailable,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum RunClaim {
    Text(String),
    Integer(i64),
}

#[derive(Deserialize)]
struct Claims {
    run_id: RunClaim,
    exp: i64,
}

/// `secret` and the Unix time must come from trusted server configuration and
/// clock, never callback fields. The clock is explicit for deterministic proof.
/// The verifier accepts canonical Python-issued tokens and deliberately refuses
/// malformed/coerced claims that the permissive Python decoder might accept.
pub fn verify_run_bearer(
    authorization: Option<&str>,
    secret: &[u8],
    expected_run: &str,
    now: i64,
) -> Result<VerifiedRunBearer, BearerError> {
    let token = authorization
        .and_then(|h| h.strip_prefix("Bearer "))
        .ok_or(BearerError::Missing)?
        .trim();
    if secret.is_empty() {
        return Err(BearerError::SecretUnavailable);
    }
    if token.len() > 8192 || now < 0 {
        return Err(BearerError::Invalid);
    }
    let (head, signature) = token.rsplit_once('.').ok_or(BearerError::Invalid)?;
    let signature = URL_SAFE_NO_PAD
        .decode(signature)
        .map_err(|_| BearerError::Invalid)?;
    let mut mac = Hmac::<Sha256>::new_from_slice(secret).map_err(|_| BearerError::Invalid)?;
    mac.update(head.as_bytes());
    // RustCrypto verifies equal-length MACs using constant-time comparison.
    mac.verify_slice(&signature)
        .map_err(|_| BearerError::Invalid)?;
    let raw = URL_SAFE_NO_PAD
        .decode(head)
        .map_err(|_| BearerError::Invalid)?;
    let claims: Claims = serde_json::from_slice(&raw).map_err(|_| BearerError::Invalid)?;
    // Python accepts the token at the exact expiry second.
    if claims.exp < now {
        return Err(BearerError::Expired);
    }
    let run_id = match claims.run_id {
        RunClaim::Text(v) => v,
        RunClaim::Integer(v) => v.to_string(),
    };
    if run_id != expected_run {
        return Err(BearerError::WrongRun);
    }
    Ok(VerifiedRunBearer {
        run_id,
        expires_at: claims.exp,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    // Independently generated with Python stdlib hmac/base64, synthetic secret.
    const TOKEN: &str = "Bearer eyJleHAiOjEwMDAsInJ1bl9pZCI6InJ1bi1maXh0dXJlIn0.TXZ1Og7YaNK55xMZq8Bl1tBozdZC-qau6PYS_hc_Rjk";
    const SECRET: &[u8] = b"synthetic-test-secret";

    #[test]
    fn python_vector_preserves_expiry_boundary_and_run_scope() {
        for now in [999, 1000] {
            let proof = verify_run_bearer(Some(TOKEN), SECRET, "run-fixture", now).unwrap();
            assert_eq!(proof.run_id(), "run-fixture");
            assert_eq!(proof.expires_at(), 1000);
        }
        assert_eq!(
            verify_run_bearer(Some(TOKEN), SECRET, "run-fixture", 1001),
            Err(BearerError::Expired)
        );
        assert_eq!(
            verify_run_bearer(Some(TOKEN), SECRET, "other-run", 999),
            Err(BearerError::WrongRun)
        );
    }

    #[test]
    fn python_integer_run_claim_matches_string_endpoint_identity() {
        let token =
            "Bearer eyJleHAiOjEwMDAsInJ1bl9pZCI6NDJ9.vu1tRnERuHkmqXm0yghUsBIeeT35Ilf0RMf_k8FdUPw";
        assert_eq!(
            verify_run_bearer(Some(token), SECRET, "42", 999)
                .unwrap()
                .run_id(),
            "42"
        );
        assert_eq!(
            verify_run_bearer(Some(token), SECRET, "042", 999),
            Err(BearerError::WrongRun)
        );
    }

    #[test]
    fn valid_signature_cannot_bypass_claim_validation() {
        for raw in [
            r#"{"exp":1000}"#,
            r#"{"run_id":"run-fixture"}"#,
            r#"{"exp":1000,"run_id":true}"#,
            r#"{"exp":"1000","run_id":"run-fixture"}"#,
            r#"{"exp":1000,"run_id":"run-fixture","run_id":"other"}"#,
        ] {
            let head = URL_SAFE_NO_PAD.encode(raw);
            let mut mac = Hmac::<Sha256>::new_from_slice(SECRET).unwrap();
            mac.update(head.as_bytes());
            let token = format!(
                "Bearer {head}.{}",
                URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
            );
            assert_eq!(
                verify_run_bearer(Some(&token), SECRET, "run-fixture", 999),
                Err(BearerError::Invalid)
            );
        }
    }

    #[test]
    fn credentials_and_malformed_input_fail_closed() {
        assert_eq!(
            verify_run_bearer(None, SECRET, "run-fixture", 999),
            Err(BearerError::Missing)
        );
        assert_eq!(
            verify_run_bearer(Some(TOKEN), b"", "run-fixture", 999),
            Err(BearerError::SecretUnavailable)
        );
        assert_eq!(
            verify_run_bearer(Some(TOKEN), b"wrong-secret", "run-fixture", 999),
            Err(BearerError::Invalid)
        );
        for token in [
            "Bearer ".to_owned(),
            TOKEN.replace("TXZ1", "AXZ1"),
            TOKEN.replace("eyJl", "eyJm"),
            format!("{TOKEN}.extra"),
            "Bearer abc.!".into(),
        ] {
            assert_eq!(
                verify_run_bearer(Some(&token), SECRET, "run-fixture", 999),
                Err(BearerError::Invalid)
            );
        }
    }
}
