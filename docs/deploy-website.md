# Running and deploying the ForgeFlow website

The website is a Streamlit app in `web/`. It is a thin presentation layer: every
page calls the same functions the CLI calls. It contains no discovery, ranking,
analysis, or publishing logic of its own.

## Run it locally

```bash
uv sync --extra web
uv run --extra web streamlit run web/app.py
```

Streamlit prints a local URL (usually `http://localhost:8501`). The default mode
is **hosted**, so the database-backed and publishing pages show a locked message.
To use them, run in local mode:

```bash
FORGEFLOW_WEB_MODE=local uv run --extra web streamlit run web/app.py
```

Or use the Make target:

```bash
make web          # hosted mode (the default)
make test-web     # the full suite including the render tests
```

## The two modes

`FORGEFLOW_WEB_MODE` selects the mode. **Hosted is the default when the variable
is unset**, because the safe assumption for a public deployment is that it is
public. A missing or misspelled value degrades to the stricter mode, not the more
powerful one.

| Capability | `hosted` (default) | `local` |
| --- | --- | --- |
| Read the local database (`data/forgeflow.db`) | refused | allowed |
| Execute repository code through the sandbox | refused | allowed, if a runtime exists |
| Import the publisher / read `FORGEFLOW_PUBLISH_TOKEN` | refused | allowed, through the existing guards |
| Use the visitor's own Gemini key | yes, in the session only | yes |
| Use the visitor's own GitHub token | yes, in the session only | yes |
| Fall back to the operator's `GITHUB_TOKEN` | no | yes |

The mode is a hard gate in code, not a label. `web/modes.py` holds three guards
(`require_local` and `require_hosted_context_allowed`), and `web/local.py` calls
`require_local` first in every capability function. A page that forgot to check
still cannot reach a hosted-forbidden action, because the guard is inside the
function, not in the page.

## Deploying to a shared server

Run it in hosted mode (the default, so no variable is needed) and **do not** set
`FORGEFLOW_WEB_MODE=local` on a public host.

What hosted mode guarantees, and what the tests prove:

- **No server-side secrets.** The app reads no model key or GitHub token from the
  environment in hosted mode. A visitor supplies their own key or token, and it
  lives in `st.session_state` for that browser session only. It is never written
  to a file, a database row, a log, or a report.
- **No local database.** Hosted code paths never open `data/forgeflow.db`. The
  refusal happens before the file is opened.
- **No publishing.** `web/views/safety.py` explains the publishing variable in
  prose, but no hosted-reachable module imports `forgeflow.publisher` or
  `forgeflow.publish_token`, and none reads the variable.
- **No repository code execution.** The sandbox page reports the detected backend
  and refuses when there is none.

`tests/test_web_isolation.py` enforces all four. It walks the web sources with
`ast` to prove no hosted module imports the publisher or the token, runs the
capability functions directly to prove they refuse in hosted mode, and replaces
`sqlite3.connect` and `read_publish_token` with tripwires to prove the refusal
happens before either is touched.

### What a public deployment still needs from you

- **A process manager.** Streamlit is a long-running server. Put it behind your
  usual supervisor (systemd, a container, your platform's runner).
- **TLS.** Terminate HTTPS in front of it. A visitor's Gemini key and GitHub token
  travel from their browser to the app; without TLS they are in the clear.
- **Resource limits.** A visitor can ask the app to walk a repository tree and
  fetch files. The walk is bounded (`web/context.py` caps depth, entries, and
  listing calls), but you should still cap memory and CPU per process.
- **A reverse-proxy timeout.** A single model call has a 60-second default
  timeout. Set your proxy's read timeout above that.

### Cost

In hosted mode there is no server-side key, so the operator pays nothing for model
calls; each visitor pays for their own. The per-session caps still apply and are
enforced before the call: 8 model calls, $0.25, and 30 GitHub requests. See
`web/limits.py` and `docs/cost-controls.md`.

## Verifying a deployment

Before you point users at it:

```bash
uv run python -m pytest tests/test_web_isolation.py -q
```

This must pass with no failures. If a hosted refusal test fails, the deployment is
not isolated and must not be exposed.

To check the mode the running app believes it is in, look at the sidebar: it shows
`Hosted` or `Local` and the mode source (`default (unset)`, or the variable).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Validation is locked in hosted mode" | The app is in hosted mode | Set `FORGEFLOW_WEB_MODE=local` and restart, or use the hosted pages |
| `ModuleNotFoundError: No module named 'streamlit'` | The extra is not installed | `uv sync --extra web` |
| Model calls fail with "no Gemini API key" | No key in the session | Paste your key on the AI Analysis page |
| GitHub pages return 403 | No token, and the anonymous rate limit is used up | Supply your own read-only token |
| The tree walk is slow | A very large repository | Expected: the walk is bounded, and it stops at the caps |

## What the website does not do

- It does not write to GitHub. Publishing is local-only and still goes through the
  CLI's approval boundary; the website's publish page, when unlocked in local
  mode, calls the same functions.
- It does not run repository code in hosted mode, and there is no host-execution
  fallback anywhere.
- It does not clone repositories. The context comes from the read-only contents
  API under a strict size cap. See `web/context.py`.
