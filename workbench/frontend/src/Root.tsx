import React, { useState } from 'react'
import App from './App'
import Gallery from './Gallery'
import DesignCanvas from './DesignCanvas'
import Admin from './Admin'
import { Template } from './types'

const DEFAULT_TEMPLATE: Template = { id: 'canvas', name: '自由设计画布', subtitle: '自由排版 / 精修', aspect: '1:1', accent: '#7C3AED', level1: 'design', level2: '自由画布', skeleton: 'poster' }

export default function Root() {
  const [screen, setScreen] = useState<'gallery' | 'design' | 'workflow' | 'admin'>('gallery')
  const [template, setTemplate] = useState<Template | null>(null)

  if (screen === 'design' && template) {
    return <DesignCanvas template={template} onBack={() => setScreen('gallery')} />
  }
  if (screen === 'admin') {
    return <Admin onBack={() => setScreen('gallery')} />
  }
  if (screen === 'workflow') {
    return (
      <div style={{ position: 'relative', height: '100vh' }}>
        <App templateName={template?.name} skeleton={template?.skeleton} onBack={() => { setTemplate(null); setScreen('gallery') }} />
      </div>
    )
  }
  return (
    <Gallery
      onUse={(t) => { setTemplate(t); setScreen('workflow') }}
      onWorkflow={() => { setTemplate(null); setScreen('workflow') }}
      onDesign={() => { setTemplate(DEFAULT_TEMPLATE); setScreen('design') }}
      onAdmin={() => setScreen('admin')}
    />
  )
}
