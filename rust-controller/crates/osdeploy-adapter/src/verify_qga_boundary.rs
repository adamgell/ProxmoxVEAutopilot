//! Report shape checks only; no authenticated host evidence constructor.
use crate::StartPeArmingError;
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VerifyQgaBindingV1 {
    run: Uuid,
    install_operation: Uuid,
    install_attempt: Uuid,
    verify_operation: Uuid,
    verify_attempt: Uuid,
    install_receipt_sha256: String,
    node: String,
    vmid: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VerifyQgaReportV1 {
    pub version: u8,
    pub binding: VerifyQgaBindingV1,
    pub reported_responsive: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VerifyQgaRefusal {
    MissingReport,
    NotResponsive,
    AuthenticatedHostEvidenceUnavailable,
}
impl VerifyQgaBindingV1 {
    /// IDs: run, InstallQga operation/attempt, VerifyQga operation/attempt.
    /// Receipt digest is a claimed byte identity, not a verified acceptance.
    pub fn new(
        ids: [Uuid; 5],
        install_receipt_sha256: String,
        node: String,
        vmid: u32,
    ) -> Result<Self, StartPeArmingError> {
        if ids.iter().any(Uuid::is_nil)
            || ids[1] == ids[3]
            || ids[2] == ids[4]
            || install_receipt_sha256.len() != 64
            || !install_receipt_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || node.is_empty()
            || node.len() > 256
            || node.trim() != node
            || node.chars().any(char::is_control)
            || vmid == 0
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            run: ids[0],
            install_operation: ids[1],
            install_attempt: ids[2],
            verify_operation: ids[3],
            verify_attempt: ids[4],
            install_receipt_sha256,
            node,
            vmid,
        })
    }
    /// The expected binding must eventually come from trusted journal history.
    /// Even byte-matching reports cannot certify host or guest authentication.
    pub fn assess(
        &self,
        report: Option<&VerifyQgaReportV1>,
    ) -> Result<VerifyQgaRefusal, StartPeArmingError> {
        let Some(report) = report else {
            return Ok(VerifyQgaRefusal::MissingReport);
        };
        if report.version != 1 || &report.binding != self {
            return Err(StartPeArmingError);
        }
        if !report.reported_responsive {
            return Ok(VerifyQgaRefusal::NotResponsive);
        }
        Ok(VerifyQgaRefusal::AuthenticatedHostEvidenceUnavailable)
    }
}
