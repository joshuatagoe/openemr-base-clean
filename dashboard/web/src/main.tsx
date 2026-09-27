import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { TRANSPORT } from './config';
import { SmartApp } from './smart/SmartApp';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');
// VITE_TRANSPORT picks the build: 'smart' = modes B/C (served by OpenEMR, EHR launch), else mode A (BFF).
createRoot(root).render(<StrictMode>{TRANSPORT === 'smart' ? <SmartApp /> : <App />}</StrictMode>);
