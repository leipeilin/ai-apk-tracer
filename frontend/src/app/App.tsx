import { Navigate, Route, Routes } from 'react-router-dom'
import { RunDetailPage } from '../features/runs/RunDetailPage'
import { RunListPage } from '../features/runs/RunListPage'
import { AppShell } from '../ui/AppShell'

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<RunListPage />} />
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
