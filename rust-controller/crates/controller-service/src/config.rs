use std::{
    env,
    ffi::OsString,
    net::IpAddr,
    path::{Path, PathBuf},
    str::FromStr,
};

use anyhow::{Context, Result, bail};
use pve_port::PveBaseUrl;
use sqlx::postgres::PgConnectOptions;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ControllerMode {
    Observe,
    Adapter,
    Native,
}

impl ControllerMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Observe => "observe",
            Self::Adapter => "adapter",
            Self::Native => "native",
        }
    }
}

impl FromStr for ControllerMode {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        match value {
            "observe" => Ok(Self::Observe),
            "adapter" => Ok(Self::Adapter),
            "native" => Ok(Self::Native),
            _ => bail!("RUST_CONTROLLER_MODE must be observe, adapter, or native"),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControllerConfig {
    pub mode: ControllerMode,
    pub database_url: String,
    pub pve_base_url: String,
    pub allow_production_reads: bool,
}

impl ControllerConfig {
    pub fn from_env() -> Result<Self> {
        let mode = required_env("RUST_CONTROLLER_MODE")?.parse()?;
        let database_url = required_env("RUST_CONTROLLER_DATABASE_URL")?;
        let pve_base_url = required_env("RUST_CONTROLLER_PVE_BASE_URL")?;
        let allow_production_reads = env::var("RUST_CONTROLLER_ALLOW_PRODUCTION_READS")
            .unwrap_or_else(|_| "false".to_owned())
            .parse()
            .context("RUST_CONTROLLER_ALLOW_PRODUCTION_READS must be true or false")?;

        Ok(Self {
            mode,
            database_url,
            pve_base_url,
            allow_production_reads,
        })
    }

    #[cfg(test)]
    fn local_observe() -> Self {
        Self {
            mode: ControllerMode::Observe,
            database_url: "postgresql://127.0.0.1/rust_controller".into(),
            pve_base_url: "http://127.0.0.1:5000".into(),
            allow_production_reads: false,
        }
    }

    pub fn validate_network_boundary(&self) -> Result<()> {
        let database = PgConnectOptions::from_str(&self.database_url)
            .context("RUST_CONTROLLER_DATABASE_URL must be a PostgreSQL URL")?;
        let database_loopback = database
            .get_host()
            .trim_start_matches('[')
            .trim_end_matches(']')
            .parse::<IpAddr>()
            .is_ok_and(is_normalized_loopback);
        let pve = PveBaseUrl::parse(&self.pve_base_url)
            .context("RUST_CONTROLLER_PVE_BASE_URL must be a validated HTTP(S) URL")?;
        let pve_loopback = pve.is_loopback();
        let remote_reads_allowed =
            self.mode == ControllerMode::Observe && self.allow_production_reads;

        if (!database_loopback || !pve_loopback) && !remote_reads_allowed {
            bail!("non-loopback reads require observe mode and explicit read permission");
        }
        Ok(())
    }
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

fn required_env(name: &str) -> Result<String> {
    env::var(name).with_context(|| format!("{name} is required"))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PveTransport {
    Fake,
    HttpObserve,
}

impl PveTransport {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Fake => "fake",
            Self::HttpObserve => "http-observe",
        }
    }
}

pub(crate) struct ValidatedObservationConfig {
    base_url: PveBaseUrl,
    token_file: PathBuf,
    allow_production_reads: bool,
}

impl ValidatedObservationConfig {
    pub(crate) fn base_url(&self) -> &PveBaseUrl {
        &self.base_url
    }
    pub(crate) fn token_file(&self) -> &Path {
        &self.token_file
    }
    pub(crate) fn allow_production_reads(&self) -> bool {
        self.allow_production_reads
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct ObservationConfigFailure;

impl std::fmt::Display for ObservationConfigFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("observation configuration rejected")
    }
}
impl std::error::Error for ObservationConfigFailure {}

pub(crate) fn observation_config(
    config: &ControllerConfig,
) -> Result<Option<ValidatedObservationConfig>, ObservationConfigFailure> {
    observation_config_from(config, |name| env::var_os(name))
}

fn observation_config_from(
    config: &ControllerConfig,
    mut lookup: impl FnMut(&str) -> Option<OsString>,
) -> Result<Option<ValidatedObservationConfig>, ObservationConfigFailure> {
    config
        .validate_network_boundary()
        .map_err(|_| ObservationConfigFailure)?;
    let transport = match lookup("RUST_CONTROLLER_PVE_TRANSPORT") {
        None => PveTransport::Fake,
        Some(value) if value == PveTransport::Fake.as_str() => PveTransport::Fake,
        Some(value) if value == PveTransport::HttpObserve.as_str() => PveTransport::HttpObserve,
        Some(_) => return Err(ObservationConfigFailure),
    };
    if transport == PveTransport::Fake {
        return Ok(None);
    }
    if config.mode != ControllerMode::Observe {
        return Err(ObservationConfigFailure);
    }
    // PveBaseUrl already requires HTTPS for every non-loopback destination.
    let base_url = PveBaseUrl::parse(&config.pve_base_url).map_err(|_| ObservationConfigFailure)?;
    let token_file = lookup("RUST_CONTROLLER_PVE_TOKEN_FILE")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or(ObservationConfigFailure)?;
    Ok(Some(ValidatedObservationConfig {
        base_url,
        token_file,
        allow_production_reads: config.allow_production_reads,
    }))
}

#[cfg(test)]
pub(crate) fn observation_test_config(path: &Path) -> ValidatedObservationConfig {
    observation_test_config_at(path, "http://127.0.0.1:5000")
}

#[cfg(test)]
pub(crate) fn observation_test_config_at(
    path: &Path,
    base_url: &str,
) -> ValidatedObservationConfig {
    let mut config = ControllerConfig::local_observe();
    config.pve_base_url = base_url.to_owned();
    observation_config_from(&config, |name| match name {
        "RUST_CONTROLLER_PVE_TRANSPORT" => Some("http-observe".into()),
        "RUST_CONTROLLER_PVE_TOKEN_FILE" => Some(path.as_os_str().to_owned()),
        _ => None,
    })
    .ok()
    .flatten()
    .expect("synthetic observation config")
}

#[cfg(test)]
mod tests {
    use super::*;

