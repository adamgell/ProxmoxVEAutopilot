import type { AppBootstrap } from "./contracts";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { ShellIndexPage } from "./pages/ShellIndexPage";

interface AppProps {
  readonly bootstrap: AppBootstrap;
}

export function App({ bootstrap }: AppProps) {
  const path = window.location.pathname;
  if (path === "/react/dashboard") {
    return <DashboardPage bootstrap={bootstrap} />;
  }
  if (path === "/react/jobs") {
    return <JobsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/monitoring") {
    return <MonitoringPage bootstrap={bootstrap} />;
  }
  return <ShellIndexPage bootstrap={bootstrap} />;
}
