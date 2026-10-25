import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button } from './button';
import { Field } from './field';
import { Input } from './input';
import { EmptyState } from './empty-state';
import { Inbox } from 'lucide-react';

describe('Button', () => {
  it('loading 时 disabled + aria-busy，点击不再触发', async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(<Button loading onClick={onClick}>提交</Button>);
    const btn = screen.getByRole("button", { name: /提交|提交中/ });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
    await user.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('普通状态可点击，type 默认 button（不误触发 form submit）', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>点我</Button>);
    const btn = screen.getByRole("button", { name: "点我" });
    expect(btn).toHaveAttribute("type", "button");
    await userEvent.setup().click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe('Field + Input', () => {
  it('错误就地展示：role=alert、aria-invalid、aria-describedby 指向错误文本', () => {
    render(
      <Field label="超时" error="必须大于 0" required>
        {(p) => <Input {...p} invalid={p.invalid} />}
      </Field>,
    );
    const input = screen.getByLabelText(/超时/);
    const err = screen.getByRole("alert");
    expect(err).toHaveTextContent("必须大于 0");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input.getAttribute("aria-describedby")).toBe(err.id);
  });

  it('无错误时显示 hint，不渲染 alert', () => {
    render(
      <Field label="备注" hint="可以留空">
        {(p) => <Input {...p} />}
      </Field>,
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("可以留空")).toBeInTheDocument();
  });
});

describe('EmptyState', () => {
  it('三件套齐全：图标 + 标题 + 主操作', () => {
    render(
      <EmptyState
        icon={<Inbox data-testid="ic" />}
        title="还没有账号"
        description="导入后自动出现"
        action={<button type="button">导入</button>}
      />,
    );
    const box = document.querySelector("[data-empty-state]");
    expect(box).toBeTruthy();
    expect(screen.getByTestId("ic")).toBeInTheDocument();
    expect(screen.getByText("还没有账号")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导入" })).toBeInTheDocument();
  });
});
