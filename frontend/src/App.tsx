import React from 'react';
import './index.css';

interface Briefing {
  id: string;
  date: string;
  theme: string;
  excerpt: string;
}

const mockBriefings: Briefing[] = [
  {
    id: 'b1',
    date: 'Sep 4, 2026',
    theme: 'Semiconductors & Clean Energy',
    excerpt: 'Global chip revenues reached $298.5B in Q1, driven by AI data center capex. Meanwhile, clean energy capex accounted for 66% of total global energy expenditures...'
  },
  {
    id: 'b2',
    date: 'Sep 3, 2026',
    theme: 'AI Infrastructure',
    excerpt: 'Persistent demand for AI data center architecture serves as a potential top-line catalyst across the value chain, overcoming short term yield pressures...'
  },
  {
    id: 'b3',
    date: 'Sep 2, 2026',
    theme: 'ESG Compliance in Tech',
    excerpt: 'Global regulatory mandates are shifting from voluntary disclosures to legally enforceable requirements. In the EU, the Corporate Sustainability Reporting Directive...'
  }
];

function App() {
  return (
    <div className="layout">
      <header className="header">
        <h1 className="header-title">Wealth Intelligence Agent</h1>
        <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
          <span style={{ fontSize: '0.9rem', color: '#64748b', fontWeight: '500' }}>Admin Dashboard</span>
          <div style={{ width: '32px', height: '32px', borderRadius: '50%', backgroundColor: '#2563eb', color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 'bold' }}>JD</div>
        </div>
      </header>
      
      <main className="main-content">
        <h2 className="dashboard-title">Morning Briefings</h2>
        
        <div className="briefings-grid">
          {mockBriefings.map((briefing) => (
            <div key={briefing.id} className="briefing-card">
              <span className="badge">{briefing.theme}</span>
              <h3>Daily Research Report</h3>
              <div className="briefing-date">{briefing.date}</div>
              <p className="briefing-excerpt">
                {briefing.excerpt}
              </p>
            </div>
          ))}
        </div>
      </main>
      
      <footer className="footer">
        © 2026 Wealth Intelligence Agent · SEC/FINRA Compliant Reporting Architecture
      </footer>
    </div>
  );
}

export default App;
