import json
from pathlib import Path
from playwright.sync_api import sync_playwright

def observe(url: str, output: str = "outputs/network.json") -> list[dict]:
    findings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        def on_response(response):
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            content_type = response.headers.get("content-type", "")
            keys = []
            if "json" in content_type:
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        keys = list(body.keys())
                except Exception:
                    pass
            findings.append({"url": response.url, "method": response.request.method,
                "status": response.status, "resource_type": response.request.resource_type,
                "content_type": content_type, "sample_keys": keys})
        page.on("response", on_response)
        page.goto(url, wait_until="networkidle")
        button = page.locator("#next")
        if button.count():
            button.click()
            page.wait_for_load_state("networkidle")
        browser.close()
    dest = Path(output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    return findings
