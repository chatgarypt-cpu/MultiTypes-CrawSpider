# Web Reverse Crawler Agent v0.1

A runnable local demo: news page -> Playwright XHR/Fetch evidence -> reviewed API -> httpx collection -> CSV + SQLite. Codex is the agent/orchestrator; REA is an optional external MCP evidence provider, **not** a Python dependency or automatic call in this demo.

## Requirements
Python 3.10+, Node.js 22.19+/24.11+/26+ and npm for REA, Codex CLI (separately installed), Chrome/Chromium for browser observation.

## Install (macOS/Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
pytest -q
```
Windows PowerShell: `.venv\Scripts\Activate.ps1` instead of `source ...`.

## Run
Terminal A:
```bash
wrc serve
```
Terminal B (activated venv):
```bash
wrc observe
wrc collect
pytest -q
```
Visit http://127.0.0.1:8765 . Inspect `outputs/network.json`, `outputs/articles.csv`, `outputs/articles.db`.
Expected: two Fetch responses (`page=1`, `page=2`) and 12 unique articles after collection.

## Codex + REA setup
```bash
npx rea-agents setup
```
Select Codex and approve configuration changes; restart Codex. REA requires an approved browser CDP endpoint/origin and observation enabled; refer to the version-matched official REA browser observation documentation. Do not blindly expose a debugging port or attach to your personal browsing profile.

Open this project with Codex, and ask:
> Read AGENTS.md and docs/CONTRACTS.md. Start with the local demo. Inspect outputs/network.json and site/index.html; confirm the API path and pagination. If REA MCP is installed, explain the additional browser evidence available and request permission before attaching. Run tests. Report evidence versus hypotheses.

REA integration is manual at this stage: no automatic REA tool call is claimed. The demo does not require REA to run, allowing isolation of crawler and environment issues.

## Scope
Only the loopback demo is authorized in v0.1. To use a real site later, explicitly review permission, rate limits, robots/site terms, and target allowlisting first.
