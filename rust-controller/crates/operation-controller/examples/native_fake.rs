//! Disposable local proof. No DB/PVE destination, credential, or mode input.
//! Work has a 30-second async budget. Cancellation can add up to 200ms for
//! child reaping plus one 3-second container cleanup budget (33.2s total,
//! apart from OS scheduling delays). Unconfirmed cleanup fails the proof;
//! a container created without a returned ID may remain if ownership cannot
//! be verified before that budget expires.
use controller_domain::ExecutionState;
use operation_controller::NativeProgress;
use std::{process::ExitCode, time::Duration};
#[path = "../proof_support/mod.rs"]
mod support;

#[tokio::main]
async fn main() -> ExitCode {
    // Fixture failures are deliberately fixed labels, never connection details.
    std::panic::set_hook(Box::new(|_| eprintln!("native_fake_proof_failed")));
    let result = tokio::time::timeout(Duration::from_secs(30), async {
        let fixture = support::Fixture::new().await;
        let ids = fixture.enqueue().await;
        let mut controller = fixture.controller("native-example");
        for operation in [ids.clone_id(), ids.configure_id(), ids.start_id()] {
            loop {
                let progress = controller.run_once(operation).await.map_err(|_| ())?;
                match progress {
                    NativeProgress::Decided(ExecutionState::Satisfied) => break,
                    NativeProgress::Decided(ExecutionState::Unknown) => {
                        if controller.reconcile_once(operation).await.map_err(|_| ())?
                            == NativeProgress::Decided(ExecutionState::Satisfied)
                        {
                            break;
                        }
                    }
                    NativeProgress::Idle | NativeProgress::Waiting => {}
                    NativeProgress::Decided(_) => return Err(()),
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
        fixture.assert_success().await;
        let summary = fixture.summary().await;
        fixture.cleanup()?;
        Ok(summary)
    })
    .await;
    if let Ok(Ok(summary)) = result {
        println!("{summary}");
        ExitCode::SUCCESS
    } else {
        eprintln!("native_fake_proof_failed");
        ExitCode::FAILURE
    }
}
