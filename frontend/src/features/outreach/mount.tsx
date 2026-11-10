// Mounts <OutreachPanel> into document.body in its own React root, so App.tsx doesn't have to.
import { createRoot } from 'react-dom/client'
import Host from './Host'
import { patchHistory } from './location'

export function mountOutreachPanel() {
  if (typeof document === 'undefined' || document.getElementById('outreach-root')) return
  patchHistory()
  const el = document.createElement('div')
  el.id = 'outreach-root'
  document.body.appendChild(el)
  createRoot(el).render(<Host />)
}