    // Break: accepting a denied selector, mode or target exposes credentials to a loader.
    #[test]
    fn observation_gates_before_loader_matrix() {
        let targets = [
            ("http://127.0.0.1:8006", true, true),
            ("http://[::1]:8006", true, true),
            ("http://[::ffff:127.0.0.1]:8006", true, true),
            ("https://192.0.2.1:8006", false, true),
            ("https://[2001:db8::1]:8006", false, true),
            ("https://pve.example.invalid:8006", false, true),
            ("http://192.0.2.1:8006", false, false),
            ("http://[2001:db8::1]:8006", false, false),
            ("http://pve.example.invalid:8006", false, false),
        ];
        for mode in [
            ControllerMode::Observe,
            ControllerMode::Adapter,
            ControllerMode::Native,
        ] {
            for transport in ["fake", "http-observe", "real", "unknown"] {
                for (target, loopback, safe_scheme) in targets {
                    for allow in [false, true] {
                        let config = ControllerConfig {
                            mode,
                            pve_base_url: target.into(),
                            allow_production_reads: allow,
                            ..ControllerConfig::local_observe()
                        };
                        let mut file_references = 0;
                        let selected = observation_config_from(&config, |name| match name {
                            "RUST_CONTROLLER_PVE_TRANSPORT" => Some(transport.into()),
                            "RUST_CONTROLLER_PVE_TOKEN_FILE" => {
                                file_references += 1;
                                Some("synthetic-not-opened.json".into())
                            }
                            _ => panic!("unexpected lookup"),
                        });
                        let network_allowed =
                            safe_scheme && (loopback || (mode == ControllerMode::Observe && allow));
                        let expected = network_allowed
                            && (transport == "fake"
                                || (transport == "http-observe"
                                    && mode == ControllerMode::Observe));
                        assert_eq!(
                            selected.is_ok(),
                            expected,
                            "mode={mode:?} transport={transport} target={target} allow={allow}"
                        );
                        if let Err(error) = &selected {
                            assert_eq!(error.to_string(), "observation configuration rejected");
                            assert_eq!(format!("{error:?}"), "ObservationConfigFailure");
                            assert!(std::error::Error::source(error).is_none());
                        }
                        let mut loader_calls = 0;
                        if let Ok(Some(validated)) = selected {
                            // Inert loader: records capability reachability, with no file/client I/O.
                            loader_calls += 1;
                            assert_eq!(validated.allow_production_reads(), allow);
                            assert_eq!(validated.base_url().is_loopback(), loopback);
                            assert_eq!(
                                validated.token_file(),
                                Path::new("synthetic-not-opened.json")
                            );
                        }
                        let wants_file = expected && transport == "http-observe";
                        assert_eq!(loader_calls, usize::from(wants_file));
                        assert_eq!(file_references, usize::from(wants_file));
                    }
                }
            }
        }
    }

