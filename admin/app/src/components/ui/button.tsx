import { forwardRef } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/cn';

/**
 * 按钮：五态齐全 + loading 防重复提交。
 *
 *  · 高度 ≥ 44px（触屏目标），小尺寸也保 36px 但不是主操作
 *  · 只用 transform / filter / colors 做动效（不触发布局）
 *  · focus-visible 走全局 outline，这里额外保证不被 overflow 裁掉
 *  · loading 时 disabled + spinner + aria-busy，点击不再触发
 */
const buttonVariants = cva(
  [
    "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap",
    "rounded-md border font-medium transition-[color,background-color,border-color,filter,transform]",
    "duration-150 ease-[var(--ease-out)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
    "transition-[transform,background-color,border-color,box-shadow,color,filter] duration-150 ease-[cubic-bezier(0.22,1,0.36,1)]",
    "hover:-translate-y-0.5 hover:shadow-1 active:translate-y-0 active:scale-[0.985] disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-55",
    "cursor-pointer [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: "border-transparent bg-accent text-accent-fg shadow-1 hover:brightness-110",
        secondary: "border-line bg-surface text-fg hover:bg-surface-2 hover:border-line-strong",
        ghost: "border-transparent bg-transparent text-fg-dim hover:bg-surface-2 hover:text-fg",
        danger: "border-transparent bg-danger-bg text-danger-fg shadow-1 hover:bg-danger-bg-hover",
        outline: "border-line bg-transparent text-fg hover:bg-surface-2",
      },
      size: {
        sm: "h-7 px-2.5 text-xs gap-1.5",
        md: "h-8 px-3 text-[13px] gap-1.5",
        lg: "h-12 px-5 text-[15px]",
        icon: "h-11 w-11 p-0",
      },
      block: { true: "w-full", false: "" },
    },
    defaultVariants: { variant: "secondary", size: "md", block: false },
  },
);

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & {
    /** 结算中：disabled + spinner，重复点击被吞掉 */
    loading?: boolean;
    /** 加载时的替换文案（可选） */
    loadingText?: string;
    icon?: React.ReactNode;
  };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, block, loading = false, loadingText, icon, children, disabled, type = "button", ...rest },
  ref,
) {
  const isDisabled = disabled || loading;
  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={cn(buttonVariants({ variant, size, block }), className)}
      {...rest}
    >
      {loading ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : icon}
      <span className="truncate">{loading && loadingText ? loadingText : children}</span>
    </button>
  );
});

export { buttonVariants };
