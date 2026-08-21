import { Hexagon, ListDashes, Package, Plus, Pulse } from '@phosphor-icons/react'
import { motion, useReducedMotion } from 'framer-motion'
import { NavLink, Outlet } from 'react-router-dom'
import { ThemeToggle } from './ThemeToggle'

export function AppShell() {
  const reduceMotion = useReducedMotion()
  return (
    <div className="app-shell">
      <header className="topbar glass-panel">
        <NavLink to="/" className="brand" aria-label="APK Tracer 首页">
          <span className="brand-mark"><Hexagon weight="fill" size={20} /><Pulse size={13} weight="bold" /></span>
          <span><strong>APK Tracer</strong><small>SECURITY WORKBENCH</small></span>
        </NavLink>
        <nav aria-label="主导航">
          <NavLink to="/" end><ListDashes size={17} />任务</NavLink>
          <NavLink to="/assets"><Package size={17} />资产批量</NavLink>
          <NavLink to="/?create=1"><Plus size={17} />新建分析</NavLink>
        </nav>
        <ThemeToggle />
      </header>
      <motion.main
        className="main-content"
        initial={reduceMotion ? false : { opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35 }}
      >
        <Outlet />
      </motion.main>
      <footer className="app-footer">
        <span>本地分析工作台</span>
        <span className="footer-rule" />
        <span>所有复核操作均可追溯</span>
      </footer>
    </div>
  )
}
