use std::{env, net::IpAddr, str::FromStr};

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

#[cfg(test)]
mod tests {
    use super::{ControllerConfig, ControllerMode};

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
