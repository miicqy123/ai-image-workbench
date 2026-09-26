import React, { useState } from 'react'
import App from './App'
import Gallery from './Gallery'
import DesignCanvas from './DesignCanvas'
import Admin from './Admin'
import HomePage from './HomePage'
import ProjectsPage from './ProjectsPage'
import LoginPage from './LoginPage'
import { Template } from './types'
import { Area, Role, ROLES, canArea, roleName } from './auth'

const DEFAULT_TEMPLATE: Template = { id: 'canvas', name: '自由设计画布', subtitle: '自由排版 / 精修', aspect: '1:1', accent: '#7C3AED', level1: 'design', level2: '自由画布', skeleton: 'poster' }

export default function Root() {
  const [role, setRole] = useState<Role | null>(null)
  const [area, setArea] = useState<Area>('creator')
  const [screen, setScreen] = useState<'home' | 'projects' | 'gallery' | 'design' | 'workflow'>('home')
  const [template, setTemplate] = useState<Template | null>(null)

  if (!role) {
    return <LoginPage onLogin={(r) => { setRole(r); setArea(canArea(r, 'creator') ? 'creator' : 'admin') }} />
  }

  const canCreator = canArea(role, 'creator')
  const canAdmin = canArea(role, 'admin')

  const creatorView = (
    <div className="creator-wrap">
      <div className="creator-subnav">
        <button className={`tag-chip ${screen === 'home' ? 'on' : ''}`} onClick={() => setScreen('home')}>首页</button>
        <button className={`tag-chip ${screen === 'projects' ? 'on' : ''}`} onClick={() => setScreen('projects')}>我的项目</button>
        <button className={`tag-chip ${screen === 'gallery' ? 'on' : ''}`} onClick={() => setScreen('gallery')}>模板中心</button>
      </div>
      <div className="creator-main">
        {screen === 'home' ? <HomePage onQuickCreate={() => setScreen('gallery')} onProjects={() => setScreen('projects')} />
          : screen === 'projects' ? <ProjectsPage onOpen={() => setScreen('workflow')} />
          : screen === 'design' && template ? <DesignCanvas template={template} onBack={() => setScreen('gallery')} />
          : screen === 'workflow' ? <div style={{ position: 'relative', height: '100%' }}><App templateName={template?.name} skeleton={template?.skeleton} onBack={() => { setTemplate(null); setScreen('gallery') }} /></div>
          : <Gallery onUse={(t) => { setTemplate(t); setScreen('workflow') }} onWorkflow={() => { setTemplate(null); setScreen('workflow') }} onDesign={() => { setTemplate(DEFAULT_TEMPLATE); setScreen('design') }} onAdmin={canAdmin ? () => setArea('admin') : undefined} />}
      </div>
    </div>
  )

  return (
    <div className="app-shell">
      <div className="app-nav">
        <span className="app-brand">AI 生图工作台</span>
        {canCreator && <button className={`app-tab ${area === 'creator' ? 'on' : ''}`} onClick={() => setArea('creator')}>创作前台</button>}
        {canAdmin && <button className={`app-tab ${area === 'admin' ? 'on' : ''}`} onClick={() => setArea('admin')}>运营后台</button>}
        <div style={{ flex: 1 }} />
        <span className="app-role">{roleName(role)}</span>
        <button className="btn ghost" onClick={() => { setRole(null); setScreen('gallery') }}>退出</button>
      </div>
      <div className="app-body">
        {area === 'admin' && canAdmin ? <Admin onBack={() => setArea(canCreator ? 'creator' : 'admin')} /> : creatorView}
      </div>
    </div>
  )
}
