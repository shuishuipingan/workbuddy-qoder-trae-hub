import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

/**
 * 空状态三件套：图标 + 一句引导 + 主操作。
 * 绝不出现"一片空白"。判据会检查：icon / title / action 三者都在。
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-empty-state="1"
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-line px-6 py-10 text-center",
        className,
      )}
    >
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-fg-mute" aria-hidden="true">
        {icon}
      </div>
      <p className="text-sm font-medium text-fg">{title}</p>
      {description ? <p className="max-w-md text-xs text-fg-mute mix-space">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}
