import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { AppBootstrap, OperatorMode, OperatorNavGroup, OperatorOutcome, OperatorQuickRoute } from "../contracts";
import { OperatorRouteMap, OperatorTopBar, OutcomeCardGrid, OutcomeModeRail, QuickRouteLane, SystemTray } from "./OutcomeNavigation";

const modes: readonly OperatorMode[] = [
  { id: "home", label: "Home", longLabel: "Home", href: "/react-shell" },
  { id: "deploy", label: "Deploy", longLabel: "Deploy", href: "/react/cloudosd" },
  { id: "build", label: "Build", longLabel: "Build", href: "/react/task-engine" }
];

const outcomes: readonly OperatorOutcome[] = [
  {
    id: "deploy-desktop",
    mode: "deploy",
    eyebrow: "Recommended",
    title: "Deploy a Windows desktop",
    summary: "Open OSDCloud Desktop.",
    primaryHref: "/react/cloudosd",
    actionLabel: "Start desktop run",
    tone: "good",
    relatedRoutes: [{ label: "Jobs", href: "/react/jobs", purpose: "Live output" }]
  }
];

const quickRoutes: readonly OperatorQuickRoute[] = [
  { label: "Jobs", href: "/react/jobs", summary: "Live output and pause gates", mode: "home" }
];

const routeGroups: readonly OperatorNavGroup[] = [
  {
    label: "Deploy",
    items: [
      { path: "/react/cloudosd", label: "OSDCloud Desktop", group: "Deploy", phase: "operational", active: true },
      { path: "/react/cloudosd/runs/:runId", label: "OSDCloud Run", group: "Deploy", phase: "operational", active: true, showInNav: false },
      { path: "/react/provision", label: "Provision", group: "Deploy", phase: "operational", active: true }
    ]
  },
  {
    label: "Build",
    items: [
      { path: "/react/task-engine", label: "Task Sequences", group: "Build", phase: "operational", active: true },
      { path: "/react/task-engine/sequences/list", label: "Sequence Library", group: "Build", phase: "operational", active: true, showInNav: false },
      { path: "/react/task-engine/sequences/new", label: "New Sequence", group: "Build", phase: "operational", active: true, showInNav: false }
    ]
  }
];

const bootstrap: AppBootstrap = {
  buildSha: "abc1234",
  buildTime: "2026-06-19T19:00:00Z",
  userName: "Adam"
};

describe("OutcomeNavigation components", () => {
  test("renders mode rail with active mode", () => {
    render(<OutcomeModeRail modes={modes} activeMode="deploy" />);

    expect(screen.getByRole("navigation", { name: "Outcome modes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Deploy" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  test("renders outcome cards with primary actions and related routes", () => {
    render(<OutcomeCardGrid outcomes={outcomes} />);

    expect(screen.getByRole("heading", { name: "Deploy a Windows desktop" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start desktop run" })).toHaveAttribute("href", "/react/cloudosd");
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders quick routes", () => {
    render(<QuickRouteLane quickRoutes={quickRoutes} />);

    expect(screen.getByRole("navigation", { name: "Quick routes" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jobs Live output and pause gates" })).toHaveAttribute("href", "/react/jobs");
  });

  test("renders a grouped route map with primary links and deeper entries", () => {
    render(<OperatorRouteMap groups={routeGroups} />);

    const routeMap = screen.getByRole("navigation", { name: "Route map" });
    const deployGroup = within(routeMap).getByRole("group", { name: "Deploy" });
    const buildGroup = within(routeMap).getByRole("group", { name: "Build" });

    expect(within(deployGroup).getByRole("link", { name: "Provision operational" })).toHaveAttribute("href", "/react/provision");
    expect(within(buildGroup).getByRole("link", { name: "Sequence Library operational" })).toHaveAttribute("href", "/react/task-engine/sequences/list");
    expect(within(buildGroup).getByRole("link", { name: "New Sequence operational" })).toHaveAttribute("href", "/react/task-engine/sequences/new");
    expect(within(deployGroup).queryByRole("link", { name: "OSDCloud Run operational" })).not.toBeInTheDocument();
    expect(within(deployGroup).getByText("OSDCloud Run")).toBeInTheDocument();
    expect(within(deployGroup).getByText("detail")).toBeInTheDocument();
  });

  test("renders top bar command and operator identity", () => {
    render(<OperatorTopBar bootstrap={bootstrap} query="" onQueryChange={() => {}} onSubmit={() => {}} />);

    expect(screen.getByRole("link", { name: "Proxmox VE Autopilot home" })).toHaveAttribute("href", "/react-shell");
    expect(screen.getByRole("searchbox", { name: "Search console" })).toBeInTheDocument();
    expect(screen.getByText("Adam")).toBeInTheDocument();
  });

  test("renders system tray build and socket state", () => {
    render(<SystemTray bootstrap={bootstrap} socketState="open" />);

    expect(screen.getByText("Live open")).toBeInTheDocument();
    expect(screen.getByText("Build abc1234")).toBeInTheDocument();
  });
});
