import type { AppBootstrap } from "./contracts";
import { AgentDownloadPage } from "./pages/AgentDownloadPage";
import { AnswerIsosPage } from "./pages/AnswerIsosPage";
import { ClassicVmsPage } from "./pages/ClassicVmsPage";
import { CloudDevicesPage } from "./pages/CloudDevicesPage";
import { CloudosdPage } from "./pages/CloudosdPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DeployJourneyPage } from "./pages/DeployJourneyPage";
import { DeploymentRunPage } from "./pages/DeploymentRunPage";
import { FilesPage } from "./pages/FilesPage";
import { HashesPage } from "./pages/HashesPage";
import { InstallTrackingPage } from "./pages/InstallTrackingPage";
import { LabsPage } from "./pages/LabsPage";
import { JobDetailPage, RunDetailPage, RunsPage } from "./pages/JobAndRunsPage";
import { JobsPage } from "./pages/JobsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { MonitoringSettingsPage } from "./pages/MonitoringSettingsPage";
import { NetworksPage } from "./pages/NetworksPage";
import { OnboardingPage } from "./pages/OnboardingPage";
import { OnboardingSetupPage } from "./pages/OnboardingSetupPage";
import { OsdeployPage } from "./pages/OsdeployPage";
import { ProvisionPage } from "./pages/ProvisionPage";
import { LoginPage, SetupPage } from "./pages/PublicPages";
import { SettingsPage } from "./pages/SettingsPage";
import { ShellIndexPage } from "./pages/ShellIndexPage";
import { TaskEnginePage } from "./pages/TaskEnginePage";
import { TemplatePage } from "./pages/TemplatePage";
import { UtmVmsPage } from "./pages/UtmVmsPage";
import { VmsPage } from "./pages/VmsPage";

interface AppProps {
  readonly bootstrap: AppBootstrap;
}

export function App({ bootstrap }: AppProps) {
  const path = window.location.pathname;
  if (path === "/auth/login") {
    return <LoginPage bootstrap={bootstrap} />;
  }
  if (path === "/setup") {
    return <SetupPage bootstrap={bootstrap} />;
  }
  if (path === "/react/dashboard") {
    return <DashboardPage bootstrap={bootstrap} />;
  }
  if (path === "/react/jobs") {
    return <JobsPage bootstrap={bootstrap} />;
  }
  {
    const jobMatch = /^\/react\/jobs\/([^/]+)$/u.exec(path);
    if (jobMatch?.[1]) {
      return <JobDetailPage bootstrap={bootstrap} jobId={jobMatch[1]} />;
    }
  }
  if (path === "/react/runs") {
    return <RunsPage bootstrap={bootstrap} />;
  }
  {
    const runMatch = /^\/react\/runs\/(\d+)$/u.exec(path);
    if (runMatch?.[1]) {
      return <RunDetailPage bootstrap={bootstrap} runId={runMatch[1]} />;
    }
  }
  if (path === "/react/monitoring") {
    return <MonitoringPage bootstrap={bootstrap} />;
  }
  if (path === "/react/install-tracking") {
    return <InstallTrackingPage bootstrap={bootstrap} />;
  }
  if (path === "/react/labs") {
    return <LabsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/networks") {
    return <NetworksPage bootstrap={bootstrap} />;
  }
  if (path === "/react/vms" || /^\/react\/vms\/\d+$/.test(path)) {
    return <VmsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/legacy-vms") {
    return <ClassicVmsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/utm-vms") {
    return <UtmVmsPage bootstrap={bootstrap} />;
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
  if (path === "/react/provision") {
    return <ProvisionPage bootstrap={bootstrap} />;
  }
  if (path === "/react/deploy") {
    return <DeployJourneyPage bootstrap={bootstrap} />;
  }
  if (path === "/react/cloudosd") {
    return <CloudosdPage bootstrap={bootstrap} />;
  }
  {
    const cloudosdRunMatch = /^\/react\/cloudosd\/runs\/([^/]+)$/u.exec(path);
    if (cloudosdRunMatch?.[1]) {
      return <DeploymentRunPage bootstrap={bootstrap} kind="cloudosd" runId={cloudosdRunMatch[1]} />;
    }
  }
  if (path === "/react/osdeploy") {
    return <OsdeployPage bootstrap={bootstrap} />;
  }
  {
    const osdeployRunMatch = /^\/react\/osdeploy\/runs\/([^/]+)$/u.exec(path);
    if (osdeployRunMatch?.[1]) {
      return <DeploymentRunPage bootstrap={bootstrap} kind="osdeploy" runId={osdeployRunMatch[1]} />;
    }
  }
  if (path === "/react/template") {
    return <TemplatePage bootstrap={bootstrap} />;
  }
  if (path === "/react/answer-isos") {
    return <AnswerIsosPage bootstrap={bootstrap} />;
  }
  if (
    path === "/react/task-engine" ||
    path === "/react/task-engine/sequences/list" ||
    path === "/react/task-engine/sequences/new" ||
    /^\/react\/task-engine\/sequences\/templates\/[^/]+$/u.test(path) ||
    /^\/react\/task-engine\/sequences\/[^/]+\/edit$/u.test(path)
  ) {
    return <TaskEnginePage bootstrap={bootstrap} />;
  }
  if (path === "/react/agent-download") {
    return <AgentDownloadPage bootstrap={bootstrap} />;
  }
  if (path === "/react/onboarding") {
    return <OnboardingPage bootstrap={bootstrap} />;
  }
  if (path === "/react/onboarding/setup") {
    return <OnboardingSetupPage bootstrap={bootstrap} />;
  }
  return <ShellIndexPage bootstrap={bootstrap} />;
}
