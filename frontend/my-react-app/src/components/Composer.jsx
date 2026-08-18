import { useRef, useState } from 'react'

const MAX_HEIGHT = 176

/** The message box: text, staged PDF attachments, and send. */
export default function Composer({ busy, onSend }) {
  const [text, setText] = useState('')
  const [pending, setPending] = useState([])
  const box = useRef(null)
  const picker = useRef(null)

  function grow(el) {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
  }

  function submit(event) {
    event.preventDefault()
    const message = text.trim()
    if (busy || (!message && pending.length === 0)) return

    onSend({ text: message, files: pending })
    setText('')
    setPending([])
    if (box.current) {
      box.current.style.height = 'auto'
      box.current.focus()
    }
  }

  return (
    <form className="composer" onSubmit={submit}>
      {pending.length > 0 && (
        <div className="attachments">
          {pending.map((file, i) => (
            <span className="chip" key={`${file.name}-${i}`}>
              {file.name}
              <button
                type="button"
                title="Remove"
                onClick={() => setPending(pending.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="composer-row">
        <button
          type="button"
          className="attach-btn"
          title="Attach PDFs"
          onClick={() => picker.current?.click()}
        >
          <span aria-hidden="true">＋</span>
          <span className="sr-only">Attach PDFs</span>
        </button>
        <input
          ref={picker}
          type="file"
          accept="application/pdf"
          multiple
          hidden
          onChange={(event) => {
            setPending((current) => [...current, ...Array.from(event.target.files)])
            // Cleared so picking the same file twice still fires onChange.
            event.target.value = ''
          }}
        />

        <textarea
          ref={box}
          rows={1}
          value={text}
          placeholder="Message AI Desktop…"
          onChange={(event) => {
            setText(event.target.value)
            grow(event.target)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit(event)
            }
          }}
        />

        <button className="send" type="submit" disabled={busy} title="Send">
          <span aria-hidden="true">↑</span>
          <span className="sr-only">Send</span>
        </button>
      </div>
      <p className="hint">Enter to send · Shift+Enter for a new line</p>
    </form>
  )
}
