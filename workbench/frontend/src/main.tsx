import React from 'react'
import ReactDOM from 'react-dom/client'
import Root from './Root'
import '@xyflow/react/dist/style.css'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
