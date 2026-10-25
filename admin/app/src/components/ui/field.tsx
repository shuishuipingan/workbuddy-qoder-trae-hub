import { useId } from 'react';
import { cn } from '@/lib/cn';

/**
 * 字段容器：标签 + 控件 + **就地错误**（不靠 toast）。
 *
 * a11y 契约：
 *  · 错误时控件带 aria-invalid，并由 aria-describedby 指向错误文本
 *  · 错误文本带 role="alert"，出现即被读屏播报
 *  · 提示（hint）与错误互斥：有错时优先显示错误
 */
export function Field({
  label,
  hint,
  error,
  required,
  htmlFor,
  children,
  className,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  error?: string | null;
  required?: boolean;
  htmlFor?: string;
  /** 渲染回调：把返回的 props 直接展开到控件上即可（含 aria-describedby）。 */
  children: (props: { id: string; invalid: boolean; "aria-describedby"?: string }) => React.ReactNode;
  className?: string;
}) {
  const autoId = useId();
  const id = htmlFor || autoId;
  const errId = id + "-err";
  const hintId = id + "-hint";
  const invalid = !!error;
  const describedBy = invalid ? errId : hint ? hintId : undefined;

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-xs font-medium text-fg-dim">
        {label}
        {required ? <span className="ml-1 text-err" aria-hidden="true">*</span> : null}
      </label>
      {children({ id, invalid, "aria-describedby": describedBy })}
      {invalid ? (
        <p id={errId} role="alert" className="text-xs text-err">
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-fg-mute">{hint}</p>
      ) : null}
    </div>
  );
}
