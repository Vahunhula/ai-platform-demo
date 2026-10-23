// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { IntegrationsPage } from "./IntegrationsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shows compact secret-free GitHub and Jira connection metadata", async () => {
  vi.spyOn(api, "listIntegrations").mockResolvedValue([
    {
      provider: "GITHUB",
      configured: true,
      status: "CONNECTED",
      display_name: "Butopea",
      account: { id: "42", name: "Butopea" },
      site: null,
      metadata: { installation_id: 123456, repository_count: 12 },
      last_checked_at: "2026-10-23T10:00:00Z",
      safe_error: null,
    },
    {
      provider: "JIRA",
      configured: true,
      status: "CONNECTED",
      display_name: "Company Jira",
      account: { id: "acct", name: "Alex" },
      site: { id: "cloud", name: "Company Jira", url: "https://company.atlassian.net" },
      metadata: { scopes: ["read:me", "read:jira-user", "offline_access"] },
      last_checked_at: "2026-10-23T10:00:00Z",
      safe_error: null,
    },
  ]);

  render(<IntegrationsPage jiraSelectionId={null} />);

  await waitFor(() => expect(screen.getByText("Butopea")).toBeTruthy());
  expect(screen.getByText("GitHub App installation #123456")).toBeTruthy();
  expect(screen.getByText("12 repositories accessible")).toBeTruthy();
  expect(screen.getByText("https://company.atlassian.net")).toBeTruthy();
  expect(screen.getByText("Connected as Alex")).toBeTruthy();
  expect(document.body.textContent).not.toContain("SUPER_SECRET");
});

it("shows not configured without trying to connect", async () => {
  vi.spyOn(api, "listIntegrations").mockResolvedValue([
    {
      provider: "GITHUB",
      configured: false,
      status: "NOT_CONFIGURED",
      display_name: null,
      account: null,
      site: null,
      metadata: {},
      last_checked_at: null,
      safe_error: null,
    },
  ]);

  render(<IntegrationsPage jiraSelectionId={null} />);

  await waitFor(() => expect(screen.getByText("not configured")).toBeTruthy());
  expect((screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement).disabled).toBe(true);
});
