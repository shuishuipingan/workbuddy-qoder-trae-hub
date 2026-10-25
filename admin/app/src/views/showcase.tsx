import { useState } from 'react';
import { Inbox, Plus, RefreshCw } from 'lucide-react';
import {
  Badge, Button, Card, Dialog, DialogContent, DialogTrigger, EmptyState, EngineBadge, Field, Input,
  SkeletonCard, SkeletonRows, Switch, TableWrap, Td, Th, Tr, useToast,
} from '@/components/ui';

export default function Showcase() {
  const [busy, setBusy] = useState(false);
  const [on, setOn] = useState(true);
  const toast = useToast();

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">组件陈列</h1>
        <p className="text-sm text-fg-mute mix-space">五态 · 加载态 · 空状态 · 错误态 · 骨架屏</p>
      </header>

      <Card title="按钮（default / hover / active / focus-visible / disabled）" subtitle="Tab 键走一遍看焦点环">
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" icon={<Plus size={16} />}>主操作</Button>
          <Button variant="secondary">次要</Button>
          <Button variant="ghost">幽灵</Button>
          <Button variant="outline">描边</Button>
          <Button variant="danger">危险</Button>
          <Button disabled>禁用</Button>
          <Button
            loading={busy}
            loadingText="提交中"
            onClick={() => { setBusy(true); setTimeout(() => setBusy(false), 1500); }}
          >
            点我加载
          </Button>
        </div>
      </Card>

      <Card title="表单（错误就地提示，不是 toast）">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="正常字段" hint="留空表示继承">
            {(p) => <Input {...p} placeholder="例如 5" />}
          </Field>
          <Field label="错误字段" error="必须是 1–1440 之间的整数" required>
            {(p) => <Input {...p} invalid={p.invalid} defaultValue="abc" />}
          </Field>
          <Field label="禁用字段" hint="不可编辑">
            {(p) => <Input {...p} disabled defaultValue="只读值" />}
          </Field>
        </div>
      </Card>

      <Card title="徽标与开关">
        <div className="flex flex-wrap items-center gap-2">
          <Badge>默认</Badge>
          <Badge tone="accent">强调</Badge>
          <Badge tone="ok">成功</Badge>
          <Badge tone="warn">警告</Badge>
          <Badge tone="err">错误</Badge>
          <Badge tone="info">信息</Badge>
          <span className="mx-1 h-4 w-px bg-line" />
          <EngineBadge engine="wb" label="WorkBuddy" />
          <EngineBadge engine="qd" label="Qoder" />
          <EngineBadge engine="trae" label="Trae" />
          <span className="mx-1 h-4 w-px bg-line" />
          <Switch checked={on} onCheckedChange={setOn} label="示例开关" />
          <Switch checked={false} onCheckedChange={() => {}} label="禁用开关" disabled />
        </div>
      </Card>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="骨架屏（数据未到）">
          <SkeletonCard />
          <div className="mt-4">
            <SkeletonRows rows={3} cols={3} />
          </div>
        </Card>

        <Card title="空状态（图标 + 引导 + 主操作）">
          <EmptyState
            icon={<Inbox size={20} />}
            title="这里还没有账号"
            description="导入一个账号后，模型清单与用量就会自动出现。"
            action={<Button variant="primary" icon={<Plus size={16} />}>导入账号</Button>}
          />
        </Card>
      </div>

      <Card title="表格（粘顶表头 · 等宽数字）">
        <TableWrap>
          <thead>
            <tr>
              <Th>账号</Th>
              <Th>引擎</Th>
              <Th className="text-right">请求数</Th>
              <Th className="text-right">token</Th>
            </tr>
          </thead>
          <tbody>
            {[1, 2, 3].map((n) => (
              <Tr key={n}>
                <Td className="text-fg">account-{n}</Td>
                <Td><EngineBadge engine={n === 2 ? "qd" : "wb"} /></Td>
                <Td className="text-right">{(1234 * n).toLocaleString()}</Td>
                <Td className="text-right">{(98765 * n).toLocaleString()}</Td>
              </Tr>
            ))}
          </tbody>
        </TableWrap>
      </Card>

      <Card title="弹层与轻提示">
        <div className="flex flex-wrap gap-2">
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="secondary">打开对话框</Button>
            </DialogTrigger>
            <DialogContent
              title="确认删除这把 Key？"
              description="删除是一次性的：密钥不再存回，历史用量仍按 id 归因。"
              footer={<><Button variant="ghost">取消</Button><Button variant="danger">删除</Button></>}
            >
              <p className="text-sm text-fg-dim">这里可以放影响说明或二次确认信息。</p>
            </DialogContent>
          </Dialog>
          <Button variant="ghost" icon={<RefreshCw size={16} />} onClick={() => toast.push("ok", "已刷新（这是轻提示，不承担校验）")}>
            触发轻提示
          </Button>
        </div>
      </Card>
    </div>
  );
}
