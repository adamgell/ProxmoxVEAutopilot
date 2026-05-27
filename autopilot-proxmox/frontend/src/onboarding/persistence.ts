const STATE_URL = "/api/onboarding/state";

// Wire-shape conversion. Backend uses snake_case in the JSON answers blob;
// TS types in onboarding/types.ts use camelCase. Walk one level deep.
const SNAKE_TO_CAMEL: Record<string, Record<string, string>> = {
  identity: {
    ad_domain: "adDomain",
    ad_join_account: "adJoinAccount",
    ad_join_password_ref: "adJoinPasswordRef",
    local_admin_password_ref: "localAdminPasswordRef",
  },
  tenant: { tenant_id: "tenantId", tenant_domain: "tenantDomain", comment_file: "commentFile" },
  artifact: { existing_artifact_id: "existingArtifactId", build_job_id: "buildJobId" },
  trial: { vm_name: "vmName", target_node: "targetNode", os_edition: "osEdition" },
};
const CAMEL_TO_SNAKE: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(SNAKE_TO_CAMEL).map(([group, map]) => [
    group,
    Object.fromEntries(Object.entries(map).map(([s, c]) => [c, s])),
  ]),
);

function convert(answers: any, table: Record<string, Record<string, string>>): any {
  if (!answers || typeof answers !== "object") return answers;
  const out: any = { ...answers };
  for (const [group, map] of Object.entries(table)) {
    if (!out[group]) continue;
    const next: any = { ...out[group] };
    for (const [from, to] of Object.entries(map)) {
      if (from in next) {
        next[to] = next[from];
        delete next[from];
      }
    }
    out[group] = next;
  }
  // Top-level camel/snake for a few fields used outside `answers`.
  if ("schema_version" in out) { out.schemaVersion = out.schema_version; delete out.schema_version; }
  if ("probe_results" in out) { out.probeResults = out.probe_results; delete out.probe_results; }
  return out;
}

export function fromWire(raw: any): any {
  return convert(raw, SNAKE_TO_CAMEL);
}

export function toWire(answers: any): any {
  // Mirror image. Used by callers building the PUT body.
  const flipped = { ...answers };
  if ("schemaVersion" in flipped) { flipped.schema_version = flipped.schemaVersion; delete flipped.schemaVersion; }
  if ("probeResults" in flipped) { flipped.probe_results = flipped.probeResults; delete flipped.probeResults; }
  return convert(flipped, CAMEL_TO_SNAKE);
}

export class PreconditionFailedError extends Error {
  constructor(message = "stale ETag") {
    super(message);
    this.name = "PreconditionFailedError";
  }
}

export class PreconditionRequiredError extends Error {
  constructor(message = "If-Match required") {
    super(message);
    this.name = "PreconditionRequiredError";
  }
}

async function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function fetchState(): Promise<{ row: any; etag: string } | null> {
  const r = await fetch(STATE_URL, { credentials: "include" });
  if (r.status === 404) {
    return null;
  }
  if (r.status === 401) {
    window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error("re-auth required");
  }
  if (!r.ok) {
    throw new Error(`fetchState: ${r.status}`);
  }
  const raw = await r.json();
  return { row: { ...raw, answers: fromWire(raw.answers ?? {}) }, etag: r.headers.get("ETag") ?? "" };
}

export async function putState(
  body: { patch: Record<string, unknown> },
  ifMatch: string | null,
): Promise<{ row: any; etag: string }> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (ifMatch) {
    headers["If-Match"] = ifMatch;
  }
  // Convert the camelCase patch to the snake_case wire shape before sending.
  // Top-level keys in the patch (current_step, status, persona, launched_run_id, answers) are
  // already wire-shaped; only `answers` itself needs key conversion.
  const wireBody = { ...body };
  if (body.patch && typeof body.patch === "object" && "answers" in body.patch) {
    wireBody.patch = { ...body.patch, answers: toWire((body.patch as any).answers) };
  }
  const backoff = [0, 1000, 3000, 9000];
  let lastError: Error | null = null;
  for (const ms of backoff) {
    if (ms > 0) {
      await delay(ms);
    }
    const r = await fetch(STATE_URL, {
      method: "PUT",
      credentials: "include",
      headers,
      body: JSON.stringify(wireBody),
    });
    if (r.status === 401) {
      window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw new Error("re-auth required");
    }
    if (r.status === 409) {
      throw new PreconditionFailedError();
    }
    if (r.status === 428) {
      throw new PreconditionRequiredError();
    }
    if (r.ok) {
      const raw = await r.json();
      return { row: { ...raw, answers: fromWire(raw.answers ?? {}) }, etag: r.headers.get("ETag") ?? "" };
    }
    if (r.status >= 500) {
      lastError = new Error(`putState: ${r.status}`);
      continue;
    }
    throw new Error(`putState: ${r.status}`);
  }
  throw lastError ?? new Error("putState exhausted retries");
}

export async function deleteState(): Promise<void> {
  const r = await fetch(STATE_URL, { method: "DELETE", credentials: "include" });
  if (r.status === 401) {
    window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error("re-auth required");
  }
  if (!r.ok && r.status !== 204) {
    throw new Error(`deleteState: ${r.status}`);
  }
}
