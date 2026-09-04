use std::collections::BTreeMap;

use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(&canonicalize(value))
}

pub fn payload_digest(value: &Value) -> Result<String, serde_json::Error> {
    let canonical = canonical_json_bytes(value)?;
    let digest = Sha256::digest(canonical);

    Ok(hex::encode(digest))
}

fn canonicalize(value: &Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.iter().map(canonicalize).collect()),
        Value::Object(values) => {
            let sorted = values
                .iter()
                .map(|(key, value)| (key.clone(), canonicalize(value)))
                .collect::<BTreeMap<_, _>>();
            Value::Object(sorted.into_iter().collect())
        }
        scalar => scalar.clone(),
    }
}
