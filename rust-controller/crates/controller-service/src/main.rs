mod config;

fn main() -> anyhow::Result<()> {
    let config = config::ControllerConfig::from_env()?;
    config.validate_network_boundary()?;

    println!(
        "controller-service version {} mode {}",
        env!("CARGO_PKG_VERSION"),
        config.mode.as_str()
    );

    Ok(())
}
