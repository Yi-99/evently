import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// Feature modules register their task runners before anything renders.
import './features'
import AppRouter from './AppRouter.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppRouter />
  </StrictMode>,
)
