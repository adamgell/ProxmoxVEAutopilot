import { useMemo, useState } from "react";
import { Camera, Download, ExternalLink, Eye, EyeOff, RefreshCw } from "lucide-react";

import { postJson } from "../apiClient";
import type { VmCredentialsRevealResponse, VmDetailEvidenceResponse, VmKnownCredential, VmLinkageCheck, VmRevealedCredential, VmTimelineEvent } from "../contracts";
import { reactHrefForUiPath } from "../routes";
import { fallbackText, formatRelativeAge, formatShortDateTime, statusClass } from "../viewModels";
import { Panel } from "./ui";

function textField(row: Readonly<Record<string, unknown>> | undefined, keys: readonly string[]): string {
  if (!row) {
    return "-";
  }
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return "-";
}

function LinkageRow({ check }: { readonly check: VmLinkageCheck }) {
  const label = check.ok === true ? "matched" : check.ok === false ? "broken" : "pending";
  const tone = check.ok === true ? "healthy" : check.ok === false ? "failed" : "pending";
  return (
    <div className="evidence-row">
      <span>{check.label}</span>
      <strong>{fallbackText(check.value)}</strong>
      <span className={statusClass(tone)}>{label}</span>
    </div>
  );
}

function credentialKey(credential: Pick<VmKnownCredential | VmRevealedCredential, "source" | "label" | "username" | "run_id" | "updated_at">): string {
  return [
    credential.source,
    credential.label,
    credential.username,
    credential.run_id,
    credential.updated_at ?? ""
  ].join("\u001f");
}

function withoutCredentialKey(current: Readonly<Record<string, string>>, key: string): Record<string, string> {
  return Object.fromEntries(Object.entries(current).filter(([entryKey]) => entryKey !== key));
}

function CredentialRow({
  credential,
  password,
  isRevealing,
  onToggleReveal
}: {
  readonly credential: VmKnownCredential;
  readonly password: string | undefined;
  readonly isRevealing: boolean;
  readonly onToggleReveal: () => void;
}) {
  const isRevealed = Boolean(password);
  const actionLabel = `${isRevealed ? "Hide" : "Reveal"} ${fallbackText(credential.label)} password for ${fallbackText(credential.username)}`;
  return (
    <div className="evidence-credential">
      <div>
        <strong>{fallbackText(credential.label)}</strong>
        <span>{fallbackText(credential.source)}</span>
      </div>
      <div>
        <span>{fallbackText(credential.username)}</span>
        <span className="credential-secret">
          <code>{credential.password_available ? (password ?? credential.password_mask) : "-"}</code>
          {credential.password_available ? (
            <button
              type="button"
              className="credential-reveal-button"
              aria-label={actionLabel}
              title={actionLabel}
              onClick={onToggleReveal}
              disabled={isRevealing}
            >
              {isRevealed ? (
                <EyeOff aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
              ) : (
                <Eye aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
              )}
            </button>
          ) : null}
        </span>
      </div>
      <div>
        <span>{formatShortDateTime(credential.updated_at)}</span>
        {credential.run_url ? <a href={reactHrefForUiPath(credential.run_url)}>Run</a> : <span>-</span>}
      </div>
    </div>
  );
}

function TimelineRow({ event }: { readonly event: VmTimelineEvent }) {
  return (
    <li>
      <time dateTime={event.at}>{formatShortDateTime(event.at)}</time>
      <span className={statusClass(event.severity)}>{fallbackText(event.source)}</span>
      <strong>{fallbackText(event.type)}</strong>
      <span>{fallbackText(event.summary)}</span>
    </li>
  );
}

