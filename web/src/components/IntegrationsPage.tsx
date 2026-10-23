import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { IntegrationConnection, JiraSite } from "../types/api";
import { formatRelative } from "./format";

interface Props {
  jiraSelectionId: string | null;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function providerPath(provider: IntegrationConnection["provider"]): "github" | "jira" {
  return provider === "GITHUB" ? "github" : "jira";
}

export function IntegrationsPage({ jiraSelectionId }: Props) {
  const [connections, setConnections] = useState<IntegrationConnection[]>([]);
  const [sites, setSites] = useState<JiraSite[]>([]);
  const [selectedSite, setSelectedSite] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setConnections(await api.listIntegrations(signal));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).catch((reason: unknown) => setError(errorMessage(reason)));
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!jiraSelectionId) return;
    const controller = new AbortController();
    api
      .getJiraSites(jiraSelectionId, controller.signal)
      .then((result) => {
        setSites(result.sites);
        setSelectedSite(result.sites[0]?.id ?? "");
      })
      .catch((reason: unknown) => setError(errorMessage(reason)));
    return () => controller.abort();
  }, [jiraSelectionId]);

  const connect = async (provider: "github" | "jira") => {
    setBusy(`${provider}-connect`);
    setError(null);
    try {
      const { authorization_url: authorizationUrl } = await api.startIntegration(provider);
      window.location.assign(authorizationUrl);
    } catch (reason) {
      setError(errorMessage(reason));
      setBusy(null);
    }
  };

  const check = async (provider: "github" | "jira") => {
    setBusy(`${provider}-check`);
    setError(null);
    try {
      await api.checkIntegration(provider);
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (provider: "github" | "jira") => {
    if (!window.confirm(`Disconnect ${provider === "github" ? "GitHub" : "Jira Cloud"}?`)) return;
    setBusy(`${provider}-disconnect`);
    setError(null);
    try {
      await api.disconnectIntegration(provider);
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const chooseSite = async () => {
    if (!jiraSelectionId || !selectedSite) return;
    setBusy("jira-site");
    setError(null);
    try {
      await api.selectJiraSite(jiraSelectionId, selectedSite);
      setSites([]);
      window.history.replaceState({}, "", "/?settings=integrations&jira=connected");
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="integrations-page">
      <header className="settings-header">
        <span className="task-kicker">Settings</span>
        <h1>Integrations</h1>
        <p>Connect external services. Credentials remain encrypted and server-side.</p>
      </header>
      {error && <div className="error-banner">{error}</div>}
      {sites.length > 1 && (
        <section className="site-selector" aria-label="Choose Jira site">
          <div>
            <strong>Choose a Jira Cloud site</strong>
            <p>This authorization can access multiple sites. Select the one to connect.</p>
          </div>
          <select value={selectedSite} onChange={(event) => setSelectedSite(event.target.value)}>
            {sites.map((site) => <option key={site.id} value={site.id}>{site.name} · {site.url}</option>)}
          </select>
          <button disabled={busy !== null || !selectedSite} onClick={() => void chooseSite()}>Connect site</button>
        </section>
      )}
      <div className="integration-grid">
        {connections.map((connection) => {
          const provider = providerPath(connection.provider);
          const connected = ["CONNECTED", "DEGRADED", "REAUTH_REQUIRED"].includes(connection.status);
          return (
            <article className="integration-card" key={connection.provider}>
              <div className="integration-heading">
                <div>
                  <span className={`provider-mark provider-${provider}`}>{connection.provider === "GITHUB" ? "GH" : "JI"}</span>
                  <h2>{connection.provider === "GITHUB" ? "GitHub" : "Jira Cloud"}</h2>
                </div>
                <span className={`connection-status connection-${connection.status.toLowerCase()}`}>
                  <i /> {connection.status.replaceAll("_", " ").toLowerCase()}
                </span>
              </div>
              <div className="integration-details">
                {connection.provider === "GITHUB" ? (
                  <>
                    <strong>{connection.account?.name ?? "No GitHub account connected"}</strong>
                    {connection.metadata.installation_id && <span>GitHub App installation #{connection.metadata.installation_id}</span>}
                    {connection.metadata.repository_count !== undefined && <span>{connection.metadata.repository_count} repositories accessible</span>}
                  </>
                ) : (
                  <>
                    <strong>{connection.site?.url ?? "No Jira site connected"}</strong>
                    {connection.account?.name && <span>Connected as {connection.account.name}</span>}
                    {connection.metadata.scopes && <span>Scopes: {connection.metadata.scopes.join(", ")}</span>}
                  </>
                )}
                <span>Last checked: {connection.last_checked_at ? formatRelative(connection.last_checked_at) : "Never"}</span>
                {connection.safe_error && <p className="integration-error">{connection.safe_error}</p>}
                {!connection.configured && <p className="integration-note">Server configuration is required before connecting.</p>}
              </div>
              <div className="integration-actions">
                {!connected && connection.status !== "DISCONNECTED" ? (
                  <button disabled={!connection.configured || busy !== null} onClick={() => void connect(provider)}>Connect</button>
                ) : (
                  <>
                    {connected && <button disabled={busy !== null} onClick={() => void check(provider)}>Check connection</button>}
                    <button disabled={!connection.configured || busy !== null} onClick={() => void connect(provider)}>Reconnect</button>
                    <button className="danger-subtle" disabled={busy !== null} onClick={() => void disconnect(provider)}>Disconnect</button>
                  </>
                )}
              </div>
              {connection.provider === "GITHUB" && connection.status === "DISCONNECTED" && (
                <p className="integration-footnote">Disconnect removes the local association; it does not uninstall the GitHub App.</p>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
