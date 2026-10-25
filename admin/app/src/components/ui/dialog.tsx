import * as D from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { cn } from '@/lib/cn';

/**
 * 对话框：Radix 保证焦点陷阱 / Esc / 点击遮罩关闭 / aria 关联。
 * 动效走 tokens.css 里的 keyframes（open/closed 两态），reduced-motion 下自动关闭。
 */
export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

export function DialogContent({
  title,
  description,
  children,
  footer,
  className,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}) {
  return (
    <D.Portal>
      <D.Overlay className="hub-overlay fixed inset-0 z-50 bg-black/55 backdrop-blur-[2px]" />
      <D.Content
        className={cn(
          "hub-content fixed left-1/2 top-1/2 z-50 w-[min(560px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2",
          "max-h-[calc(100dvh-4rem)] overflow-auto rounded-xl border border-line bg-overlay p-5 shadow-3",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <D.Title className="text-base font-semibold text-fg">{title}</D.Title>
            {description ? (
              <D.Description className="mt-1 text-xs text-fg-mute mix-space">{description}</D.Description>
            ) : null}
          </div>
          <D.Close
            className="grid h-9 w-9 shrink-0 cursor-pointer place-items-center rounded-md text-fg-mute transition-colors hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            aria-label="关闭"
          >
            <X size={16} aria-hidden="true" />
          </D.Close>
        </div>
        {children ? <div className="mt-4">{children}</div> : null}
        {footer ? <div className="mt-5 flex justify-end gap-2">{footer}</div> : null}
      </D.Content>
    </D.Portal>
  );
}
