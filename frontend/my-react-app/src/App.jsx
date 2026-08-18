import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api'
import Sidebar from './components/Sidebar'
import Thread from './components/Thread'
import Composer from './components/Composer'

/** Toast stack. Nothing here blocks, so failures are reported without a modal. */
function Toasts({ items }) {
  return (
    <div className="toasts" aria-live="polite">
      {items.map((t) => (
        <div className={`toast ${t.kind}`} key={t.id}>
          {t.text}
        </div>
      ))}
    </div>
  )
}

function Pills({ llm, mode, documents, turns }) {
  const chunks = documents.reduce((n, d) => n + d.chunks, 0)
  const coding = mode === 'coding'
  const ready = coding ? true : Boolean(llm?.ready)

  return (
    <div className="pills">
      <span className="pill">
        <span className={`dot ${ready ? 'on' : 'warn'}`} />
        <b>{coding ? 'Gemini' : (llm?.provider ?? '—')}</b>
        {!coding && llm?.chat_model ? ` ${llm.chat_model}` : ''}
      </span>
      <span className="pill">
        <span className={`dot ${documents.length ? 'on' : ''}`} />
        {documents.length} doc{documents.length === 1 ? '' : 's'}
        <b>&nbsp;{chunks}</b>&nbsp;chunks
      </span>
      <span className="pill">
        {turns} turn{turns === 1 ? '' : 's'}
      </span>
    </div>
  )
}

export default function App() {
  const [llm, setLlm] = useState(null)
  const [openAccess, setOpenAccess] = useState(false)
  const [mode, setMode] = useState('chat')
  const [threadId, setThreadId] = useState(() => crypto.randomUUID())
  const [messages, setMessages] = useState([])
  const [documents, setDocuments] = useState([])
  const [busy, setBusy] = useState(false)
  const [toasts, setToasts] = useState([])
  const nextToast = useRef(0)

  const toast = useCallback((text, kind = '') => {
    const id = nextToast.current++
    setToasts((current) => [...current, { id, text, kind }])
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 5000)
  }, [])

  const refreshStatus = useCallback(async () => {
    const status = await api.status()
    setLlm(status?.llm ?? null)
    setOpenAccess(status ? !status.auth.required : false)
  }, [])

  useEffect(() => {
    refreshStatus()
  }, [refreshStatus])

  async function send({ text, files }) {
    setBusy(true)
    try {
      if (files.length) {
        const added = []
        for (const file of files) {
          try {
            const { filename, chunks } = await api.uploadDocument(file)
            added.push({ name: filename, chunks })
            toast(`Indexed ${chunks} chunk(s) from ${filename}`, 'ok')
          } catch (err) {
            toast(`${file.name}: ${err.message}`, 'err')
          }
        }
        if (added.length) setDocuments((current) => [...current, ...added])
      }

      // Attachments on their own are a complete action -- index them and stop,
      // rather than sending the model an empty turn.
      if (!text) return

      setMessages((current) => [
        ...current,
        { role: 'user', content: text, files: files.map((f) => f.name) },
        { role: 'assistant', content: '', streaming: true },
      ])

      const prompt = mode === 'coding' && !/^\/(coding|crm)\b/.test(text) ? `/coding ${text}` : text

      const replace = (patch) =>
        setMessages((current) => {
          const next = current.slice()
          next[next.length - 1] = { ...next[next.length - 1], ...patch }
          return next
        })

      try {
        await api.streamChat({ message: prompt, threadId }, (sofar) => replace({ content: sofar }))
        replace({ streaming: false })
      } catch (err) {
        replace({ content: `Something went wrong: ${err.message}`, streaming: false, error: true })
      }
    } finally {
      setBusy(false)
      refreshStatus()
    }
  }

  async function clearDocuments() {
    try {
      await api.clearDocuments()
      setDocuments([])
      toast('Document index emptied', 'ok')
    } catch (err) {
      toast(err.message, 'err')
    }
  }

  function exportTranscript() {
    if (!messages.length) return toast('Nothing to export yet')
    const text = [`# AI Desktop — ${new Date().toLocaleString()}`, '']
      .concat(messages.map((m) => `**${m.role}**\n\n${m.content}\n`))
      .join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `ai-desktop-${Date.now()}.md`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <>
      <div className="aurora" aria-hidden="true" />

      {openAccess && (
        // Not decoration. With AUTH_REQUIRED off, anyone who can reach this URL
        // can chat, upload, and spend whatever the model provider charges -- so
        // the state is worth seeing every time the page loads, not buried in a
        // log line nobody reads.
        <div className="banner" role="status">
          <b>Open access</b> — authentication is off, so every request runs as a
          shared development user. Set <code>AUTH_REQUIRED=true</code> before this is
          reachable from anywhere but your machine.
        </div>
      )}

      <div className="shell">
        <Sidebar
          mode={mode}
          onMode={setMode}
          llm={llm}
          documents={documents}
          onClearDocuments={clearDocuments}
          onNewChat={() => {
            setMessages([])
            setThreadId(crypto.randomUUID())
            toast('Started a new conversation')
          }}
          onExport={exportTranscript}
        />

        <main className="main">
          <header className="hero">
            <h1>What can I help you build?</h1>
            <p>
              Ask anything. Attach a PDF to ground the answer, or start with <code>/crm</code> to
              reach Zoho.
            </p>
            <Pills
              llm={llm}
              mode={mode}
              documents={documents}
              turns={messages.filter((m) => m.role === 'user').length}
            />
          </header>

          <Thread messages={messages} />
          <Composer busy={busy} onSend={send} />
        </main>
      </div>

      <Toasts items={toasts} />
    </>
  )
}
