use std::{collections::BTreeMap, fmt, net::IpAddr};

use serde::{Deserialize, Deserializer, de};
use serde_json::{Map, Number, Value};
use thiserror::Error;

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
        serde_json::from_str(json).map_err(JobValidationError::InvalidJson)
    }

    pub fn validate_sanitized(&self) -> Result<(), JobValidationError> {
        let value = serde_json::to_value(JobEnvelopeWireRef::from(self))
            .map_err(JobValidationError::InvalidJson)?;
        validate_value(&value)
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

impl TryFrom<Value> for JobEnvelope {
    type Error = JobValidationError;

    fn try_from(value: Value) -> Result<Self, Self::Error> {
        validate_value(&value)?;
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
}

impl<'de> Deserialize<'de> for JobEnvelope {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        UniqueValue::deserialize(deserializer)?
            .0
            .try_into()
            .map_err(de::Error::custom)
    }
}

#[derive(Debug, Error)]
pub enum JobValidationError {
    #[error("invalid legacy job JSON: {0}")]
    InvalidJson(serde_json::Error),
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
        D: Deserializer<'de>,
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
            if values.contains_key(&key) {
                return Err(de::Error::custom(JobValidationError::DuplicateKey(key)));
            }
            let value = map.next_value::<UniqueValue>()?;
            values.insert(key, value.0);
        }
        Ok(UniqueValue(Value::Object(values)))
    }
}

fn validate_value(value: &Value) -> Result<(), JobValidationError> {
    match value {
        Value::Object(values) => {
            for (key, value) in values {
                validate_key(key, value)?;
                validate_value(value)?;
            }
        }
        Value::Array(values) => {
            for value in values {
                validate_value(value)?;
            }
        }
        Value::String(value) => validate_string(value)?,
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
    Ok(())
}

fn validate_key(key: &str, value: &Value) -> Result<(), JobValidationError> {
    let normalized: String = key
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    if ["token", "password", "secret", "bearer", "privatekey"]
        .iter()
        .any(|pattern| normalized.contains(pattern))
    {
        return Err(JobValidationError::UnsafeKey);
    }

    if [
        "tenantid",
        "tenantuuid",
        "applicationid",
        "applicationuuid",
        "clientid",
        "appid",
    ]
    .contains(&normalized.as_str())
        && value.as_str().is_some_and(is_uuid)
    {
        return Err(JobValidationError::UnsafeKey);
    }
    Ok(())
}

fn validate_string(value: &str) -> Result<(), JobValidationError> {
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
    if contains_non_loopback_ip(value) {
        return Err(JobValidationError::NonLoopbackAddress);
    }
    Ok(())
}

fn is_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23]
            .into_iter()
            .all(|index| bytes.get(index) == Some(&b'-'))
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| [8, 13, 18, 23].contains(&index) || byte.is_ascii_hexdigit())
}

fn contains_non_loopback_ip(value: &str) -> bool {
    value
        .split(|character: char| {
            !(character.is_ascii_hexdigit() || character == '.' || character == ':')
        })
        .filter(|candidate| !candidate.is_empty())
        .filter_map(parse_ip_candidate)
        .any(|address| !address.is_loopback())
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
