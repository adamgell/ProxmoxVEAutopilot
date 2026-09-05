mod config;
mod health;
mod observe;
mod runtime;

#[tokio::main]
async fn main() {
    if start().await.is_err() {
        // Errors may contain DSNs, paths or database text. Emit a fixed label.
        eprintln!(
            "controller startup or runtime failed; check local configuration and dependencies"
        );
        std::process::exit(1);
    }
}

async fn start() -> anyhow::Result<()> {
    let config = config::ControllerConfig::from_env()?;
    config.validate_network_boundary()?;

    runtime::serve(config).await
}
