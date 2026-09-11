use std::collections::BTreeMap;

use serde_json::{Number, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

const MAX_SAFE_INTEGRAL_FLOAT: f64 = 9_007_199_254_740_991.0;

#[derive(Debug, Error)]
pub enum CanonicalizationError {
    #[error("JSON number is not a supported finite value")]
    NonFiniteFloat,
    #[error("integral JSON float exceeds the exact safe-integer range")]
    IntegralFloatOutsideSafeRange,
    #[error("JSON number cannot be represented by the canonical numeric policy")]
    UnsupportedNumber,
    #[error(transparent)]
    Serialization(#[from] serde_json::Error),
}

/// Returns compact canonical JSON suitable for idempotency hashing.
///
/// Object keys are recursively sorted and array order is retained. Integer-backed
/// JSON numbers keep their exact decimal value. Float-backed numbers are treated
/// as their parsed finite `f64` value: signed zero becomes `0`, and integral
/// floats within the exact IEEE-754 safe-integer range become the matching integer
/// spelling. Non-integral floats use serde_json's shortest deterministic `f64`
/// spelling. Integral floats outside the safe-integer range are rejected rather
/// than risk collapsing a rounded float with a distinct integer payload.
pub fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>, CanonicalizationError> {
    serde_json::to_vec(&canonicalize(value)?).map_err(Into::into)
}

pub fn payload_digest(value: &Value) -> Result<String, CanonicalizationError> {
    let canonical = canonical_json_bytes(value)?;
    let digest = Sha256::digest(canonical);

    Ok(hex::encode(digest))
}

fn canonicalize(value: &Value) -> Result<Value, CanonicalizationError> {
    match value {
        Value::Array(values) => values
            .iter()
            .map(canonicalize)
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array),
        Value::Object(values) => {
            let sorted = values
                .iter()
                .map(|(key, value)| Ok((key.clone(), canonicalize(value)?)))
                .collect::<Result<BTreeMap<_, _>, CanonicalizationError>>()?;
            Ok(Value::Object(sorted.into_iter().collect()))
        }
        Value::Number(number) => canonicalize_number(number),
        scalar => Ok(scalar.clone()),
    }
}

fn canonicalize_number(number: &Number) -> Result<Value, CanonicalizationError> {
    if let Some(integer) = number.as_i64() {
        return Ok(Value::Number(Number::from(integer)));
    }
    if let Some(integer) = number.as_u64() {
        return Ok(Value::Number(Number::from(integer)));
    }

    let float = number
        .as_f64()
        .ok_or(CanonicalizationError::UnsupportedNumber)?;
    if !float.is_finite() {
        return Err(CanonicalizationError::NonFiniteFloat);
    }
    if float == 0.0 {
        return Ok(Value::Number(Number::from(0)));
    }
    if float.fract() == 0.0 {
        if float.abs() > MAX_SAFE_INTEGRAL_FLOAT {
            return Err(CanonicalizationError::IntegralFloatOutsideSafeRange);
        }
        return Ok(Value::Number(Number::from(float as i64)));
    }

    Number::from_f64(float)
        .map(Value::Number)
        .ok_or(CanonicalizationError::NonFiniteFloat)
}
