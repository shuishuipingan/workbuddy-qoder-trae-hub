import { cn } from '@/lib/cn';

/** 卡片：标题行 + 可选操作区 + 内容。语义标签用 section，标题用 h2。 */
export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
  as: Tag = "section",
}: {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  as?: "section" | "article" | "div";
}) {
  return (
    <Tag className={cn("card-lift rounded-lg border border-line bg-surface shadow-1 transition-[transform,box-shadow,border-color] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]", className)}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-line-faint px-4 py-3">
          <div className="min-w-0">
            {title ? <h2 className="truncate text-[13px] font-semibold text-fg">{title}</h2> : null}
            {subtitle ? <p className="mt-0.5 text-[11.5px] text-fg-mute mix-space">{subtitle}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
        </header>
      )}
      <div className={cn("p-4", bodyClassName)}>{children}</div>
    </Tag>
  );
}
