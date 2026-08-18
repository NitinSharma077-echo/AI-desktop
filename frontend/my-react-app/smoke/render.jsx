/* Renders <App /> once in Node and fails loudly if it throws.
 *
 * Exists because `vite build` and `oxlint` both passed on a bundle that crashed
 * on load with "Cannot access 'de' before initialization" -- a hook dependency
 * array naming a const declared further down the component. Bundlers compile
 * that happily and oxlint has no no-use-before-define rule, so nothing but
 * actually running the component catches it.
 *
 * Only the initial render is exercised: effects do not run under
 * renderToString, so no network is touched and no server needs to be up. That
 * is precisely the render in which the crash happened.
 */
import { renderToString } from 'react-dom/server'
import App from '../src/App.jsx'

// api.js reads these through getters during render paths.
globalThis.localStorage ??= {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
}

renderToString(<App />)
console.log('smoke: <App /> rendered without throwing')
