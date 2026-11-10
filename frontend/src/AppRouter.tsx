import { BrowserRouter, Route, Routes, useMatch, useParams } from 'react-router-dom'
import Root from './Root'
import JoinPage from './features/live/JoinPage'
import PresenterPage from './features/live/PresenterPage'

// The planner serves both `/` and `/events/:eventId` from one element, so creating an event (which replaces the
// URL with /events/{id}) keeps the same App instance and its state. App follows `routeEventId` changes itself.
// Root wraps App with the hash routes of the PDF live-slides demo (#/present/…, #/live/…, #/upload/…).
function PlannerRoute() {
  const match = useMatch('/events/:eventId')
  return <Root routeEventId={match?.params.eventId} />
}

// Route params → props, so pages stay plain components. `key` remounts a page when its param changes.
function JoinRoute() {
  const { code = '' } = useParams()
  return <JoinPage key={code} code={code} />
}

function PresentRoute() {
  const { sessionId = '' } = useParams()
  return <PresenterPage key={sessionId} sessionId={sessionId} />
}

export default function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/join/:code" element={<JoinRoute />} />
        {/* Two "present" URL spaces coexist: path /present/:sessionId is the backend live session; hash #/present/:id (in Root) is the local PDF-slides demo. */}
        <Route path="/present/:sessionId" element={<PresentRoute />} />
        <Route path="*" element={<PlannerRoute />} />
      </Routes>
    </BrowserRouter>
  )
}
