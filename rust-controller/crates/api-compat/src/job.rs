use std::{
    collections::BTreeMap,
    fmt,
    net::{IpAddr, Ipv4Addr},
};

use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD},
};
use serde::{Deserialize, de};
use serde_json::{Map, Number, Value};
use thiserror::Error;

const MAX_JSON_BYTES: usize = 16 * 1024;
const MAX_DEPTH: usize = 8;
const MAX_CONTAINER_ITEMS: usize = 32;
const MAX_STRING_BYTES: usize = 1024;
const MAX_DECODE_ROUNDS: usize = 3;

/// A legacy job can only enter through duplicate-aware raw JSON parsing.
/// Already-collapsed `serde_json::Value` input is deliberately unsupported.
///
/// ```compile_fail
/// use api_compat::JobEnvelope;
///
/// let value = serde_json::json!({"id": "already-collapsed"});
/// let _ = JobEnvelope::try_from(value);
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JobEnvelope {
    id: String,
    job_type: String,
    playbook: String,
    cmd: Vec<String>,
    args: BTreeMap<String, Value>,
    status: String,
}

impl JobEnvelope {
    pub fn from_json_str(json: &str) -> Result<Self, JobValidationError> {
        Self::from_json_bytes(json.as_bytes())
    }

    pub fn from_json_bytes(json: &[u8]) -> Result<Self, JobValidationError> {
        if json.len() > MAX_JSON_BYTES {
            return Err(JobValidationError::DocumentTooLarge);
        }
        let unique: UniqueValue =
            serde_json::from_slice(json).map_err(JobValidationError::InvalidJson)?;
        Self::from_unique_value(unique.0)
    }

    pub fn validate_sanitized(&self) -> Result<(), JobValidationError> {
        let value = serde_json::to_value(JobEnvelopeWireRef::from(self))
            .map_err(JobValidationError::InvalidJson)?;
        validate_value(&value, 0)
    }

    fn from_unique_value(value: Value) -> Result<Self, JobValidationError> {
        validate_value(&value, 0)?;
        let wire: JobEnvelopeWire =
            serde_json::from_value(value).map_err(JobValidationError::InvalidJson)?;
        let envelope = Self {
            id: wire.id,
            job_type: wire.job_type,
            playbook: wire.playbook,
            cmd: wire.cmd,
            args: wire.args,
            status: wire.status,
        };
        envelope.validate_sanitized()?;
        Ok(envelope)
    }

    pub(crate) fn id(&self) -> &str {
        &self.id
    }

    pub(crate) fn job_type(&self) -> &str {
        &self.job_type
    }

    pub(crate) fn playbook(&self) -> &str {
        &self.playbook
    }

    pub(crate) fn command(&self) -> &[String] {
        &self.cmd
    }

    pub(crate) const fn args(&self) -> &BTreeMap<String, Value> {
        &self.args
    }

    pub(crate) fn status(&self) -> &str {
        &self.status
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JobEnvelopeWire {
    id: String,
    job_type: String,
    playbook: String,
    cmd: Vec<String>,
    args: BTreeMap<String, Value>,
    status: String,
}

#[derive(serde::Serialize)]
struct JobEnvelopeWireRef<'a> {
    id: &'a str,
    job_type: &'a str,
    playbook: &'a str,
    cmd: &'a [String],
    args: &'a BTreeMap<String, Value>,
    status: &'a str,
}

impl<'a> From<&'a JobEnvelope> for JobEnvelopeWireRef<'a> {
    fn from(value: &'a JobEnvelope) -> Self {
        Self {
            id: &value.id,
            job_type: &value.job_type,
            playbook: &value.playbook,
            cmd: &value.cmd,
            args: &value.args,
            status: &value.status,
        }
    }
}

