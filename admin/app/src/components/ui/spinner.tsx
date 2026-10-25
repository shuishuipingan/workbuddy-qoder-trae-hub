import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/cn';

/** 行内加载指示器；带 aria-label 以便读屏知道「正在加载」。 */
export function Spinner({ size = 16, className, label }: { size?: number; className?: string; label?: string }) {
  return (
    <span role="status" aria-live="polite" className={cn("inline-flex items-center gap-2 text-fg-mute", className)}>
      <Loader2 size={size} className="animate-spin" aria-hidden="true" />
      {label ? <span className="text-xs">{label}</span> : <span className="sr-only">{label ?? "加载中"}</span>}
    </span>
  );
}
