import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import { setPanelPassword } from '@/lib/api';
import KeysView from './keys';

type Call = { url: string; method: string; body: string | null };
let calls: Call[] = [];
const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { "Content-Type": "application/json" } });
const user = () => userEvent.setup({ pointerEventsCheck: 0 });

function mockFetch(handler: (url: string, method: string) => Response) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url: url, method: method, body: typeof init?.body === "string" ? init.body : null });
    return handler(url, method);
  }));
}

const SETTINGS = { panel_password_is_default: true, api_keys: [{ id: "k0", name: "国际", masked: "7260****8fb5", enabled: true }], limits: {} };

function renderKeys() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <KeysView />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => { sessionStorage.setItem("hub-panel-token", "tk"); });
afterEach(() => { vi.restoreAllMocks(); });

describe('KeysView', () => {
  it('合并 Key 新增：把新条目并入 merged_keys 提交', async () => {
    setPanelPassword("admin");
    mockFetch((url) => {
      if (url.includes("/hub/keys")) return json({ ok: true, merged: [], counts: {} });
      if (url.includes("/hub/scheduler")) return json({ engines: {} });
      if (url.includes("/hub/selfcheck")) return json({ checks: [] });
      return json(SETTINGS);
    });
    renderKeys();

    const nameInput = await screen.findByPlaceholderText("merged-1");
    fireEvent.change(nameInput, { target: { value: "k-new" } });
    const card = nameInput.closest("section") as HTMLElement;
    const secret = card.querySelector("input[type=password]") as HTMLInputElement;
    fireEvent.change(secret, { target: { value: "sk-abc" } });

    const add = Array.from(card.querySelectorAll("button")).find((b) => /新增|add/i.test((b.textContent || "").trim()));
    expect(add).toBeTruthy();
    await user().click(add!);

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/hub/settings/save"));
      expect(post).toBeTruthy();
      expect(post!.body).toContain("k-new");
    });
  });

  it('合并 Key 删除：两步确认后才提交', async () => {
    setPanelPassword("admin");
    mockFetch((url) => {
      if (url.includes("/hub/keys")) return json({ ok: true, merged: [{ id: "m1", name: "old-key", key: "sk-old" }], counts: {} });
      if (url.includes("/hub/scheduler")) return json({ engines: {} });
      if (url.includes("/hub/selfcheck")) return json({ checks: [] });
      return json(SETTINGS);
    });
    renderKeys();

    const del = await screen.findByRole("button", { name: /删除|delete/i });
    await user().click(del);
    expect(calls.some((c) => c.method === "POST" && c.url.includes("/hub/settings/save"))).toBe(false);

    const confirm = screen.getAllByRole("button").find((b) => /确认|confirm/i.test((b.textContent || "").trim()));
    await user().click(confirm!);

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/hub/settings/save"));
      expect(post).toBeTruthy();
      expect(post!.body).not.toContain("old-key");
    });
  });
});