    #[test]
    fn observation_fake_defaults_without_file_and_http_requires_nonempty_file() {
        let config = ControllerConfig::local_observe();
        assert!(matches!(
            observation_config_from(&config, |_| None),
            Ok(None)
        ));
        for file in [None, Some(OsString::new())] {
            let result = observation_config_from(&config, |name| {
                if name == "RUST_CONTROLLER_PVE_TRANSPORT" {
                    Some("http-observe".into())
                } else {
                    file.clone()
                }
            });
            assert!(matches!(result, Err(ObservationConfigFailure)));
        }
        assert_eq!(PveTransport::Fake.as_str(), "fake");
        assert_eq!(PveTransport::HttpObserve.as_str(), "http-observe");
    }

    #[test]
    fn observation_rechecks_database_boundary_before_file_reference() {
        for host in ["192.0.2.1", "[2001:db8::1]", "db.example.invalid"] {
            for mode in [
                ControllerMode::Observe,
                ControllerMode::Adapter,
                ControllerMode::Native,
            ] {
                for allow in [false, true] {
                    let config = ControllerConfig {
                        mode,
                        database_url: format!("postgresql://{host}/proof"),
                        allow_production_reads: allow,
                        ..ControllerConfig::local_observe()
                    };
                    let mut file_references = 0;
                    let result = observation_config_from(&config, |name| {
                        if name == "RUST_CONTROLLER_PVE_TRANSPORT" {
                            Some("http-observe".into())
                        } else {
                            file_references += 1;
                            Some("synthetic-not-opened.json".into())
                        }
                    });
                    let accepted = mode == ControllerMode::Observe && allow;
                    assert_eq!(result.is_ok(), accepted);
                    assert_eq!(file_references, usize::from(accepted));
                }
            }
        }
    }

    #[test]
    fn rejects_production_controller_in_adapter_mode() {
        let config = ControllerConfig {
            mode: ControllerMode::Adapter,
            database_url: "postgresql://localhost/rust_controller".into(),
            pve_base_url: "http://192.168.2.4:5000".into(),
            allow_production_reads: false,
        };
        assert!(config.validate_network_boundary().is_err());
    }

    #[test]
    fn observe_requires_explicit_production_read_permission() {
        let mut config = ControllerConfig::local_observe();
        config.pve_base_url = "https://192.168.2.4:5000".into();
        assert!(config.validate_network_boundary().is_err());
        config.allow_production_reads = true;
        assert!(config.validate_network_boundary().is_ok());
    }

    #[test]
    fn unapproved_observe_rejects_database_and_pve_aliases_before_construction() {
        let cases = [
            (
                "postgresql://observer@localhost/rust_controller",
                "http://127.0.0.1:5000",
            ),
            (
                "postgresql://observer@192.168.2.4/rust_controller",
                "http://127.0.0.1:5000",
            ),
            (
                "postgresql://observer@2130706433/rust_controller",
                "http://127.0.0.1:5000",
            ),
            (
                "postgresql://observer@127.0.0.1/rust_controller",
                "https://localhost:8006",
            ),
            (
                "postgresql://observer@127.0.0.1/rust_controller",
                "https://192.168.2.48:8006",
            ),
        ];

        for (database_url, pve_base_url) in cases {
            let config = ControllerConfig {
                mode: ControllerMode::Observe,
                database_url: database_url.to_owned(),
                pve_base_url: pve_base_url.to_owned(),
                allow_production_reads: false,
            };
            assert!(config.validate_network_boundary().is_err());
        }
    }

    #[test]
    fn explicit_remote_reads_are_limited_to_observe_mode() {
        let remote = ControllerConfig {
            mode: ControllerMode::Adapter,
            database_url: "postgresql://observer@192.168.2.4/rust_controller".into(),
            pve_base_url: "https://192.168.2.48:8006".into(),
            allow_production_reads: true,
        };
        assert!(remote.validate_network_boundary().is_err());

        let observe = ControllerConfig {
            mode: ControllerMode::Observe,
            ..remote
        };
        assert!(observe.validate_network_boundary().is_ok());
    }

    #[test]
    fn normalized_literal_loopback_endpoints_are_allowed_without_opt_in() {
        for host in ["127.0.0.1", "[::1]", "[::ffff:127.0.0.1]"] {
            let config = ControllerConfig {
                mode: ControllerMode::Observe,
                database_url: format!("postgresql://observer@{host}/rust_controller"),
                pve_base_url: format!("http://{host}:5000"),
                allow_production_reads: false,
            };
            assert!(
                config.validate_network_boundary().is_ok(),
                "rejected {host}"
            );
        }
    }
}
