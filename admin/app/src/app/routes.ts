import { Activity, Boxes, Coins, KeyRound, LayoutDashboard, ScrollText, Settings, Users, Key } from 'lucide-react';

export type RouteId = "overview" | "accounts" | "models" | "metrics" | "logs" | "api" | "keys" | "settings" | "rewards";

export type RouteMeta = {
  id: RouteId;
  path: string;
  labelKey: string;
  icon: typeof LayoutDashboard;
  group: "main" | "ops" | "config";
};

export const ROUTES: RouteMeta[] = [
  { id: "overview", path: "/overview", labelKey: "nav.overview", icon: LayoutDashboard, group: "main" },
  { id: "accounts", path: "/accounts", labelKey: "nav.accounts", icon: Users, group: "main" },
  { id: "models", path: "/models", labelKey: "nav.models", icon: Boxes, group: "main" },
  { id: "metrics", path: "/metrics", labelKey: "nav.metrics", icon: Activity, group: "main" },
  { id: "logs", path: "/logs", labelKey: "nav.logs", icon: ScrollText, group: "ops" },
  { id: "api", path: "/api", labelKey: "nav.api", icon: KeyRound, group: "ops" },
  { id: "keys", path: "/keys", labelKey: "nav.keys", icon: Key, group: "config" },
  { id: "settings", path: "/settings", labelKey: "nav.settings", icon: Settings, group: "config" },
  { id: "rewards", path: "/rewards", labelKey: "nav.rewards", icon: Coins, group: "config" },
];

export const GROUP_LABEL: Record<RouteMeta["group"], string> = {
  main: "nav.group.main",
  ops: "nav.group.ops",
  config: "nav.group.config",
};

export function routeById(id: RouteId): RouteMeta | undefined {
  return ROUTES.find((r) => r.id === id);
}
