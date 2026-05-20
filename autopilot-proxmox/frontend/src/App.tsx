import type { AppBootstrap } from "./contracts";
import { AgentDownloadPage } from "./pages/AgentDownloadPage";
import { ClassicVmsPage } from "./pages/ClassicVmsPage";
import { CloudDevicesPage } from "./pages/CloudDevicesPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { FilesPage } from "./pages/FilesPage";
import { HashesPage } from "./pages/HashesPage";
import { JobsPage } from "./pages/JobsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { MonitoringSettingsPage } from "./pages/MonitoringSettingsPage";
import { SettingsPage } from "./pages/SettingsPage";
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
  if (path === "/react/legacy-vms") {
    return <ClassicVmsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/devices") {
    return <CloudDevicesPage bootstrap={bootstrap} />;
  }
  if (path === "/react/hashes") {
    return <HashesPage bootstrap={bootstrap} />;
  }
  if (path === "/react/files") {
    return <FilesPage bootstrap={bootstrap} />;
  }
  if (path === "/react/settings") {
    return <SettingsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/credentials" || path === "/react/credentials/new" || /^\/react\/credentials\/\d+\/edit$/u.test(path)) {
    return <CredentialsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/monitoring/settings") {
    return <MonitoringSettingsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/agent-download") {
    return <AgentDownloadPage bootstrap={bootstrap} />;
  }
  return <ShellIndexPage bootstrap={bootstrap} />;
}
