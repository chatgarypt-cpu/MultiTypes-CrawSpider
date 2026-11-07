# Codex agent instructions
You are developing Web Reverse Crawler Agent v0.1. First run tests and read docs/CONTRACTS.md.
Only inspect the user-approved local demo at http://127.0.0.1:8765 in v0.1.
Use Playwright to observe Fetch/XHR; use REA MCP for supplementary evidence when installed and explicitly authorized.
Never claim REA was invoked unless the tool call actually succeeded. Do not invent tool outputs.
Do not extract credentials, cookies, tokens, or bypass access controls.
Preserve source evidence, separate confirmed observations from hypotheses, and use small bounded tests.
Run `pytest -q` before claiming completion.
