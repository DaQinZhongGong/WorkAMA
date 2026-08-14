import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ShareApp } from './ShareApp'
import './styles.css'

createRoot(document.getElementById('app')!).render(<StrictMode><BrowserRouter><ShareApp /></BrowserRouter></StrictMode>)
