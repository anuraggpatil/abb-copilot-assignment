/** Test setup: DOM matchers, unmounting between tests, and the one browser API jsdom lacks. */

import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Testing Library only registers its own automatic cleanup when Vitest's globals are enabled, and
// they are not here (tests import `describe`/`it`/`expect` explicitly). Without this, every render
// stays in the document and the second test in a file sees two copies of the composer.
afterEach(cleanup)

// jsdom has no layout, so `Element.scrollTo`/`scrollIntoView` are undefined. The chat thread pins
// itself to the bottom by assigning `scrollTop`, which jsdom does accept — but a component added
// later that calls `scrollIntoView` would fail here for a reason that has nothing to do with it,
// so the stub is worth its two lines.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
