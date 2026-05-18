import { afterEach, describe, expect, test, vi } from "vitest";

import { fetchJson } from "./apiClient";

describe("fetchJson", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("requests same-origin JSON and returns the typed body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ sha_short: "abc1234" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const body = await fetchJson<{ sha_short: string }>("/api/version");

    expect(body.sha_short).toBe("abc1234");
    const call = fetchMock.mock.calls[0];
    expect(call?.[0]).toBe("/api/version");
    const init = call?.[1];
    expect(init?.credentials).toBe("same-origin");
    expect(new Headers(init?.headers).get("accept")).toBe("application/json");
  });

  test("throws a useful error for non-2xx responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "authentication required" }), {
        status: 401,
        statusText: "Unauthorized",
        headers: { "content-type": "application/json" }
      })
    );

    await expect(fetchJson("/api/version")).rejects.toThrow(
      "GET /api/version failed: authentication required"
    );
  });
});
