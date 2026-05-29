import { useCallback, useState } from "react";

import { ApiError, fetchJson, postJson } from "../apiClient";
import { usePolling } from "../hooks/usePolling";
import { Panel } from "./ui";

interface OsDeployCacheEntry {
  readonly id: string;
  readonly entry_type: string;
  readonly windows_version: string;
  readonly edition: string | null;
  readonly status: string;
  readonly size_bytes: number | null;
  readonly source_url: string | null;
  readonly error: string | null;
  readonly metadata?: Readonly<Record<string, unknown>>;
}

interface OsDeployCachePayload {
  readonly entries?: readonly OsDeployCacheEntry[];
}

// Mirrors web/osdeploy_endpoints.py _entry_is_factory_built: only server_image
// entries with a manual:// placeholder produced by the OSDeploy/OSDBuilder factory
// are warmed by dispatching a build to the build-host agent.
function isFactoryBuilt(entry: OsDeployCacheEntry): boolean {
  if (entry.entry_type !== "server_image") {
    return false;
  }
  if (!(entry.source_url ?? "").startsWith("manual://")) {
    return false;
  }
  const factory = entry.metadata?.factory;
  const factoryText = typeof factory === "string" ? factory.toLowerCase() : "";
  return factoryText.includes("osdbuilder") || factoryText.includes("osdeploy");
}

function formatSize(bytes: number | null): string {
  if (!bytes || bytes <= 0) {
    return "-";
  }
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

export function OsDeployCachePanel() {
  const [entries, setEntries] = useState<readonly OsDeployCacheEntry[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyId, setBusyId] = useState("");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<OsDeployCachePayload>("/api/osdeploy/v1/cache");
      setEntries(payload.entries ?? []);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load OSDeploy cache");
    }
  }, []);

  usePolling(load);

  const warm = useCallback(async (entry: OsDeployCacheEntry) => {
    const label = `${entry.windows_version} ${entry.edition ?? ""}`.trim();
    setBusyId(entry.id);
    setNotice("");
    setError("");
    try {
      await postJson(`/api/osdeploy/v1/cache/${entry.id}/warm`);
      setNotice(`Build dispatched to the build-host agent for ${label}.`);
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("Build-host agent is not ready. Start the build host (VM 100) and retry.");
      } else {
        setError(err instanceof Error ? err.message : "Warm failed");
      }
    } finally {
      setBusyId("");
    }
  }, [load]);

  return (
    <Panel
      title="Image cache"
      action={(
        <button type="button" className="action-link" onClick={() => { void load(); }}>
          Refresh
        </button>
      )}
    >
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {notice ? <p className="notice notice--good" role="status">{notice}</p> : null}
      {entries.length === 0 ? (
        <p className="empty">No cache entries yet. Refresh the catalog to seed server images.</p>
      ) : (
        <div className="table-wrap">
          <table className="jobs-table" aria-label="OSDeploy image cache">
            <thead>
              <tr>
                <th scope="col">Image</th>
                <th scope="col">Status</th>
                <th scope="col">Size</th>
                <th scope="col" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const label = `${entry.windows_version} ${entry.edition ?? ""}`.trim();
                const dispatching = busyId === entry.id;
                return (
                  <tr key={entry.id}>
                    <td>{label}</td>
                    <td>{entry.status}{entry.error ? ` (${entry.error})` : ""}</td>
                    <td>{formatSize(entry.size_bytes)}</td>
                    <td>
                      {isFactoryBuilt(entry) ? (
                        <button
                          type="button"
                          className="action-link"
                          disabled={dispatching || entry.status === "warming"}
                          onClick={() => { void warm(entry); }}
                        >
                          {entry.status === "warming" ? "Warming..." : dispatching ? "Dispatching..." : "Warm"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
