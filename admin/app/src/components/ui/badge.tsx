import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/cn';
import { EngineGlyph } from '@/components/app/brand';

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-5 whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-line bg-surface-2 text-fg-dim",
        accent: "border-transparent bg-accent-soft text-accent",
        ok: "border-transparent bg-ok-soft text-ok",
        warn: "border-transparent bg-warn-soft text-warn",
        err: "border-transparent bg-err-soft text-err",
        info: "border-transparent bg-info-soft text-info",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeProps = React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...rest }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...rest} />;
}

/** 引擎徽标：三家各有稳定识别色（与旧面板一致：wb 蓝 / qd 青 / trae 橙）。 */
/** 引擎徽标：品牌色字形块 + 名称（原来只有文字胶囊，扫列表时认不出是哪家）。 */
export function EngineBadge({ engine, label, className }: { engine: string; label?: string; className?: string }) {
  return (
    <span
      data-engine={engine}
      className={cn("inline-flex items-center gap-1.5 rounded-full border border-line bg-surface-2 px-1.5 py-0.5 text-[11px] leading-5 text-fg-dim", className)}
    >
      <EngineGlyph engine={engine} size={13} />
      <span className="pr-0.5">{label || engine}</span>
    </span>
  );
}

export { badgeVariants };
