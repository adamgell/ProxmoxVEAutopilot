import type { AppBootstrap } from "./contracts";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { ShellIndexPage } from "./pages/ShellIndexPage";
import { VmsPage } from "./pages/VmsPage";

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
  if (path === "/react/vms" || /^\/react\/vms\/\d+$/.test(path)) {
    return <VmsPage bootstrap={bootstrap} />;
  }
  return <ShellIndexPage bootstrap={bootstrap} />;
}
