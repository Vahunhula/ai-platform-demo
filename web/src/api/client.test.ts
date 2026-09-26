// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("bounded Activity API client", () => {
  it("loads a 50-event server page with an exclusive older cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [],
          order: "desc",
          limit: 50,
          has_more: false,
          next_before_sequence: null,
          next_after_sequence: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.getActivityPage("TASK / 1", 1042);

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/tasks/TASK%20%2F%201/events/page?order=desc&limit=50&before_sequence=1042",
    );
  });
});
