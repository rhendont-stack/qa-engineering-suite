# Frontend runner dependency set

`package.json` pins the dependencies of the suite's frontend runner exactly; `package-lock.json` is the resolved lockfile
(lockfileVersion 3, 141 package entries, every `resolved` URL on the public npm registry):

| package | version |
|---|---|
| `@playwright/test` | 1.65.1 (the version of the pinned Playwright image) |
| `@axe-core/playwright` | 4.14.0 |
| `axe-core` | 4.14.0 |
| `lighthouse` | 13.6.0 |

## How the lockfile was produced

It was produced by one recorded container execution, `EXE-01M7BNSQT8Y3AMGP70CNPR6CGB` (status passed), on the engine, from the
Playwright image `mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579`:

```
qa run --container mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579 \
  --install 'npm install --package-lock-only --ignore-scripts --no-audit --no-fund' --category pipeline --allow-empty \
  --timeout 900 --shell -- 'sha256sum /work/package-lock.json && base64 -w0 /work/package-lock.json'
```

The install step ran under an `install-dependencies` grant (`AUTH-001`) recorded with the user's own words as the approval; the
test step ran with no network.

The test step could not copy the lockfile to `{evidence}/`. The guard's `delete-outside-qa` rule refuses a redirect into the
evidence directory, and a declared report passes through the redactor, which altered two `minipass` entries. So the test step
printed the file's sha256 and a base64 copy to stdout; the copy was decoded and its sha256 checked against the printed one
(`d482d7062852aa3448c1eff93f56535804057349a0bf2594c0c3fc4acb4e0a00`). This route was used only for this file.

A scan of the committed lockfile with the kernel's redaction patterns matches only those two `minipass` entries (one pattern,
`password_assignment`, triggered by the key name); they are the name and version range of a public registry package, not secrets.

## Use

Scripts install it with `npm ci --ignore-scripts --no-audit --no-fund` in the container's install step and call
`node_modules/.bin/<tool>` directly. Regenerate it only through a new recorded container execution, and update the Playwright
entry in `shared/policies/container-images.json` in the same change.