#[derive(Debug, Error)]
pub enum JobValidationError {
    #[error("invalid legacy job JSON: {0}")]
    InvalidJson(serde_json::Error),
    #[error("legacy job JSON exceeds the size limit")]
    DocumentTooLarge,
    #[error("legacy job JSON exceeds the nesting limit")]
    TooDeep,
    #[error("legacy job JSON container exceeds the item limit")]
    ContainerTooLarge,
    #[error("legacy job contains an oversized or non-ASCII string")]
    InvalidCharacters,
    #[error("legacy job contains malformed encoded text")]
    UnsafeEncoding,
    #[error("duplicate JSON key: {0}")]
    DuplicateKey(String),
    #[error("unsafe key in sanitized job")]
    UnsafeKey,
    #[error("unsafe value in sanitized job")]
    UnsafeValue,
    #[error("non-loopback network address in sanitized job")]
    NonLoopbackAddress,
}

struct UniqueValue(Value);

impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueValueVisitor)
    }
}

struct UniqueValueVisitor;

impl<'de> de::Visitor<'de> for UniqueValueVisitor {
    type Value = UniqueValue;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("JSON without duplicate object keys")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Bool(value)))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Number(Number::from(value))))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Number(Number::from(value))))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Number::from_f64(value)
            .map(Value::Number)
            .map(UniqueValue)
            .ok_or_else(|| E::custom("non-finite JSON number"))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        self.visit_string(value.to_owned())
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::String(value)))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Null))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Null))
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: de::SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(value) = sequence.next_element::<UniqueValue>()? {
            if values.len() == MAX_CONTAINER_ITEMS {
                return Err(de::Error::custom(JobValidationError::ContainerTooLarge));
            }
            values.push(value.0);
        }
        Ok(UniqueValue(Value::Array(values)))
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: de::MapAccess<'de>,
    {
        let mut values = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if values.len() == MAX_CONTAINER_ITEMS {
                return Err(de::Error::custom(JobValidationError::ContainerTooLarge));
            }
            if values.contains_key(&key) {
                return Err(de::Error::custom(JobValidationError::DuplicateKey(key)));
            }
            let value = map.next_value::<UniqueValue>()?;
            values.insert(key, value.0);
        }
        Ok(UniqueValue(Value::Object(values)))
    }
}

fn validate_value(value: &Value, depth: usize) -> Result<(), JobValidationError> {
    if depth > MAX_DEPTH {
        return Err(JobValidationError::TooDeep);
    }
    match value {
        Value::Object(values) => {
            if values.len() > MAX_CONTAINER_ITEMS {
                return Err(JobValidationError::ContainerTooLarge);
            }
            for (key, value) in values {
                validate_ascii(key)?;
                validate_key(key)?;
                validate_value(value, depth + 1)?;
            }
        }
        Value::Array(values) => {
            if values.len() > MAX_CONTAINER_ITEMS {
                return Err(JobValidationError::ContainerTooLarge);
            }
            for value in values {
                validate_value(value, depth + 1)?;
            }
        }
        Value::String(value) => validate_string(value)?,
        Value::Number(value) => validate_integer_address(value)?,
        Value::Null | Value::Bool(_) => {}
    }
    Ok(())
}

fn validate_ascii(value: &str) -> Result<(), JobValidationError> {
    if value.len() > MAX_STRING_BYTES
        || !value
            .bytes()
            .all(|byte| byte.is_ascii() && (!byte.is_ascii_control() || byte == b' '))
    {
        return Err(JobValidationError::InvalidCharacters);
    }
    Ok(())
}

fn validate_key(key: &str) -> Result<(), JobValidationError> {
    let normalized = key
        .bytes()
        .filter(u8::is_ascii_alphanumeric)
        .map(|byte| byte.to_ascii_lowercase())
        .collect::<Vec<_>>();
    let normalized = String::from_utf8(normalized).expect("ASCII normalization stays UTF-8");
    if ["token", "password", "secret", "bearer", "privatekey"]
        .iter()
        .any(|pattern| normalized.contains(pattern))
        || (normalized != "id"
            && [
                "tenant",
                "directoryid",
                "application",
                "appid",
                "clientid",
                "serviceprincipal",
                "objectid",
            ]
            .iter()
            .any(|pattern| normalized.contains(pattern)))
    {
        return Err(JobValidationError::UnsafeKey);
    }
    Ok(())
}

fn validate_string(value: &str) -> Result<(), JobValidationError> {
    validate_ascii(value)?;
    validate_decoded(value, 0)
}

