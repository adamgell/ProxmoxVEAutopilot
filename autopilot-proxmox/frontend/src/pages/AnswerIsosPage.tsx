import { Trash2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime } from "../viewModels";

interface AnswerIsoRow {
  readonly hash: string;
  readonly short_hash: string;
  readonly volid: string;
  readonly compiled_at?: string | null;
  readonly last_used_at?: string | null;
  readonly in_use: boolean;
}

interface AnswerIsosPayload {
  readonly rows: readonly AnswerIsoRow[];
  readonly error?: string;
}

interface PruneResponse {
  readonly removed?: readonly string[];
  readonly error?: string;
}

function defaultPayload(): AnswerIsosPayload {
  return {
    rows: [],
    error: ""
  };
}

export function AnswerIsosPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<AnswerIsosPayload>(defaultPayload);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    try {
      const nextPayload = await fetchJson<AnswerIsosPayload>("/api/answer-isos/page");
      setPayload(nextPayload);
      setError(textValue(nextPayload.error, ""));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load answer ISO cache");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const selectableRows = useMemo(() => payload.rows.filter((row) => !row.in_use), [payload.rows]);
  const allSelected = selectableRows.length > 0 && selectableRows.every((row) => selected.has(row.hash));

  const toggleHash = (hash: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(hash)) {
        next.delete(hash);
      } else {
        next.add(hash);
      }
      return next;
    });
  };

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableRows.map((row) => row.hash)));
  };

  const pruneSelected = async () => {
    const hashes = Array.from(selected);
    if (!hashes.length) {
      setStatus("select unused rows first");
      return;
    }
    setPending(true);
    try {
      const response = await postJson<PruneResponse>("/api/answer-isos/prune", { hashes });
      if (response.error) {
        setStatus(response.error);
      } else {
        setStatus(`removed ${String(response.removed?.length ?? hashes.length)}`);
        setSelected(new Set());
        await load();
      }
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Prune failed");
    } finally {
      setPending(false);
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title="Answer ISO Cache" section="Build" path="/react/answer-isos">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading Answer ISOs</span>
          <div className="load-strip__track" role="progressbar" aria-label="Answer ISO cache loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {status ? <p className="notice" role="status">{status}</p> : null}

      <section className="metric-strip" aria-label="Answer ISO metrics">
        <Metric label="Cached" value={String(payload.rows.length)} />
        <Metric label="In use" value={String(payload.rows.filter((row) => row.in_use).length)} />
        <Metric label="Unused" value={String(selectableRows.length)} />
      </section>

      <Panel
        title="Cached Floppies"
        action={(
          <button className="utility-button utility-button--danger" type="button" onClick={() => { void pruneSelected(); }} disabled={pending || !selected.size}>
            <Trash2 size={15} aria-hidden="true" /> Prune selected
          </button>
        )}
      >
        {payload.rows.length ? (
          <div className="table-wrap">
            <table className="jobs-table utility-table" aria-label="Answer ISO cache">
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label="Select all unused answer ISOs"
                      checked={allSelected}
                      disabled={!selectableRows.length}
                      onChange={toggleAll}
                    />
                  </th>
                  <th scope="col">Short hash</th>
                  <th scope="col">Path on Proxmox host</th>
                  <th scope="col">Compiled</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {payload.rows.map((row) => (
                  <tr key={row.hash}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${row.short_hash}`}
                        checked={selected.has(row.hash)}
                        disabled={row.in_use}
                        title={row.in_use ? "in use by a VM" : "select for prune"}
                        onChange={() => {
                          toggleHash(row.hash);
                        }}
                      />
                    </td>
                    <td><code>{row.short_hash}</code></td>
                    <td><code>{row.volid}</code></td>
                    <td>{formatShortDateTime(row.compiled_at)}</td>
                    <td>{formatShortDateTime(row.last_used_at)}</td>
                    <td><span className={row.in_use ? "status status--good" : "status status--neutral"}>{row.in_use ? "in use" : "unused"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty">Cache is empty.</p>
        )}
      </Panel>
    </PageFrame>
  );
}
