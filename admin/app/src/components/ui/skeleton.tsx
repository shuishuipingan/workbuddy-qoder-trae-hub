import { cn } from '@/lib/cn';

/** 骨架块：数据未到时的占位，避免"白屏 → 内容"的跳变。 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "animate-pulse rounded-md bg-surface-2",
        "motion-reduce:animate-none",
        className,
      )}
    />
  );
}

/** 常用组合：KPI 卡骨架 */
export function SkeletonCard() {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="mt-3 h-7 w-32" />
      <Skeleton className="mt-2 h-3 w-40" />
    </div>
  );
}

/** 表格骨架：rows 行 × cols 列 */
export function SkeletonRows({ rows = 6, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="flex flex-col gap-2" aria-busy="true" aria-live="polite">
      <span className="sr-only">加载中</span>
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="grid gap-3" style={{ gridTemplateColumns: "repeat(" + cols + ", minmax(0,1fr))" }}>
          {Array.from({ length: cols }).map((__, c) => (
            <Skeleton key={c} className="h-4" />
          ))}
        </div>
      ))}
    </div>
  );
}
