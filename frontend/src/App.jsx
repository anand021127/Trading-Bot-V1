import React, {useEffect, useState} from 'react'

export default function App(){
  const [health, setHealth] = useState(null)
  const backendUrl = import.meta.env.VITE_BACKEND_URL || '/api'

  useEffect(()=>{
    fetch(`${backendUrl}/health`).then(r=>r.json()).then(setHealth).catch(()=>setHealth({status:'unreachable'}))
  },[])

  return (
    <div className="app">
      <header>
        <h1>Upstox Trading Bot</h1>
      </header>
      <main>
        <section className="card">
          <h2>Backend Health</h2>
          <pre>{JSON.stringify(health, null, 2)}</pre>
        </section>
        <section className="card">
          <h2>Open Trades</h2>
          <p>Coming soon</p>
        </section>
      </main>
    </div>
  )
}
