# Local Web configuration

Horizon currently ships a static Jekyll digest reader and a terminal wizard, with no web configuration service. Add a local browser console without changing the aggregation pipeline or introducing a frontend build system.

## Scope

- Chinese responsive UI: overview/run, AI connection, RSS/GitHub/Hacker News sources, digest controls, advanced JSON, saved digests.
- Existing data/config.json is the source of truth. Form editing preserves unsupported config sections. Provider credentials remain in .env and are never returned by the API. Blank password input preserves the existing key.
- Local-only Docker port 127.0.0.1:18764. No remote access, accounts, automatic schedule, or distribution settings added.
- Explicit Save and Run actions. One background child process at a time, bounded/redacted logs, exit status, stop control. Reuse python -m src.main.
- Existing config validation and profile registry validate saves; revision conflicts protect against stale tabs. Atomic JSON replacement with backup; .env uses in-place writes to support a single-file Docker bind mount.
- Same-origin API with Host/Origin checks, CSRF header for writes, no CORS, no remote assets. Render saved Markdown through a server-side allowlist; keep original text and byte-preserving downloads.
- Stdlib HTTP server intended for localhost only. Existing Docker image packages src/web/static; CLI service remains available.

## Validation

API tests cover persistence, unchanged secret, stale writes, invalid profile/config rejection, request origin/CSRF, process exclusivity, log redaction and summary path validation. Browser checks cover save/reload, source editing, layout, and missing-key behavior. Docker smoke verifies both entry points, mount persistence and health.

## Delivery

Develop in an isolated worktree, merge the reviewed changes into /Users/pandy/ai-code/horizon, preserve current local configuration, rebuild image, start the local web service, then remove the temporary worktree. Package source and deployment files into outputs without credentials or generated private data.

## Verified behavior

Browser checks confirmed rule persistence across reload, RSS addition, advanced unsupported-field preservation, missing-credential errors, digest text viewing, and 390px layout with no horizontal overflow. Secret save and Docker bind-mount persistence were tested with a fictional key only in the development worktree. Original main-workspace configuration is preserved.

## Responses support

`ai.api_protocol` selects `chat_completions` (default, backward compatible) or `responses` for provider `openai`. The provider and protocol are separate UI fields. Other provider types keep their native behavior; incompatible combinations are rejected. Responses uses the installed OpenAI SDK with system/user input messages, JSON text format, max_output_tokens, store=false, output_text aggregation and usage accounting. Incomplete/empty responses are rejected rather than passed downstream as valid news. Only explicitly unsupported temperature triggers a same-protocol retry. Provider chains retain the primary protocol without applying it to unrelated fallback providers.

Tests use the real SDK and httpx.MockTransport to assert the HTTP path and JSON payload. Browser tests verify selecting, saving, reloading, and switching provider types. Production configuration/credentials remain user-controlled.

## Digest reading page

Use a standalone SPA route `#reader/<filename>` instead of the old text-only dialog. Reuse existing Markdown and BeautifulSoup packages. Sanitize document tags, attributes, links and IDs before returning HTML; retain details/summary, source links and original per-item anchors. Build a sidebar TOC, score badges and source/tag styling from sanitized headings and paragraphs. Normalize Horizon's bold-category/immediate-list syntax for Python-Markdown without changing stored files or fenced code.

Reader supports date/language file selection, deep-link reload, back/forward navigation, raw-text mode and exact original downloads. Requests carry generation IDs to prevent rapid navigation from displaying a stale response. Unsupported/empty documents retain a readable fallback layout. Existing config, API keys and digests are not rewritten. Tests cover HTML allowlisting, anchor isolation, formatting, original-byte downloads, invalid files, and the existing API/pipeline rendering behavior. Browser checks cover the real saved digest, directory jumps, reference disclosures, raw mode, download, date switching, narrow layout, refresh and history.
