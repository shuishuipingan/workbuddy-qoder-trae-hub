import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

/** 输入框：五态齐全；错误态由 aria-invalid 驱动（颜色 + 描边一起变）。 */
export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean };

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, invalid, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        "h-8 w-full rounded-md border border-line bg-surface px-3 text-[13px] text-fg placeholder:text-fg-mute",
        "transition-[border-color,background-color] duration-150",
        "hover:border-line-strong focus-visible:border-accent",
        "disabled:cursor-not-allowed disabled:opacity-55 disabled:bg-surface-2",
        invalid && "border-err hover:border-err focus-visible:border-err",
        className,
      )}
      {...rest}
    />
  );
});

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }>(
  function Textarea({ className, invalid, ...rest }, ref) {
    return (
      <textarea
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          "w-full rounded-md border border-line bg-surface px-3 py-2 text-[13px] text-fg placeholder:text-fg-mute",
          "transition-[border-color] duration-150 hover:border-line-strong focus-visible:border-accent",
          "disabled:cursor-not-allowed disabled:opacity-55",
          invalid && "border-err hover:border-err",
          className,
        )}
        {...rest}
      />
    );
  },
);
