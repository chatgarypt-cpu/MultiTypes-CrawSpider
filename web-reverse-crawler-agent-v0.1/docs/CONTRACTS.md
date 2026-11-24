# Module contracts

| Module | Input | Output | Failure behavior |
|---|---|---|---|
| `site.serve` | none | local HTTP demo on 127.0.0.1:8765 | port conflict raises |
| `observe.observe` | approved page URL, output path | list of response metadata, JSON file | browser/network errors propagate |
| REA (external MCP) | user-approved CDP target | evidence with limitations | optional; no fabricated evidence |
| `collect.collect` | approved demo API URL, paths, pagination bounds | count; CSV + SQLite | rejects non-demo API, HTTP errors propagate |
| `cli.main` | serve/observe/collect | console output and artifacts | nonzero on exception |

Network evidence stores URLs, status, content types and JSON top-level keys only; no raw response bodies, cookies or authorization headers.
REA is configured in Codex externally by `npx rea-agents setup`, not invoked by the Python process. This is deliberate: the orchestrator is Codex.

## v0.1 limits
No arbitrary URL crawling, automatic API synthesis, authentication, anti-bot bypass, or unsupervised execution. Local demonstration only. The JS inspection and candidate selection are performed by Codex using the saved evidence and optional REA MCP.
