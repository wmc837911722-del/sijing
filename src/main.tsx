import React from 'react';
import ReactDOM from 'react-dom/client';
import './styles.css';
import App from './App';
import './desktop-design.css';
import './pages/pages-design.css';
try {
  document.documentElement.dataset.theme = localStorage.getItem('businessmap.theme') === 'dark' ? 'dark' : 'light';
} catch {
  document.documentElement.dataset.theme = 'light';
}
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
