import { useCallback, useMemo, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, FileShelfRow, FilesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, lowerText, textValue } from "../utilityModels";

function fileDownloadUrl(row: FileShelfRow): string {
  return row.url || `/files/${encodeURIComponent(row.name)}`;
}

export function FilesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const replaceInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const [rows, setRows] = useState<readonly FileShelfRow[]>([]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingAction, setPendingAction] = useState("");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<FilesResponse>("/api/files");
      setRows(payload.files);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load files");
    }
  }, []);

  usePolling(load);

  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => lowerText(row.name).includes(query)), [query, rows]);
  const totalBytes = rows.reduce((sum, row) => sum + (typeof row.size_bytes === "number" ? row.size_bytes : 0), 0);

  const uploadFiles = async () => {
    const files = fileInput.current?.files;
    if (!files?.length) {
      return;
    }
    const form = new FormData();
    Array.from(files).forEach((file) => {
      form.append("files", file);
    });
    setPendingAction("upload");
    try {
      const result = await postForm<{ readonly uploaded?: number }>("/api/files/upload", form);
      setMessage(`Uploaded or replaced ${String(result.uploaded ?? files.length)} MSI file(s)`);
      setError("");
      if (fileInput.current) {
        fileInput.current.value = "";
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed");
    } finally {
      setPendingAction("");
    }
  };

  const replaceFile = async (name: string) => {
    const input = replaceInputs.current[name];
    const file = input?.files?.[0];
    if (!file) {
      setError(`Choose a replacement MSI for ${name}`);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    setPendingAction(`replace:${name}`);
    try {
      const result = await postForm<{ readonly replaced?: string }>(`/api/files/${encodeURIComponent(name)}/replace`, form);
      setMessage(`Replaced ${result.replaced ?? name}`);
      setError("");
      input.value = "";
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Replace failed for ${name}`);
    } finally {
      setPendingAction("");
    }
  };

  const deleteFile = async (name: string) => {
    if (!window.confirm(`Delete ${name}?`)) {
      return;
    }
    const form = new FormData();
    form.append("files", name);
    setPendingAction(`delete:${name}`);
    try {
      const result = await postForm<{ readonly deleted?: number }>("/api/files/delete", form);
      setMessage(`Deleted ${String(result.deleted ?? 1)} MSI file(s)`);
      setError("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Delete failed for ${name}`);
    } finally {
      setPendingAction("");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Files"
      section="Fleet"
      path="/react/files"
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="File shelf metrics">
        <Metric label="Files" value={String(rows.length)} />
        <Metric label="Storage" value={bytesLabel(totalBytes)} />
        <Metric label="Visible" value={String(filtered.length)} />
        <Metric label="Type" value="MSI" />
      </section>
      <section className="filter-row" aria-label="File shelf controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search files</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="MSI filename" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
        </div>
        <div className="utility-upload-row">
          <input ref={fileInput} type="file" accept=".msi,application/octet-stream" multiple aria-label="Upload MSI files" />
          <button
            className="utility-button"
            type="button"
            disabled={pendingAction === "upload"}
            onClick={() => { void uploadFiles(); }}
          >
            Upload / Replace MSI
          </button>
        </div>
      </section>
      <Panel title="MSI files">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">URL</th>
                <th scope="col">Size</th>
                <th scope="col">Modified</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.name}>
                  <td><a href={fileDownloadUrl(row)}>{row.name}</a></td>
                  <td>
                    <a className="mono-link" href={fileDownloadUrl(row)}>{fileDownloadUrl(row)}</a>
                  </td>
                  <td>{bytesLabel(row.size_bytes)}</td>
                  <td>{textValue(row.modified)}</td>
                  <td>
                    <div className="utility-action-stack">
                      <input
                        ref={(input) => {
                          replaceInputs.current[row.name] = input;
                        }}
                        type="file"
                        accept=".msi,application/octet-stream"
                        aria-label={`Replacement MSI for ${row.name}`}
                      />
                      <div className="utility-action-row">
                        <button
                          className="utility-button"
                          type="button"
                          disabled={pendingAction === `replace:${row.name}`}
                          onClick={() => { void replaceFile(row.name); }}
                        >
                          Replace
                        </button>
                        <button
                          className="utility-button utility-button--danger"
                          type="button"
                          disabled={pendingAction === `delete:${row.name}`}
                          onClick={() => { void deleteFile(row.name); }}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No MSI files.</p> : null}
      </Panel>
    </PageFrame>
  );
}
