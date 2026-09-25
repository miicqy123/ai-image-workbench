import React, { useState } from 'react'
import App from './App'
import Gallery from './Gallery'
import DesignCanvas from './DesignCanvas'
import { Template } from './types'

export default function Root() {
  const [screen, setScreen] = useState<'gallery' | 'design' | 'workflow'>('gallery')
  const [template, setTemplate] = useState<Template | null>(null)

  if (screen === 'design' && template) {
    return <DesignCanvas template={template} onBack={() => setScreen('gallery')} />
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
      onUse={(t) => { setTemplate(t); setScreen('design') }}
      onWorkflow={() => { setTemplate(null); setScreen('workflow') }}
    />
  )
}
