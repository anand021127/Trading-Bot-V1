import { useState, useEffect } from 'react';
// Mock dashboard that uses new endpoints
export default function Dashboard() {
  const [scanner, setScanner] = useState(null);
  const [wsDiag, setWsDiag] = useState(null);

  useEffect(() => {
    fetch('/api/scanner/status').then(r => r.json()).then(setScanner);
    fetch('/api/diagnostics/websocket').then(r => r.json()).then(setWsDiag);
  }, []);

  return (
    <div>
      <h1>Dashboard</h1>
      <pre>{JSON.stringify(scanner, null, 2)}</pre>
      <pre>{JSON.stringify(wsDiag, null, 2)}</pre>
    </div>
  );
}
