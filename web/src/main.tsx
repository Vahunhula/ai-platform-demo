import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { api, setUnauthorizedHandler } from "./api/client";
import { Login } from "./components/Login";
import type { CurrentUser } from "./types/api";
import "./styles.css";

/** Resolves the session cookie to a user; any 401 later returns here to the login screen. */
function Root() {
  const [user, setUser] = useState<CurrentUser | null | undefined>(undefined);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  if (user === undefined) return <div className="empty-state">Loading…</div>;
  if (user === null) return <Login onSignedIn={setUser} />;
  return (
    <App
      key={user.id}
      user={user}
      onSignOut={() => {
        void api.logout().finally(() => setUser(null));
      }}
    />
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
