import { cn } from '@/lib/cn';

/**
 * 表格：语义化 <table>，表头可粘顶，数字列等宽。
 * 不引入第三方表格库 —— 视图需要的是密度与可控性，不是编辑器级的交互。
 */
export function TableWrap({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("overflow-auto rounded-md border border-line", className)}>
      <table className="w-full border-collapse text-sm tnum">{children}</table>
    </div>
  );
}

export function Th({ className, children, ...rest }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        "sticky top-0 z-[1] whitespace-nowrap border-b border-line bg-surface-2 px-3 py-2 text-left",
        "text-[10.5px] font-medium uppercase tracking-[0.06em] text-fg-mute",
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

export function Td({ className, children, ...rest }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("border-b border-line-faint px-3 py-2 align-middle text-fg-dim", className)} {...rest}>
      {children}
    </td>
  );
}

export function Tr({ className, children, ...rest }: React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr className={cn("transition-colors duration-150 hover:bg-surface-2/60", className)} {...rest}>
      {children}
    </tr>
  );
}
