import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { type Account, type LogLine, api, enc } from '../api'
import { Card, Empty, Seg, cn, usePoll } from '../ui'

const MAX = 2000

function isAgentLine(r: LogLine): boolean {
  return r.kind === 'agent' || r.text.startsWith('调 agent') || r.text.startsWith('agent ') || r.text.includes('已打断') || r.text.includes('连着来的')
}

export function Logs({ account: a }: { account: Account }) {
  const [rows, setRows] = useState<LogLine[]>([])
  const [filter, setFilter] = useState<'all' | 'agent' | 'warn'>('all')
  const box = useRef<HTMLDivElement>(null)
  const since = useRef(0)
  const stick = useRef(true)

  const pull = async () => {
    const fresh = await api<LogLine[]>('GET', `/accounts/${enc(a.id)}/logs?since=${since.current}`)
    if (!fresh.length) return
    since.current = fresh[fresh.length - 1]!.seq
    setRows((cur) => [...cur, ...fresh].slice(-MAX))
  }
  useEffect(() => {
    since.current = 0
    setRows([])
    void pull().catch(() => {})
  }, [a.id])
  usePoll(() => void pull().catch(() => {}), 1500)

  const shown = filter === 'agent'
    ? rows.filter(isAgentLine)
    : filter === 'warn'
      ? rows.filter((r) => r.level !== 'INFO')
      : rows

  useLayoutEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight
  }, [shown.length, filter])

  return (
    <div className="fade-in mx-auto flex h-full w-full max-w-[960px] flex-col px-6 py-8">
      <div className="mb-4 flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-title font-medium">日志</h1>
          <p className="text-small text-fg-3">这个账号收发消息和调用 Agent 的实时步骤记录，保留最近 2000 条。</p>
        </div>
        <Seg
          value={filter}
          onChange={(v) => setFilter(v as 'all' | 'agent' | 'warn')}
          options={[
            { value: 'all', label: '全部' },
            { value: 'agent', label: '仅 Agent' },
            { value: 'warn', label: '告警/错误' },
          ]}
        />
      </div>
      <Card className="min-h-0 flex-1 overflow-hidden">
        {shown.length ? (
          <div
            ref={box}
            className="page-scroll h-full p-4 font-mono text-caption"
            onScroll={(e) => {
              const el = e.currentTarget
              stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 24
            }}
          >
            {shown.map((r) => (
              <div
                key={r.seq}
                className={cn(
                  'break-all whitespace-pre-wrap',
                  isAgentLine(r) && r.level === 'INFO' && 'text-accent',
                  r.level === 'WARNING' && 'text-warning',
                  (r.level === 'ERROR' || r.level === 'CRITICAL') && 'text-danger',
                )}
              >
                <span className="tabular mr-3 text-fg-4">{r.time}</span>
                {r.text}
              </div>
            ))}
          </div>
        ) : (
          <div className="grid h-full place-items-center">
            <Empty title={rows.length ? '当前筛选下暂无日志' : '暂无日志'}>
              {rows.length ? '切换到「全部」查看所有收发记录。' : '启动账号后，这里会实时显示收发消息和 agent 调用。'}
            </Empty>
          </div>
        )}
      </Card>
    </div>
  )
}
