//! Verification of the canonical bearer emitted by Python `winpe_token.sign`.
//! This proves possession of a run credential only. It provides no VM identity,
//! session, attempt, scheduler authority, or permission to dispatch StartPe.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;

/// Preserve the legacy JSON claim type. Numeric identities must not be silently
/// converted to text because that changes the deterministic credential bytes.
pub enum RunBearerIdentity<'a> {
    Text(&'a str),
    Integer(i64),
}

/// A server-issued credential. Explicit exposure is required for delivery;
/// debug output never contains the bearer. This grants no session authority.
///
/// ```compile_fail
/// let _: api_compat::run_bearer::IssuedRunBearer = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail
/// let _ = api_compat::run_bearer::IssuedRunBearer { token: String::new() };
/// ```
pub struct IssuedRunBearer {
    token: String,
}

impl std::fmt::Debug for IssuedRunBearer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("IssuedRunBearer([REDACTED])")
    }
}

impl IssuedRunBearer {
    pub fn expose_for_delivery(&self) -> &str {
        &self.token
    }
}

/// Issue the canonical Python bearer using server-selected identity and absolute
/// Unix expiry. Reissuing identical inputs returns identical bytes; it does not
/// create or renew a session or its registration deadline. Text identities are
/// bounded and control-free; integer identities must be positive.
pub fn issue_run_bearer(
    identity: RunBearerIdentity<'_>,
    expires_at: i64,
    secret: &[u8],
) -> Result<IssuedRunBearer, BearerError> {
    if secret.is_empty() {
        return Err(BearerError::SecretUnavailable);
    }
    if expires_at <= 0 {
        return Err(BearerError::Invalid);
    }
    let run = match identity {
        RunBearerIdentity::Integer(n) if n > 0 => n.to_string(),
        RunBearerIdentity::Text(s)
            if !s.trim().is_empty() && s.len() <= 256 && !s.chars().any(char::is_control) =>
        {
            let json = serde_json::to_string(s).map_err(|_| BearerError::Invalid)?;
            // Python json.dumps defaults to ensure_ascii=True, including UTF-16
            // surrogate pairs for supplementary code points.
            let mut ascii = String::new();
            for c in json.chars() {
                if c.is_ascii() {
                    ascii.push(c);
                } else {
                    use std::fmt::Write;
                    for unit in c.encode_utf16(&mut [0; 2]) {
                        write!(ascii, "\\u{unit:04x}").map_err(|_| BearerError::Invalid)?;
                    }
                }
            }
            ascii
        }
        _ => return Err(BearerError::Invalid),
    };
    let head = URL_SAFE_NO_PAD.encode(format!("{{\"exp\":{expires_at},\"run_id\":{run}}}"));
    let mut mac = Hmac::<Sha256>::new_from_slice(secret).map_err(|_| BearerError::Invalid)?;
    mac.update(head.as_bytes());
    Ok(IssuedRunBearer {
        token: format!(
            "{head}.{}",
            URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
        ),
    })
}

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
    fn issuer_matches_python_vectors_and_deterministic_reissue() {
        for (identity, expected, run) in [
            (
                RunBearerIdentity::Text("run-fixture"),
                TOKEN.strip_prefix("Bearer ").unwrap(),
                "run-fixture",
            ),
            (
                RunBearerIdentity::Integer(42),
                "eyJleHAiOjEwMDAsInJ1bl9pZCI6NDJ9.vu1tRnERuHkmqXm0yghUsBIeeT35Ilf0RMf_k8FdUPw",
                "42",
            ),
        ] {
            let issued = issue_run_bearer(identity, 1000, SECRET).unwrap();
            assert_eq!(issued.expose_for_delivery(), expected);
            let authorization = format!("Bearer {}", issued.expose_for_delivery());
            assert_eq!(
                verify_run_bearer(Some(&authorization), SECRET, run, 1000)
                    .unwrap()
                    .expires_at(),
                1000
            );
            assert_eq!(format!("{issued:?}"), "IssuedRunBearer([REDACTED])");
        }
        let issue =
            || issue_run_bearer(RunBearerIdentity::Text("run-fixture"), 1000, SECRET).unwrap();
        assert_eq!(issue().expose_for_delivery(), issue().expose_for_delivery());
        assert_ne!(
            issue().expose_for_delivery(),
            issue_run_bearer(RunBearerIdentity::Text("run-fixture"), 1001, SECRET)
                .unwrap()
                .expose_for_delivery()
        );
    }

    #[test]
    fn issuer_preserves_python_ascii_json_escaping() {
        let issued = issue_run_bearer(RunBearerIdentity::Text("é🚀\"\\"), 1000, SECRET).unwrap();
        let head = issued.expose_for_delivery().split('.').next().unwrap();
        assert_eq!(
            String::from_utf8(URL_SAFE_NO_PAD.decode(head).unwrap()).unwrap(),
            r#"{"exp":1000,"run_id":"\u00e9\ud83d\ude80\"\\"}"#
        );
        let header = format!("Bearer {}", issued.expose_for_delivery());
        assert_eq!(
            verify_run_bearer(Some(&header), SECRET, "é🚀\"\\", 999)
                .unwrap()
                .run_id(),
            "é🚀\"\\"
        );
    }

    #[test]
    fn issuer_refuses_invalid_server_inputs() {
        for run in ["", " ", "bad\nrun"] {
            assert_eq!(
                issue_run_bearer(RunBearerIdentity::Text(run), 1000, SECRET).unwrap_err(),
                BearerError::Invalid
            );
        }
        for n in [0, -1] {
            assert_eq!(
                issue_run_bearer(RunBearerIdentity::Integer(n), 1000, SECRET).unwrap_err(),
                BearerError::Invalid
            );
            assert_eq!(
                issue_run_bearer(RunBearerIdentity::Integer(42), n, SECRET).unwrap_err(),
                BearerError::Invalid
            );
        }
        assert_eq!(
            issue_run_bearer(RunBearerIdentity::Text(&"a".repeat(257)), 1000, SECRET).unwrap_err(),
            BearerError::Invalid
        );
        assert_eq!(
            issue_run_bearer(RunBearerIdentity::Integer(42), 1000, b"").unwrap_err(),
            BearerError::SecretUnavailable
        );
    }

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
