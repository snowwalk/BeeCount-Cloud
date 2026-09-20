import { useEffect, useState } from 'react'

import type { WorkspaceCategory } from '@beecount/api-client'
import {
  Button,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  useT,
} from '@beecount/ui'
import { CategorySelector } from '@beecount/web-features'
import { Loader2, Tags } from 'lucide-react'

type Kind = 'expense' | 'income'

interface Props {
  open: boolean
  /** 已选交易中各 kind 的笔数 —— 决定 tab 的可用性与默认选中。 */
  counts: Record<Kind, number>
  rows: readonly WorkspaceCategory[]
  iconPreviewUrlByFileId?: Record<string, string>
  saving: boolean
  onConfirm: (kind: Kind, category: WorkspaceCategory) => void
  onClose: () => void
}

/**
 * 批量修改分类 —— 所选交易可能混合支出/收入/转账,这里按 kind 分 tab,
 * 只有与目标分类同 kind 的交易会被更新(transfer 没有分类,永远不出现)。
 * 后端同样会兜底 kind_mismatch,前端把计数直接展示出来。
 */
export function BatchEditCategoryDialog({
  open,
  counts,
  rows,
  iconPreviewUrlByFileId,
  saving,
  onConfirm,
  onClose,
}: Props) {
  const t = useT()
  const [kind, setKind] = useState<Kind>(() => (counts.income > counts.expense ? 'income' : 'expense'))
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 每次打开时重置:默认选中笔数多的 kind,清空已选分类
  useEffect(() => {
    if (!open) return
    setKind(counts.income > counts.expense ? 'income' : 'expense')
    setSelectedId(null)
  }, [open, counts])

  const selected = selectedId ? rows.find((r) => r.id === selectedId && r.kind === kind) ?? null : null
  const matchedCount = counts[kind]

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !saving && onClose()}>
      <DialogContent className="max-w-2xl gap-0 p-0">
        <DialogHeader className="border-b border-border/60 px-6 py-4">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Tags className="h-4 w-4 text-primary" />
            {t('txBatch.editCategoryDialog.title')}
          </DialogTitle>
        </DialogHeader>
        <div className="px-6 py-4 text-sm">
          {/* kind tab —— 只有出现在选择里的 kind 才可切换 */}
          <div className="mb-3 flex gap-1 rounded-md bg-muted/60 p-1">
            {(['expense', 'income'] as Kind[]).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => {
                  setKind(k)
                  setSelectedId(null)
                }}
                disabled={counts[k] === 0}
                className={`flex-1 rounded px-2 py-1 text-xs transition-colors ${
                  kind === k
                    ? 'bg-background font-medium text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                } disabled:cursor-not-allowed disabled:opacity-40`}
              >
                {t(`txBatch.editCategoryDialog.kind.${k}`)}({counts[k]})
              </button>
            ))}
          </div>
          <p className="mb-3 text-xs text-muted-foreground">
            {matchedCount > 0
              ? t('txBatch.editCategoryDialog.body', { count: matchedCount, kind: t(`txBatch.editCategoryDialog.kind.${kind}`) })
              : t('txBatch.editCategoryDialog.none', { kind: t(`txBatch.editCategoryDialog.kind.${kind}`) })}
          </p>
          <div className="-mx-1 max-h-[50vh] overflow-y-auto px-1 py-2">
            <CategorySelector
              kind={kind}
              rows={rows}
              selectedId={selectedId}
              iconPreviewUrlByFileId={iconPreviewUrlByFileId}
              onSelect={(cat) => setSelectedId(cat.id)}
            />
          </div>
        </div>
        <DialogFooter className="border-t border-border/60 bg-muted/20 px-6 py-3">
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button
            size="sm"
            onClick={() => selected && onConfirm(kind, selected)}
            disabled={saving || !selected || matchedCount === 0}
          >
            {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
            {t('txBatch.editCategoryDialog.confirm', { count: matchedCount })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
