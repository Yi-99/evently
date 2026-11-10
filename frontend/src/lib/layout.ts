import type { Task } from '../types'

export const NODE_W = 210
export const NODE_H = 92
// Result sub-nodes that fan out of an opened step.
export const CHILD_W = 310
export const CHILD_H = 84
// A clicked option grows to show what needs doing.
export const CHILD_OPEN_W = 380
export const CHILD_OPEN_H = 650
const GAP_X = 56
const GAP_Y = 24
const CHILD_GAP = 12

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

export interface OpenStep {
  id: string
  count: number
  // false: children are tucked behind the parent; true: fanned out into their own column.
  spread: boolean
  // Index of the expanded child, if any, and its height when not the default.
  focus: number | null
  focusH?: number
}

// Left-to-right layered layout: a task's column is the longest dependency chain before it.
// An open step gets an extra column of children right after it; later columns shift right to make room.
export function layout(tasks: Task[], open: OpenStep | null = null): { boxes: Record<string, Box>; children: Box[] } {
  const byId = new Map(tasks.map((t) => [t.id, t]))
  const depth = new Map<string, number>()

  const visit = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id)!
    if (seen.has(id)) return 0
    seen.add(id)
    const deps = byId.get(id)?.dependsOn.filter((d) => byId.has(d)) ?? []
    const d = deps.length ? Math.max(...deps.map((p) => visit(p, seen))) + 1 : 0
    depth.set(id, d)
    return d
  }
  tasks.forEach((t) => visit(t.id, new Set()))

  const cols: string[][] = []
  tasks.forEach((t) => {
    const d = depth.get(t.id)!
    ;(cols[d] ??= []).push(t.id)
  })

  const openCol = open && byId.has(open.id) ? depth.get(open.id)! : -1
  const childSize = (i: number) =>
    open?.focus === i ? { w: CHILD_OPEN_W, h: open.focusH ?? CHILD_OPEN_H } : { w: CHILD_W, h: CHILD_H }
  const childColW = open?.focus != null ? CHILD_OPEN_W : CHILD_W
  const shift = open?.spread && open.count ? childColW + GAP_X : 0

  const boxes: Record<string, Box> = {}
  cols.forEach((col, c) => {
    const height = col.length * NODE_H + (col.length - 1) * GAP_Y
    const x = c * (NODE_W + GAP_X) + (c > openCol && openCol >= 0 ? shift : 0)
    col.forEach((id, i) => {
      boxes[id] = { x, y: i * (NODE_H + GAP_Y) - height / 2, w: NODE_W, h: NODE_H }
    })
  })

  const children: Box[] = []
  const parent = open && boxes[open.id]
  if (open && parent) {
    const sizes = Array.from({ length: open.count }, (_, i) => childSize(i))
    const total = sizes.reduce((sum, c) => sum + c.h, 0) + (open.count - 1) * CHILD_GAP
    let y = parent.y + NODE_H / 2 - total / 2
    sizes.forEach((c) => {
      children.push(
        open.spread
          ? { x: parent.x + NODE_W + GAP_X, y, ...c }
          : { x: parent.x + (NODE_W - c.w) / 2, y: parent.y + (NODE_H - c.h) / 2, ...c },
      )
      y += c.h + CHILD_GAP
    })
  }
  return { boxes, children }
}
