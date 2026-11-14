# Pages, viewports and states

What the checks leg checks, and what to give `run_inputs.py --kind checks` so it checks the right things. Read the project first (its pages, its scripts, the requests they make); never ask the user what the files already say.

## pages.json

`run_inputs.py` writes it from its options; you never write it by hand.

| Option | Meaning | Read it from |
|---|---|---|
| `--page PATH` (repeat) | A page to open, as a path on the base URL: `/`, `/about.html`, `/cart?step=2`. Default `/` | The site's HTML files, the server's routes, the links in the navigation |
| `--state-route PATH` (repeat) | A data request the page makes, as a path: `/api/products`. The state checks answer it themselves; nothing reaches the server | A `fetch`, `XMLHttpRequest` or framework call in the page's scripts |
| `--loading SELECTOR` | The element shown while the data is on its way: `#loading` | The element the script hides or removes when the data arrives |
| `--list SELECTOR` | The element the data fills: `#product-list` | The element the script appends rows to |
| `--empty SELECTOR` | The message for an empty list, when the page has one | The text the script shows for no data |
| `--error SELECTOR` | The message for a failed request, when the page has one. Without it the check looks for `[role=alert]` | The text the script shows on an error |

A path has no host: the base URL is always `http://127.0.0.1:8080`, the project's own server started inside the run. A page or a route on another host is not a page of this project, and the runner refuses any base URL that is not loopback before a browser starts.

## The viewports

| Project | Size | Device |
|---|---|---|
| `phone-375x812` | 375 x 812 CSS px | mobile, touch, device pixel ratio 2 |
| `tablet-768x1024` | 768 x 1024 CSS px | mobile, touch, device pixel ratio 2 |
| `desktop-1280x800` | 1280 x 800 CSS px | desktop |

Every check runs at all three. A test case's name starts with its viewport: `[phone-375x812] /: no horizontal overflow`.

## The checks

| Check | Fails when | Says nothing about |
|---|---|---|
| Console | A console error, an uncaught page error, a failed request or an HTTP error response while the page loads | Errors after a click or a later request |
| Overflow | The page is wider than the viewport (`scrollWidth` over `clientWidth`); the message names the widest elements and their right edge. The screenshot's sha256, pixel size and byte size are a `screenshot` property of the test case | Content cut off inside a box, text over 200% zoom (WCAG 1.4.4), spacing (1.4.12) |
| Keyboard | An element with a click handler, a button role or a pointer cursor that is not a native control is never reached by Tab | Whether Enter or Space then works, focus order (2.4.3), focus visibility (2.4.7) |
| Email | An email field (typed `email`, or named or `autocomplete`d as one) accepts a value without `@` by the browser's own validation. Skipped without such a field | Validation that runs only on submit; nothing is submitted |
| Loading | No loading indicator while the data request is held, or the indicator stays after the data arrives | |
| Error | Under an HTTP 500 for the data request, the loading indicator stays, or no error message shows | Network failures other than an HTTP status |
| Empty | Under an empty list (`[]`) for the data request, the loading indicator stays, or the list shows nothing and no message says it is empty | Other shapes of empty data |

A state check is skipped, with its reason in the report, when `pages.json` names no data request, names no selector it needs, or the page requests none of the named routes. A skipped check is `not_run` for that page and viewport, never passed.

## Reading the leg's result

- `failed`: read each failed test case from `.qa/evidence/<EXE>/reports/junit.xml`: the page, the viewport and the message. One problem that fails at three viewports is one finding.
- `passed`: "the checks leg ran (EXE-…) and its checks held for <pages> at three viewports". Never "works on phones" beyond what the checks above cover.
- Anything else: `references/tools.md`, "When a tool cannot run".
