import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Copy, KeyRound, RefreshCw, Settings } from 'lucide-react';
import {
  Badge, Button, Card, EmptyState, EngineBadge, SkeletonRows, TableWrap, Td, Th, Tr, useToast,
} from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { ENGINE_LABELS, ENGINES, hubHealth, hubKeys, type Engine } from '@/lib/api';

/** 门户对外只有一个地址：<origin>/api/v1。所有引擎都在它后面。 */
const API_PATH = "/api/v1";

type CopyTarget = "base" | "models" | "chat";

const curlModels = (base: string): string =>
  [
    "curl " + base + "/models \\",
    '  -H "Authorization: Bearer <KEY>"',
  ].join("\n");

const curlChat = (base: string): string =>
  [
    "curl " + base + "/chat/completions \\",
    '  -H "Authorization: Bearer <KEY>" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d '{"model":"<MODEL>","messages":[{"role":"user","content":"hi"}]}'`,
  ].join("\n");

/**
 * 接入说明：一个地址 + 三类 key 各走哪条路 + 可粘贴示例 + 引擎端点状态。
 * 数据都来自 /api/hub/keys 与 /api/hub/health（与总览页同一份 health）。
 */
export default function ApiView() {
  const { t } = useLang();
  const toast = useToast();
  const navigate = useNavigate();

  // origin 只在首帧取一次：测试与多实例联调时不随路由抖动。
  const [origin] = useState(() => (typeof window === "undefined" ? "" : window.location.origin));
  const base = origin + API_PATH;

  const keysQuery = useQuery({ queryKey: ["hub-keys"], queryFn: hubKeys });
  const healthQuery = useQuery({ queryKey: ["hub-health"], queryFn: hubHealth });

  const [manual, setManual] = useState<CopyTarget | null>(null);
  const baseRef = useRef<HTMLElement | null>(null);

  const keys = keysQuery.data;
  const mergedCount = keys?.merged?.length ?? 0;
  const counts = keys?.counts as Record<string, unknown> | undefined;

  /** 引擎 key 数：优先数 hub/keys 里该引擎的条目数组，旧后端只给 counts 时退回计数。 */
  const engineKeyCount = (eng: Engine): number => {
    const list = keys?.[eng];
    if (Array.isArray(list)) return list.length;
    const n = Number(counts?.[eng]);
    return Number.isFinite(n) ? n : 0;
  };
  const engineKeyTotal = ENGINES.reduce((sum, eng) => sum + engineKeyCount(eng), 0);

  const rules = keys?.rules ?? {};
  const ruleOf = (id: string): string => {
    const value = rules[id];
    return typeof value === "string" ? value : "";
  };

  const engines = healthQuery.data?.engines ?? {};
  const online = ENGINES.filter((eng) => {
    const info = engines[eng];
    return !!info && info.ok !== false;
  }).length;

  const busy = keysQuery.isFetching || healthQuery.isFetching;
  const reloadAll = () => {
    void keysQuery.refetch();
    void healthQuery.refetch();
  };

  const selectNode = (node: HTMLElement | null) => {
    try {
      if (!node) return;
      const selection = window.getSelection();
      if (!selection) return;
      const range = document.createRange();
      range.selectNodeContents(node);
      selection.removeAllRanges();
      selection.addRange(range);
    } catch {
      /* 选区失败不影响提示：toast 里已经说了手动复制 */
    }
  };

  /** 复制：成功播报；失败就把目标文本全选，并在原地提示手动复制。 */
  const copy = async (target: CopyTarget, text: string, node: HTMLElement | null) => {
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      setManual(null);
      toast.push("ok", t("common.copied"));
    } catch {
      setManual(target);
      selectNode(node);
      toast.push("err", t("common.copyFailed") + " · " + t("copy.manualHint"));
    }
  };

  const modelsCurl = curlModels(base);
  const chatCurl = curlChat(base);

  return (
    <div className="flex flex-col gap-4">
      {/* 1) 唯一调用地址 */}
      <Card
        title={t("nav.api")}
        subtitle={t("app.tagline")}
        actions={(
          <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={busy} onClick={reloadAll}>
            {t("common.refresh")}
          </Button>
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="accent" className="font-mono">API</Badge>
          <code
            ref={baseRef}
            title={base}
            className="min-w-0 flex-1 select-all truncate rounded-md border border-line bg-surface-2 px-3 py-2 font-mono text-xs text-fg"
          >
            {base}
          </code>
          <Button
            size="sm"
            variant="secondary"
            icon={<Copy size={14} />}
            aria-label={t("common.copy") + " · API"}
            onClick={() => void copy("base", base, baseRef.current)}
          >
            {t("common.copy")}
          </Button>
        </div>
        {manual === "base" ? (
          <p role="status" className="mt-2 text-[11px] text-warn mix-space">{t("copy.manualHint")}</p>
        ) : null}
      </Card>

      {/* 2) 哪把 key 走哪条路 */}
      <Card title={t("overview.hubRoute.title")} subtitle={t("overview.hubRoute.hint")}>
        {keysQuery.isPending ? <SkeletonRows rows={4} cols={3} /> : null}

        {keysQuery.isError ? (
          <LoadError
            detail={String((keysQuery.error as Error)?.message || "")}
            busy={keysQuery.isFetching}
            onRetry={() => void keysQuery.refetch()}
          />
        ) : null}

        {!keysQuery.isPending && !keysQuery.isError ? (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={mergedCount > 0 ? "accent" : "warn"}>
                {t("health.mergedKeys", { n: mergedCount })}
              </Badge>
              <Badge tone="neutral">{t("health.engineKeys", { n: engineKeyTotal })}</Badge>
            </div>

            {mergedCount === 0 ? (
              <EmptyState
                icon={<KeyRound size={20} />}
                title={t("health.noMergedKey")}
                action={(
                  <Button size="sm" variant="primary" icon={<Settings size={14} />} onClick={() => navigate("/settings")}>
                    {t("nav.settings")}
                  </Button>
                )}
              />
            ) : null}

            <TableWrap>
              <thead>
                <tr>
                  <Th>{t("col.key")}</Th>
                  <Th>{t("engine.label")}</Th>
                  <Th className="text-right">{t("common.total")}</Th>
                </tr>
              </thead>
              <tbody>
                <Tr>
                  <Td className="max-w-[320px]">
                    <span className="block truncate font-medium text-fg">{t("engine.merge")} key</span>
                    {ruleOf("merged") ? (
                      <p className="truncate text-[11px] text-fg-mute" title={ruleOf("merged")}>{ruleOf("merged")}</p>
                    ) : null}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {ENGINES.map((eng) => (
                        <EngineBadge key={eng} engine={eng} label={ENGINE_LABELS[eng]} />
                      ))}
                    </div>
                  </Td>
                  <Td className="text-right tnum">{mergedCount}</Td>
                </Tr>
                {ENGINES.map((eng) => (
                  <Tr key={eng}>
                    <Td className="max-w-[320px]">
                      <span className="block truncate font-medium text-fg">{ENGINE_LABELS[eng]} key</span>
                      {ruleOf(eng) ? (
                        <p className="truncate text-[11px] text-fg-mute" title={ruleOf(eng)}>{ruleOf(eng)}</p>
                      ) : null}
                    </Td>
                    <Td><EngineBadge engine={eng} /></Td>
                    <Td className="text-right tnum">{engineKeyCount(eng)}</Td>
                  </Tr>
                ))}
              </tbody>
            </TableWrap>
          </div>
        ) : null}
      </Card>

      {/* 3) 可粘贴的 curl 示例 */}
      <Card title={t("overview.engine.apiKeyRequired")}>
        <div className="flex flex-col gap-3">
          <CodeBlock
            title="GET /models"
            code={modelsCurl}
            failed={manual === "models"}
            onCopy={(node) => void copy("models", modelsCurl, node)}
          />
          <CodeBlock
            title="POST /chat/completions"
            code={chatCurl}
            failed={manual === "chat"}
            onCopy={(node) => void copy("chat", chatCurl, node)}
          />
        </div>
      </Card>

      {/* 3b) 出口与引擎覆盖：同一个端口的两种定向方式 */}
      <Card title={t("api.route.title")} subtitle={t("api.route.onePort")}>
        <div className="flex flex-col gap-2 text-[12px] text-fg-dim">
          <p className="mix-space">{t("api.route.realm")}</p>
          <pre className="overflow-auto rounded-md border border-line-faint bg-surface-2 px-2.5 py-2 font-mono text-[11px]">{'curl ' + base + '/chat/completions?realm=cn \
  -H "Authorization: Bearer <your-key>" \
  -d \'{"model":"hy3","messages":[{"role":"user","content":"hi"}]}\''}</pre>
          <p className="mix-space">{t("api.route.v1Alias")}</p>
          <p className="mix-space">{t("api.route.engine")}</p>
          <pre className="overflow-auto rounded-md border border-line-faint bg-surface-2 px-2.5 py-2 font-mono text-[11px]">{'curl ' + base + '/models?engine=qd \
  -H "Authorization: Bearer <your-qd-key>"'}</pre>
        </div>
      </Card>

      {/* 4) 引擎端点：名称 + 版本 + 在线状态 */}
      <Card
        title={t("overview.engine.title")}
        subtitle={healthQuery.data ? t("engine.online", { up: online, total: ENGINES.length }) : undefined}
        actions={(
          <Button
            size="sm"
            variant="secondary"
            icon={<RefreshCw size={14} />}
            loading={healthQuery.isFetching}
            onClick={() => void healthQuery.refetch()}
          >
            {t("common.refresh")}
          </Button>
        )}
      >
        {healthQuery.isPending ? <SkeletonRows rows={3} cols={3} /> : null}

        {healthQuery.isError ? (
          <LoadError
            detail={String((healthQuery.error as Error)?.message || "")}
            busy={healthQuery.isFetching}
            onRetry={() => void healthQuery.refetch()}
          />
        ) : null}

        {!healthQuery.isPending && !healthQuery.isError ? (
          <TableWrap>
            <thead>
              <tr>
                <Th>{t("engine.label")}</Th>
                <Th>{t("app.version")}</Th>
                <Th className="text-right">{t("common.status")}</Th>
              </tr>
            </thead>
            <tbody>
              {ENGINES.map((eng) => {
                const info = engines[eng];
                const up = !!info && info.ok !== false;
                return (
                  <Tr key={eng}>
                    <Td><EngineBadge engine={eng} label={ENGINE_LABELS[eng]} /></Td>
                    <Td className="max-w-[160px]">
                      <span
                        className="block truncate font-mono text-xs text-fg-mute tnum"
                        title={info?.version ? String(info.version) : undefined}
                      >
                        {String(info?.version || t("common.dash"))}
                      </span>
                    </Td>
                    <Td className="text-right">
                      {info ? (
                        <Badge tone={up ? "ok" : "err"}>
                          {up ? t("overview.engine.up") : t("overview.engine.down")}
                        </Badge>
                      ) : (
                        <Badge tone="neutral">{t("common.unknown")}</Badge>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </TableWrap>
        ) : null}
      </Card>
    </div>
  );
}

/** 代码块：标题行带复制按钮，失败时原地提示手动复制。 */
function CodeBlock(props: {
  title: string;
  code: string;
  failed: boolean;
  onCopy: (node: HTMLElement | null) => void;
}) {
  const { t } = useLang();
  const { title, code, failed, onCopy } = props;
  const ref = useRef<HTMLPreElement | null>(null);

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-dim" title={title}>{title}</span>
        <Button
          size="sm"
          variant="ghost"
          icon={<Copy size={14} />}
          aria-label={t("common.copy") + " · " + title}
          onClick={() => onCopy(ref.current)}
        >
          {t("common.copy")}
        </Button>
      </div>
      <pre
        ref={ref}
        className="overflow-x-auto rounded-md border border-line bg-surface-2 p-3 font-mono text-xs leading-6 text-fg-dim"
      >
        <code>{code}</code>
      </pre>
      {failed ? (
        <p role="status" className="mt-1 text-[11px] text-warn mix-space">{t("copy.manualHint")}</p>
      ) : null}
    </div>
  );
}

/** 取数失败：标题 + 原因 + 重试（不静默留白）。 */
function LoadError(props: { detail: string; busy: boolean; onRetry: () => void }) {
  const { t } = useLang();
  const { detail, busy, onRetry } = props;

  return (
    <div className="flex flex-col items-start gap-2">
      <p className="text-sm text-err">{t("state.errTitle")}</p>
      <p className="max-w-full truncate text-xs text-fg-mute" title={detail}>
        {detail || t("state.errDesc")}
      </p>
      <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={busy} onClick={onRetry}>
        {t("common.retry")}
      </Button>
    </div>
  );
}
