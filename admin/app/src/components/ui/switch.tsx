import * as S from '@radix-ui/react-switch';
import { cn } from '@/lib/cn';

/** 开关：44×24 命中区，键盘可用（Radix 处理 Space/Enter），禁用态 cursor-not-allowed。 */
export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
  className,
  id,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
  label?: string;
  className?: string;
  id?: string;
}) {
  return (
    <S.Root
      id={id}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full border border-line bg-surface-2 transition-colors duration-150",
        "cursor-pointer data-[state=checked]:border-accent data-[state=checked]:bg-accent",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        "disabled:cursor-not-allowed disabled:opacity-55",
        className,
      )}
    >
      <S.Thumb
        className={cn(
          "block h-[18px] w-[18px] translate-x-[3px] rounded-full bg-fg shadow-1 transition-transform duration-150",
          "data-[state=checked]:translate-x-[23px] data-[state=checked]:bg-accent-fg",
        )}
      />
    </S.Root>
  );
}
