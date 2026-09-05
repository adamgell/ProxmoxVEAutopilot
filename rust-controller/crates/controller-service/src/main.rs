mod config;
mod health;
mod observe;
mod pve_credentials;
mod pve_observation;
mod runtime;

// Keep dependency errors out of startup diagnostics. This private category pins
// rejection at the network boundary independently of later configuration errors.
enum StartupFailure {
    NetworkBoundary,
    Other,
}

impl StartupFailure {
    fn label(self) -> &'static str {
        match self {
            Self::NetworkBoundary => "controller startup rejected by network boundary",
            Self::Other => {
                "controller startup or runtime failed; check local configuration and dependencies"
            }
        }
    }
}

#[tokio::main]
async fn main() {
    if let Err(failure) = start().await {
        // Errors may contain DSNs, paths or database text. Emit a fixed label.
        eprintln!("{}", failure.label());
        std::process::exit(1);
    }
}

async fn start() -> Result<(), StartupFailure> {
    let config = config::ControllerConfig::from_env().map_err(|_| StartupFailure::Other)?;
    config
        .validate_network_boundary()
        .map_err(|_| StartupFailure::NetworkBoundary)?;

    runtime::serve(config)
        .await
        .map_err(|_| StartupFailure::Other)
}
