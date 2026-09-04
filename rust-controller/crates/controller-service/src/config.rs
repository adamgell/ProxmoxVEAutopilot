use std::{env, str::FromStr};

use anyhow::{Context, Result, bail};

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
            database_url: "postgresql://localhost/rust_controller".into(),
            pve_base_url: "http://127.0.0.1:5000".into(),
            allow_production_reads: false,
        }
    }

    pub fn validate_network_boundary(&self) -> Result<()> {
        let production = self.pve_base_url.contains("192.168.2.4");
        if production && self.mode != ControllerMode::Observe {
            bail!(
                "{} mode cannot target production address 192.168.2.4",
                self.mode.as_str()
            );
        }
        if production && !self.allow_production_reads {
            bail!("production reads require explicit opt-in");
        }
        Ok(())
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
        assert_eq!(
            config.validate_network_boundary().unwrap_err().to_string(),
            "adapter mode cannot target production address 192.168.2.4"
        );
    }

    #[test]
    fn observe_requires_explicit_production_read_permission() {
        let mut config = ControllerConfig::local_observe();
        config.pve_base_url = "http://192.168.2.4:5000".into();
        assert!(config.validate_network_boundary().is_err());
        config.allow_production_reads = true;
        assert!(config.validate_network_boundary().is_ok());
    }
}
