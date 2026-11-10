import { useEffect, useState } from 'react'
import App from './App.tsx'
import Audience from './components/live/Audience.tsx'
import Presenter from './components/live/Presenter.tsx'
import Upload from './components/live/Upload.tsx'

// Live slides have their own URLs (#/present/new or #/upload/:id to upload, #/present/:id, #/live/:id) so they can open in a new tab.
// `routeEventId`: the backend event from the /events/:eventId path route (see AppRouter), passed through to App.
export default function Root({ routeEventId }: { routeEventId?: string }) {
  const [hash, setHash] = useState(location.hash)
  useEffect(() => {
    const onChange = () => setHash(location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const [, view, id] = hash.match(/^#\/(present|live|upload)\/([\w-]+)$/) ?? []
  if (view === 'present' && id === 'new') return <Upload />
  if (view === 'upload') return <Upload key={id} deckId={id} />
  if (view === 'present') return <Presenter key={id} id={id} />
  if (view === 'live') return <Audience key={id} id={id} />
  return <App routeEventId={routeEventId} />
}
