/** Model switch, indexed documents, and session actions. */
export default function Sidebar({
  mode,
  onMode,
  llm,
  account,
  onSignOut,
  documents,
  onRemoveDocument,
  onClearDocuments,
  onNewChat,
  onExport,
}) {
  // What the model panel says depends on which route the next message takes,
  // so the note is derived rather than stored -- it cannot go stale.
  let note = 'Backend unreachable.'
  let warn = true
  if (mode === 'coding') {
    // `/coding` is OpenAI regardless of the active provider, so it can be
    // unavailable while ordinary chat works -- an Ollama box with no key.
    note = llm?.coding_ready
      ? `openai · ${llm.coding_model}`
      : 'Needs an OpenAI API key (OPENAI_API_KEY or OPEN_API).'
    warn = !llm?.coding_ready
  } else if (llm) {
    note = llm.ready ? `${llm.provider} · ${llm.chat_model}` : llm.detail
    warn = !llm.ready
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="mark" aria-hidden="true">A</span>
        <span>
          <span className="brand-name">AI Desktop</span>
          <span className="brand-sub">{account?.username ?? 'chat · documents · crm'}</span>
        </span>
      </div>

      <div className="panel">
        <div className="panel-key">Model</div>
        <div className="seg" role="group" aria-label="Model">
          {[
            ['chat', 'Chat'],
            ['coding', 'Coding'],
          ].map(([value, text]) => (
            <button
              key={value}
              type="button"
              className={`seg-btn${mode === value ? ' is-active' : ''}`}
              onClick={() => onMode(value)}
            >
              {text}
            </button>
          ))}
        </div>
        <p className={`panel-note${warn ? ' warn' : ''}`}>{note}</p>
      </div>

      <div className="panel">
        <div className="panel-key">Documents</div>
        {documents.length === 0 ? (
          <p className="muted">Attach a PDF to search it.</p>
        ) : (
          <>
            {documents.map((doc) => (
              <div className="doc" key={doc.name}>
                <span className="doc-name" title={doc.name}>
                  {doc.name}
                </span>
                <span className="doc-count">{doc.chunks}</span>
                <button
                  className="doc-remove"
                  type="button"
                  title={`Remove ${doc.name}`}
                  onClick={() => onRemoveDocument(doc.name)}
                >
                  ×<span className="sr-only">Remove {doc.name}</span>
                </button>
              </div>
            ))}
            <button className="ghost" type="button" onClick={onClearDocuments}>
              Clear documents
            </button>
          </>
        )}
      </div>

      <div className="panel">
        <div className="panel-key">Session</div>
        <button className="ghost" type="button" onClick={onNewChat}>
          New conversation
        </button>
        <button className="ghost" type="button" onClick={onExport}>
          Export transcript
        </button>
        {/* Absent when auth is off -- there is no session to end, and a button
            that cannot do anything is worse than no button. */}
        {onSignOut && (
          <button className="ghost" type="button" onClick={onSignOut}>
            Sign out
          </button>
        )}
      </div>

      <p className="foot">
        <a href="/docs">API docs</a>
      </p>
    </aside>
  )
}
