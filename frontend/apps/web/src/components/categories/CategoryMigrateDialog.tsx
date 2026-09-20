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
import { FolderInput, Loader2 } from 'lucide-react'

interface Props {
  open: boolean
  /** 源分类(workspace 行,带 tx_count)。transfer 是虚拟分类,无需迁移。 */
  source: WorkspaceCategory
  /** 源分类下的子分类数(>0 时禁止"迁完删除源分类")。 */
  sourceChildCount: number
  /** 全量 workspace 分类(组件内部按 kind 过滤 + 排除源分类)。 */
  rows: readonly WorkspaceCategory[]
  iconPreviewUrlByFileId?: Record<string, string>
  saving: boolean
  onConfirm: (target: WorkspaceCategory, deleteSource: boolean) => void
  onClose: () => void
}

/**
 * 分类迁移 —— 把源分类下的全部交易迁到目标分类(对齐 app"先迁移、再删空
 * 分类"的口径;后端按账本逐个迁移,跨账本编排在外层 CategoriesPage)。
 * 目标分类按源 kind 过滤;可勾选迁完后删除源分类(有子分类时不允许)。
 */
export function CategoryMigrateDialog({
  open,
  source,
  sourceChildCount,
  rows,
  iconPreviewUrlByFileId,
  saving,
  onConfirm,
  onClose,
}: Props) {
  const t = useT()
  const [targetId, setTargetId] = useState<string | null>(null)
  const [deleteSource, setDeleteSource] = useState(false)

  useEffect(() => {
    if (!open) return
    setTargetId(null)
    setDeleteSource(false)
  }, [open])

  const kind = source.kind === 'income' ? 'income' : 'expense'
  const candidates = rows.filter((r) => r.kind === kind && r.id !== source.id)
  const target = targetId ? candidates.find((r) => r.id === targetId) ?? null : null
  const txCount = source.tx_count ?? 0
  const isTransfer = source.kind === 'transfer'

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !saving && onClose()}>
      <DialogContent className="max-w-2xl gap-0 p-0">
        <DialogHeader className="border-b border-border/60 px-6 py-4">
          <DialogTitle className="flex items-center gap-2 text-base">
            <FolderInput className="h-4 w-4 text-primary" />
            {t('categories.migrateDialog.title')}
          </DialogTitle>
        </DialogHeader>
        <div className="px-6 py-4 text-sm">
          <p className="mb-1 text-xs text-muted-foreground">
            {t('categories.migrateDialog.source', { name: source.name })}
          </p>
          <p className="mb-3 text-xs text-muted-foreground">
            {isTransfer
              ? t('categories.migrateDialog.transferNoop')
              : t('categories.migrateDialog.body', { count: txCount })}
          </p>
          {!isTransfer ? (
            <div className="-mx-1 max-h-[45vh] overflow-y-auto px-1 py-2">
              <CategorySelector
                kind={kind}
                rows={candidates}
                selectedId={targetId}
                iconPreviewUrlByFileId={iconPreviewUrlByFileId}
                emptyText={t('categories.migrateDialog.noTarget') as string}
                onSelect={(cat) => setTargetId(cat.id)}
              />
            </div>
          ) : null}
          {!isTransfer ? (
            <label className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-primary"
                checked={deleteSource && sourceChildCount === 0}
                disabled={saving || sourceChildCount > 0}
                onChange={(e) => setDeleteSource(e.target.checked)}
              />
              {sourceChildCount > 0
                ? t('categories.migrateDialog.deleteBlocked', { count: sourceChildCount })
                : t('categories.migrateDialog.deleteSource')}
            </label>
          ) : null}
        </div>
        <DialogFooter className="border-t border-border/60 bg-muted/20 px-6 py-3">
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button
            size="sm"
            onClick={() => target && onConfirm(target, deleteSource && sourceChildCount === 0)}
            disabled={saving || isTransfer || !target}
          >
            {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
            {t('categories.migrateDialog.confirm', { count: txCount })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
