use std::fmt;

use reqwest::header::HeaderValue;
use thiserror::Error;

#[derive(Clone)]
pub struct PveApiToken(HeaderValue);

#[derive(Clone, Copy, Debug, Error, Eq, PartialEq)]
#[error("invalid PVE API token")]
pub struct InvalidPveApiToken;

impl PveApiToken {
    pub fn parse(id: &str, secret: &str) -> Result<Self, InvalidPveApiToken> {
        fn component(value: &str) -> bool {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        }
        let (user_realm, token) = id.split_once('!').ok_or(InvalidPveApiToken)?;
        let (user, realm) = user_realm.split_once('@').ok_or(InvalidPveApiToken)?;
        if !component(user)
            || !component(realm)
            || !component(token)
            || secret.is_empty()
            || secret.len() > 4096
            || !secret
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        {
            return Err(InvalidPveApiToken);
        }
        let mut header = HeaderValue::from_str(&format!("PVEAPIToken={id}={secret}"))
            .map_err(|_| InvalidPveApiToken)?;
        header.set_sensitive(true);
        Ok(Self(header))
    }

    pub(crate) fn header(&self) -> HeaderValue {
        self.0.clone()
    }
}

impl fmt::Debug for PveApiToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PveApiToken(<redacted>)")
    }
}
