import { useEffect, useState } from "react";
import { api } from "./api";
import { useHashRoute, useInterval } from "./hooks";
import type { Health } from "./types";
import Documents from "./pages/Documents";
import DocumentDetail from "./pages/DocumentDetail";
import Intake from "./pages/Intake";
import Search from "./pages/Search";

const NAV = [
  { to: "/intake", label: "Intake" },
  { to: "/documents", label: "Documents" },
  { to: "/search", label: "Search" },
];

export default function App() {
  const [route, go] = useHashRoute();
  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  const poll = () => api.health().then(setHealth).catch(() => setHealth(null));
  useEffect(() => { void poll(); }, []);
  useInterval(() => void poll(), 15000);

  const detail = route.match(/^\/documents\/([A-Za-z0-9]+)$/);
  let page = <Intake go={go} />;
  if (detail) page = <DocumentDetail id={detail[1]} go={go} />;
  else if (route.startsWith("/documents")) page = <Documents go={go} />;
  else if (route.startsWith("/search")) page = <Search go={go} />;

  return (
    <div className="shell">
      <nav className="rail" aria-label="Main">
        <a className="brand" href="#/intake">
          <span className="brand-mark" aria-hidden="true" />
          <span><strong>Invoice Desk</strong><span className="muted small">document intelligence</span></span>
        </a>
        <ul>
          {NAV.map((n) => (
            <li key={n.to}>
              <a href={`#${n.to}`} aria-current={route.startsWith(n.to) ? "page" : undefined}>{n.label}</a>
            </li>
          ))}
        </ul>
        <div className="rail-foot small">
          <div className={health ? (health.status === "ok" ? "ok-text" : "warn-text") : "err-text"}>
            {health === undefined ? "Checking server..." : health === null ? "Server not reachable" : health.status === "ok" ? "Server online" : "Server degraded"}
          </div>
          {health && <div className="muted">{health.extractor === "llm" ? `AI extraction: ${health.model}` : "Rules only, no AI key set"}</div>}
        </div>
      </nav>
      <main className="content">{page}</main>
    </div>
  );
}
