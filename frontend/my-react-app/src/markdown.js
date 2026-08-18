/* A deliberately small markdown renderer for model output.
 *
 * Escaping runs first and unconditionally: the reply is untrusted text that
 * happens to be displayed as HTML, so nothing the model emits can become
 * markup. Everything after that operates on already-escaped text, which is why
 * the tags this adds are the only tags that can ever reach the DOM.
 *
 * Small on purpose. A full markdown library is a large dependency and a much
 * larger sanitisation problem, for output that is mostly prose and code blocks.
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

// Delimiter for parked code blocks. NUL rather than spaces or brackets: a reply
// containing " 3 " or "[[0]]" would otherwise get that fragment swapped for a
// code block on the way back out. NUL passes through escapeHtml untouched and
// does not occur in model output.
const MARK = String.fromCharCode(0)

export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ESCAPES[c])
}

export function renderMarkdown(text) {
  // Fenced code is extracted first so the inline rules below cannot mangle
  // asterisks, underscores or backticks that are inside a code block.
  const blocks = []
  let out = escapeHtml(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(
      `<pre><code${lang ? ` class="lang-${lang}"` : ''}>${code.replace(/\n$/, '')}</code></pre>`,
    )
    return `${MARK}${blocks.length - 1}${MARK}`
  })

  out = out
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
    .replace(/\n/g, '<br>')

  return out.replace(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'), (_, i) => blocks[Number(i)])
}
