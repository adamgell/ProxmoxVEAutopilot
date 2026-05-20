import { useCallback, useMemo, useRef, useState } from "react";

import { postForm, fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, HashFileRow, HashesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, lowerText, textValue } from "../utilityModels";

function hashMatches(row: HashFileRow, query: string): boolean {
  if (!query) {
    return true;
  }
  return [row.filename, row.serial, row.name, row.group_tag].some((value) => lowerText(value).includes(query));
}

export function HashesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [rows, setRows] = useState<readonly HashFileRow[]>([]);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<readonly string[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<HashesResponse>("/api/hashes");
      setRows(payload.hash_files);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load hashes");
    }
  }, []);

  usePolling(load);

  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => hashMatches(row, query)), [query, rows]);
  const inIntune = rows.filter((row) => row.in_intune).length;

  const uploadFiles = async () => {
    const files = fileInput.current?.files;
    if (!files?.length) {
      return;
    }
    const form = new FormData();
    Array.from(files).forEach((file) => {
      form.append("files", file);
    });
    try {
      const result = await postForm<{ readonly uploaded?: number }>("/api/hashes/upload", form);
      setMessage(`Uploaded ${String(result.uploaded ?? files.length)} hash file(s)`);
      if (fileInput.current) {
        fileInput.current.value = "";
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Hash upload failed");
    }
  };

  const deleteSelected = async () => {
    if (!selected.length || !window.confirm(`Delete ${String(selected.length)} hash file(s)?`)) {
      return;
    }
    const form = new FormData();
    selected.forEach((filename) => {
      form.append("files", filename);
    });
    try {
      const result = await postForm<{ readonly deleted?: number }>("/api/hashes/delete", form);
      setMessage(`Deleted ${String(result.deleted ?? selected.length)} hash file(s)`);
      setSelected([]);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Hash delete failed");
    }
  };

  const uploadSelectedToIntune = async () => {
    if (!selected.length) {
      return;
    }
    const form = new FormData();
    selected.forEach((filename) => {
      form.append("files", filename);
      const row = rows.find((item) => item.filename === filename);
      form.append("group_tags", textValue(row?.group_tag, ""));
    });
    try {
      const result = await postForm<{ readonly count?: number }>("/api/jobs/upload", form);
      setMessage(`Queued ${String(result.count ?? selected.length)} Intune upload job(s)`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Intune upload failed");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Hashes"
      section="Fleet"
      path="/react/hashes"
      action={<a className="action-link" href="/legacy/hashes">Legacy</a>}
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Hash metrics">
        <Metric label="Files" value={String(rows.length)} />
        <Metric label="In Intune" value={String(inIntune)} tone={inIntune ? "good" : "neutral"} />
        <Metric label="Selected" value={String(selected.length)} tone={selected.length ? "active" : "neutral"} />
        <Metric label="Missing" value={String(rows.length - inIntune)} tone={rows.length - inIntune ? "active" : "neutral"} />
      </section>
      <section className="filter-row" aria-label="Hash controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search hashes</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="File, serial, tag" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
        </div>
        <div className="utility-upload-row">
          <input ref={fileInput} type="file" accept=".csv,text/csv" multiple aria-label="Upload hash CSV files" />
          <button className="utility-button" type="button" onClick={() => { void uploadFiles(); }}>Upload CSV</button>
          <button className="utility-button" type="button" onClick={() => { void uploadSelectedToIntune(); }} disabled={!selected.length}>Upload to Intune</button>
          <button className="utility-button utility-button--danger" type="button" onClick={() => { void deleteSelected(); }} disabled={!selected.length}>Delete</button>
        </div>
      </section>
      <Panel title="Hash files">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">Select</th>
                <th scope="col">File</th>
                <th scope="col">Serial</th>
                <th scope="col">Group tag</th>
                <th scope="col">Intune</th>
                <th scope="col">Size</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.filename}>
                  <td>
                    <input
                      aria-label={`Select ${row.filename}`}
                      type="checkbox"
                      checked={selected.includes(row.filename)}
                      onChange={(event) => {
                        setSelected((current) => event.target.checked
                          ? [...current, row.filename]
                          : current.filter((filename) => filename !== row.filename));
                      }}
                    />
                  </td>
                  <td><a href={`/api/hashes/${encodeURIComponent(row.filename)}`}>{row.filename}</a></td>
                  <td>{textValue(row.serial)}</td>
                  <td>{textValue(row.group_tag)}</td>
                  <td>{row.in_intune ? "Yes" : "No"}</td>
                  <td>{bytesLabel(row.size)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No hash files.</p> : null}
      </Panel>
    </PageFrame>
  );
}
