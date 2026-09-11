//! Descriptive agent package binding; not installation or enrollment authority.
use crate::{StartPeArmingError, WatchdogBindingV1};
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InstallAgentBindingV1 {
    predecessor: WatchdogBindingV1,
    operation: Uuid,
    attempt: Uuid,
    artifact: Uuid,
    watchdog_receipt_sha256: String,
    artifact_sha256: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InstallAgentReportV1 {
    pub version: u8,
    pub binding: InstallAgentBindingV1,
    pub claimed_installed: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallAgentRefusal {
    MissingReport,
    IncompleteReport,
    AuthenticatedInstallationUnavailable,
}
impl InstallAgentBindingV1 {
    /// IDs: InstallAgent operation/attempt, package artifact. Digests: watchdog
    /// receipt and artifact bytes. Predecessor retains exact VM/agent identity
    /// and VerifyQga result/receipt claims; none are authenticated here.
    pub fn new(
        predecessor: WatchdogBindingV1,
        ids: [Uuid; 3],
        digests: [String; 2],
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || digests.iter().any(|d| {
                d.len() != 64
                    || !d
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
        {
            return Err(StartPeArmingError);
        }
        let [watchdog_receipt_sha256, artifact_sha256] = digests;
        Ok(Self {
            predecessor,
            operation: ids[0],
            attempt: ids[1],
            artifact: ids[2],
            watchdog_receipt_sha256,
            artifact_sha256,
        })
    }
    /// Expected binding must eventually be reconstructed from trusted history.
    /// Even identical claimed installed results remain unauthenticated.
    pub fn assess(
        &self,
        report: Option<&InstallAgentReportV1>,
    ) -> Result<InstallAgentRefusal, StartPeArmingError> {
        let Some(report) = report else {
            return Ok(InstallAgentRefusal::MissingReport);
        };
        if report.version != 1 || &report.binding != self {
            return Err(StartPeArmingError);
        }
        if !report.claimed_installed {
            return Ok(InstallAgentRefusal::IncompleteReport);
        }
        Ok(InstallAgentRefusal::AuthenticatedInstallationUnavailable)
    }
}
