import { useEffect, useRef } from 'react'
import { renderMarkdown } from '../markdown'

function Message({ role, content, files, error, streaming }) {
  return (
    <article className={`msg ${role}${error ? ' error' : ''}`}>
      {/* A named turn rather than an avatar glyph. Two coloured circles have to
          be learned; "You" and "AI Desktop" are read. */}
      <span className="role">{role === 'assistant' ? 'AI Desktop' : 'You'}</span>
      <div className="body">
        {streaming && !content ? (
          <span className="typing" aria-label="Thinking">
            <i />
            <i />
            <i />
          </span>
        ) : (
          // Safe because renderMarkdown escapes before it adds any tag -- see
          // the note at the top of markdown.js.
          <div className="content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />
        )}

        {files?.length > 0 && (
          <div className="chips">
            {files.map((name) => (
              <span className="chip" key={name}>
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  )
}

/** The conversation, pinned to the newest message. */
export default function Thread({ messages }) {
  const end = useRef(null)

  // Depends on the last message's length, not just the count, so the view keeps
  // pace with a streaming reply instead of only moving once per turn.
  const tail = messages.at(-1)?.content?.length ?? 0
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, tail])

  return (
    <section className="thread" aria-live="polite">
      {messages.map((message, i) => (
        <Message key={i} {...message} />
      ))}
      <div ref={end} />
    </section>
  )
}
