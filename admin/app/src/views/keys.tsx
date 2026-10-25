/**
 * 网关 Key：合并 Key（一把调全部）+ 每个引擎自己的 Key，全在这一页。
 *
 * 为什么独立成页：这些是"谁能进网关"的配置，平时改得最多；埋在设置页里要点两次才看到，
 * 而且设置页本身还有限额/调度/代理/高级开关，混在一起不好找。
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, EngineBadge, SkeletonRows } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { ENGINES, ENGINE_LABELS, engineSettings, type ApiKey, type Engine } from '@/lib/api';
import { EngineKeyCard, HubPanels } from './settings';

export default function KeysView() {
  const { t } = useLang();
  const [engine, setEngine] = useState<Engine>("wb");
  const settings = useQuery({ queryKey: ["settings", engine], queryFn: function () { return engineSettings(engine); } });
  const rows = (settings.data?.api_keys ?? []) as ApiKey[];

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={t("keys.engineTitle")}
        subtitle={t("keys.engineHint")}
        actions={<EngineBadge engine={engine} label={ENGINE_LABELS[engine]} />}
      >
        <div role="tablist" aria-label={t("engine.label")} className="mb-3 flex w-fit gap-1 rounded-md border border-line p-0.5">
          {ENGINES.map(function (eng) {
            return (
              <button
                key={eng}
                role="tab"
                aria-selected={engine === eng}
                onClick={function () { setEngine(eng); }}
                className={"min-h-9 cursor-pointer rounded px-2.5 text-xs transition-[background-color,color,transform] duration-150 active:scale-95 " +
                  (engine === eng ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")}
              >
                {ENGINE_LABELS[eng]}
              </button>
            );
          })}
        </div>
        {settings.isPending ? <SkeletonRows rows={3} cols={4} /> : (
          <EngineKeyCard engine={engine} rows={rows} t={t} />
        )}
      </Card>

      <HubPanels t={t} only="keys" />
    </div>
  );
}