fn validate_decoded(value: &str, round: usize) -> Result<(), JobValidationError> {
    let lowercase = value.to_ascii_lowercase();
    if [
        "token",
        "password",
        "secret",
        "bearer",
        "pveapitoken",
        "private key",
    ]
    .iter()
    .any(|pattern| lowercase.contains(pattern))
    {
        return Err(JobValidationError::UnsafeValue);
    }
    if contains_non_loopback_ip(value) || contains_non_loopback_integer_ip(value) {
        return Err(JobValidationError::NonLoopbackAddress);
    }
    if round == MAX_DECODE_ROUNDS {
        return Ok(());
    }

    if value.contains('%') {
        let decoded = percent_decode(value)?;
        validate_ascii(&decoded)?;
        validate_decoded(&decoded, round + 1)?;
    }
    if looks_like_base64(value)
        && let Some(decoded) = decode_base64(value)
        && let Ok(decoded) = String::from_utf8(decoded)
    {
        validate_ascii(&decoded)?;
        validate_decoded(&decoded, round + 1)?;
    }
    Ok(())
}

fn percent_decode(value: &str) -> Result<String, JobValidationError> {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'%' {
            decoded.push(bytes[index]);
            index += 1;
            continue;
        }
        let Some(hex) = bytes.get(index + 1..index + 3) else {
            return Err(JobValidationError::UnsafeEncoding);
        };
        let hex = std::str::from_utf8(hex).map_err(|_| JobValidationError::UnsafeEncoding)?;
        decoded.push(u8::from_str_radix(hex, 16).map_err(|_| JobValidationError::UnsafeEncoding)?);
        index += 3;
    }
    String::from_utf8(decoded).map_err(|_| JobValidationError::UnsafeEncoding)
}

fn looks_like_base64(value: &str) -> bool {
    value.len() >= 8
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'-' | b'_' | b'=')
        })
}

fn decode_base64(value: &str) -> Option<Vec<u8>> {
    STANDARD
        .decode(value)
        .or_else(|_| STANDARD_NO_PAD.decode(value))
        .or_else(|_| URL_SAFE.decode(value))
        .or_else(|_| URL_SAFE_NO_PAD.decode(value))
        .ok()
}

fn validate_integer_address(value: &Number) -> Result<(), JobValidationError> {
    if let Some(value) = value.as_u64()
        && let Ok(value) = u32::try_from(value)
        && value >= 0x0100_0000
        && !Ipv4Addr::from(value).is_loopback()
    {
        return Err(JobValidationError::NonLoopbackAddress);
    }
    Ok(())
}

fn contains_non_loopback_integer_ip(value: &str) -> bool {
    value
        .split(|character: char| !character.is_ascii_hexdigit() && !matches!(character, 'x' | 'X'))
        .filter_map(|candidate| {
            candidate
                .strip_prefix("0x")
                .or_else(|| candidate.strip_prefix("0X"))
                .map_or_else(
                    || {
                        candidate
                            .parse::<u32>()
                            .ok()
                            .filter(|_| candidate.len() >= 8)
                    },
                    |hex| u32::from_str_radix(hex, 16).ok(),
                )
        })
        .filter(|value| *value >= 0x0100_0000)
        .any(|value| !Ipv4Addr::from(value).is_loopback())
}

fn contains_non_loopback_ip(value: &str) -> bool {
    value
        .split(|character: char| {
            !(character.is_ascii_hexdigit() || character == '.' || character == ':')
        })
        .filter(|candidate| !candidate.is_empty())
        .filter_map(parse_ip_candidate)
        .any(|address| !is_normalized_loopback(address))
}

fn parse_ip_candidate(candidate: &str) -> Option<IpAddr> {
    if let Ok(address) = candidate.parse() {
        return Some(address);
    }
    let (host, port) = candidate.rsplit_once(':')?;
    if !port.chars().all(|character| character.is_ascii_digit()) {
        return None;
    }
    host.parse().ok()
}

fn is_normalized_loopback(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => address.is_loopback(),
        IpAddr::V6(address) => {
            address.is_loopback()
                || address
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_loopback())
        }
    }
}