export function VmEvidencePanels({
  vmid,
  evidence,
  onRefreshScreenshot
}: {
  readonly vmid: number;
  readonly evidence: VmDetailEvidenceResponse | null;
  readonly onRefreshScreenshot: () => void;
}) {
  const screenshot = evidence?.latest_screenshot ?? null;
  const ad = evidence?.ad_matches[0];
  const entra = evidence?.entra_matches[0];
  const intune = evidence?.intune_matches[0];
  const sync = evidence?.identity_sync;
  const timeline = evidence?.timeline.slice(0, 8) ?? [];
  const [revealedPasswords, setRevealedPasswords] = useState<Readonly<Record<string, string>>>({});
  const [revealingKey, setRevealingKey] = useState("");
  const [revealError, setRevealError] = useState("");

  const credentialKeys = useMemo(
    () => new Set((evidence?.known_credentials ?? []).map((credential) => credentialKey(credential))),
    [evidence?.known_credentials]
  );
  const visibleRevealedPasswords = useMemo(() => {
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(revealedPasswords)) {
      if (credentialKeys.has(key)) {
        next[key] = value;
      }
    }
    return next;
  }, [credentialKeys, revealedPasswords]);

  async function toggleCredentialReveal(credential: VmKnownCredential): Promise<void> {
    const key = credentialKey(credential);
    if (visibleRevealedPasswords[key]) {
      setRevealedPasswords((current) => withoutCredentialKey(current, key));
      return;
    }
    setRevealingKey(key);
    setRevealError("");
    try {
      const response = await postJson<VmCredentialsRevealResponse>(`/api/vms/${String(vmid)}/credentials/reveal`);
      const next: Record<string, string> = {};
      for (const item of response.credentials) {
        if (item.password) {
          next[credentialKey(item)] = item.password;
        }
      }
      setRevealedPasswords((current) => ({ ...current, ...next }));
      if (!next[key]) {
        setRevealError("Credential password was not returned for this VM.");
      }
    } catch (err) {
      setRevealError(err instanceof Error ? err.message : "Credential reveal failed");
    } finally {
      setRevealingKey("");
    }
  }

  return (
    <section className="vm-evidence-grid" aria-label="VM evidence">
      <Panel title="Latest screenshot">
        <div className="screenshot-preview">
          {screenshot ? (
            <>
              <img src={screenshot.image_url} alt={`Latest VM ${String(vmid)} screenshot`} />
              <dl className="vm-detail-list vm-detail-list--compact">
                <div>
                  <dt>Captured</dt>
                  <dd title={formatShortDateTime(screenshot.captured_at)}>{formatRelativeAge(screenshot.captured_at)}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>{fallbackText(screenshot.source)}</dd>
                </div>
              </dl>
            </>
          ) : (
            <p className="empty">No screenshot yet.</p>
          )}
          <div className="evidence-actions">
            <button type="button" className="fleet-action" onClick={onRefreshScreenshot}>
              <RefreshCw aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
              <span>Refresh</span>
            </button>
            {screenshot ? (
              <>
                <a className="fleet-action" href={screenshot.image_url} target="_blank" rel="noreferrer">
                  <ExternalLink aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
                  <span>Open screenshot</span>
                </a>
                <a className="fleet-action" href={screenshot.image_url} download={`vm-${String(vmid)}-screenshot.png`}>
                  <Download aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
                  <span>Download</span>
                </a>
              </>
            ) : null}
          </div>
        </div>
      </Panel>

      <Panel title="Identity linkage">
        {evidence?.linkage.length ? (
          <div className="evidence-stack">
            {evidence.linkage.map((check) => <LinkageRow key={check.label} check={check} />)}
          </div>
        ) : <p className="empty">No linkage evidence yet.</p>}
      </Panel>

      <Panel title="Known credentials">
        {evidence?.known_credentials.length ? (
          <div className="evidence-stack">
            {evidence.known_credentials.map((credential) => (
              <CredentialRow
                key={credentialKey(credential)}
                credential={credential}
                password={visibleRevealedPasswords[credentialKey(credential)]}
                isRevealing={revealingKey === credentialKey(credential)}
                onToggleReveal={() => { void toggleCredentialReveal(credential); }}
              />
            ))}
            {revealError ? <p className="credential-reveal-error" role="alert">{revealError}</p> : null}
          </div>
        ) : <p className="empty">No visible credentials.</p>}
      </Panel>

      <Panel title="Directory evidence">
        <dl className="vm-detail-list">
          <div>
            <dt>AD</dt>
            <dd>{textField(ad, ["cn", "distinguishedName", "objectGUID"])}</dd>
          </div>
          <div>
            <dt>Entra</dt>
            <dd>{textField(entra, ["displayName", "deviceId", "trustType"])}</dd>
          </div>
          <div>
            <dt>Intune</dt>
            <dd>{textField(intune, ["deviceName", "serialNumber", "complianceState"])}</dd>
          </div>
          <div>
            <dt>Synced</dt>
            <dd>{sync ? `${formatShortDateTime(sync.last_checked_at)} / AD ${String(sync.ad_count)} / Entra ${String(sync.entra_count)} / Intune ${String(sync.intune_count)}` : "-"}</dd>
          </div>
        </dl>
      </Panel>

      <Panel title="Timeline">
        {timeline.length ? (
          <ol className="evidence-timeline">
            {timeline.map((event, index) => (
              <TimelineRow key={`${event.at}-${event.source}-${event.type}-${String(index)}`} event={event} />
            ))}
          </ol>
        ) : (
          <div className="empty evidence-empty">
            <Camera aria-hidden="true" focusable="false" size={18} strokeWidth={2.4} />
            <span>No timeline events yet.</span>
          </div>
        )}
      </Panel>
    </section>
  );
}
