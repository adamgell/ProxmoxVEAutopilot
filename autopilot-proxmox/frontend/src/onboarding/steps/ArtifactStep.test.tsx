import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { ArtifactStep } from "./ArtifactStep";
import { initialState } from "../types";

afterEach(cleanup);

function artifactState(overrides?: {
  source?: "existing" | "build";
  buildJobId?: string | null;
}) {
  const base = initialState();
  return {
    ...base,
    answers: {
      ...base.answers,
      artifact: {
        ...base.answers.artifact,
        source: overrides?.source ?? "existing",
        buildJobId: overrides?.buildJobId ?? null,
      },
    },
  };
}

function probeResponse(body: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ArtifactStep inventory fetch", () => {
  beforeEach(() => {
    (global as Record<string, unknown>).fetch = undefined;
  });

  it("renders inventory once the probe resolves", async () => {
    global.fetch = vi.fn(async () =>
      probeResponse({
        ok: true,
        detail: "1 CloudOSD, 0 OSDeploy",
        cloudosd: [{ id: "cosd-1", label: "CloudOSD 2026-05", built_at: "2026-05-20T10:00Z" }],
        osdeploy: [],
      }),
    ) as typeof fetch;
    const onPatch = vi.fn();
    render(<ArtifactStep state={artifactState()} onPatch={onPatch} />);

    await waitFor(() =>
      expect(screen.getByRole("option", { name: /CloudOSD 2026-05/ })).toBeInTheDocument(),
    );
  });

  it("inventory fetch failure renders an error and exits the loading state", async () => {
    global.fetch = vi.fn(async () => {
      throw new Error("network down");
    }) as typeof fetch;
    const onPatch = vi.fn();
    render(<ArtifactStep state={artifactState()} onPatch={onPatch} />);

    await waitFor(() => expect(screen.queryByText(/Loading artifact inventory/)).not.toBeInTheDocument());
    expect(screen.getByText(/Could not load artifact inventory.*network down/)).toBeInTheDocument();
  });

  it("non-2xx inventory response surfaces an error", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 503 })) as typeof fetch;
    const onPatch = vi.fn();
    render(<ArtifactStep state={artifactState()} onPatch={onPatch} />);

    await waitFor(() => expect(screen.queryByText(/Loading artifact inventory/)).not.toBeInTheDocument());
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
  });
});

describe("ArtifactStep build button", () => {
  beforeEach(() => {
    (global as Record<string, unknown>).fetch = undefined;
  });

  it("non-2xx build response surfaces an error and re-enables the button", async () => {
    // First call: inventory probe (empty lists). Second call: build POST (502).
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(probeResponse({ ok: false, detail: "empty", cloudosd: [], osdeploy: [] }))
      .mockResolvedValueOnce(new Response("preflight failed", { status: 502 }));
    global.fetch = fetchMock as unknown as typeof fetch;
    const onPatch = vi.fn();
    render(<ArtifactStep state={artifactState({ source: "build" })} onPatch={onPatch} />);

    await waitFor(() => expect(screen.getByRole("button", { name: /Kick a build/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Kick a build/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Kick a build/i })).toBeEnabled());
    expect(screen.getByText(/Build request failed.*HTTP 502/)).toBeInTheDocument();
    // onPatch should NOT have been called with a buildJobId on failure.
    const patchCalls = onPatch.mock.calls.map((c) => c[0]);
    expect(patchCalls.some((p) => p?.artifact?.buildJobId)).toBe(false);
  });

  it("happy path stores job_id via onPatch", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(probeResponse({ ok: false, detail: "empty", cloudosd: [], osdeploy: [] }))
      .mockResolvedValueOnce(probeResponse({ ok: true, job_id: "job-123" }, { status: 202 }));
    global.fetch = fetchMock as unknown as typeof fetch;
    const onPatch = vi.fn();
    render(<ArtifactStep state={artifactState({ source: "build" })} onPatch={onPatch} />);

    await waitFor(() => expect(screen.getByRole("button", { name: /Kick a build/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Kick a build/i }));

    await waitFor(() => {
      expect(onPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          artifact: expect.objectContaining({ buildJobId: "job-123" }),
        }),
      );
    });
  });
});

describe("ArtifactStep build resume", () => {
  beforeEach(() => {
    (global as Record<string, unknown>).fetch = undefined;
  });

  it("missing build job clears buildJobId and surfaces an alert", async () => {
    // First call: inventory. Second call: /api/jobs/<id> returns 200 with error=not found.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(probeResponse({ ok: false, detail: "empty", cloudosd: [], osdeploy: [] }))
      .mockResolvedValueOnce(probeResponse({ error: "not found" }));
    global.fetch = fetchMock as unknown as typeof fetch;
    const onPatch = vi.fn();
    render(
      <ArtifactStep state={artifactState({ source: "build", buildJobId: "pruned-job" })} onPatch={onPatch} />,
    );

    await waitFor(() => {
      expect(onPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          artifact: expect.objectContaining({ buildJobId: null }),
        }),
      );
    });
    expect(screen.getByText(/The previous build job is gone/)).toBeInTheDocument();
  });
});
