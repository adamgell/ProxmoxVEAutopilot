mod config;
mod observe;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let config = config::ControllerConfig::from_env()?;
    config.validate_network_boundary()?;

    if config.mode == config::ControllerMode::Observe {
        println!("{}", observe::run(&config.database_url).await?);
    } else {
        println!(
            "controller-service version {} mode {}",
            env!("CARGO_PKG_VERSION"),
            config.mode.as_str()
        );
    }

    Ok(())
}
