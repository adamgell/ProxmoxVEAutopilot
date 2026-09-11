fn main() {
    println!("cargo:rerun-if-env-changed=CONTROLLER_GIT_SHA");
    // CI/container builds pass a verified source revision without needing .git.
    // Local builds resolve HEAD, never user-controlled text or credentials.
    let sha = std::env::var("CONTROLLER_GIT_SHA")
        .ok()
        .or_else(|| {
            std::process::Command::new("git")
                .args(["rev-parse", "HEAD"])
                .output()
                .ok()
                .filter(|output| output.status.success())
                .and_then(|output| String::from_utf8(output.stdout).ok())
        })
        .unwrap_or_default();
    let sha = sha.trim();
    assert!(
        sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()),
        "a verified Git SHA is required"
    );
    println!("cargo:rustc-env=CONTROLLER_GIT_SHA={sha}");
}
