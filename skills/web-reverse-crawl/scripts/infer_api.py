#!/usr/bin/env python3
"""Offline REA scenario/HAR adapter for the vendored Browserbase pipeline."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def origin(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Expected HTTP(S) URL without userinfo credentials")
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    port = p.port
    suffix = f":{port}" if port and port != {"http": 80, "https": 443}[p.scheme] else ""
    return f"{p.scheme}://{host}{suffix}"


def body_text(body):
    if body.get("state") != "captured":
        return None
    if body.get("encoding") != "base64":
        raise ValueError("Unsupported REA body encoding")
    data = base64.b64decode(body["content"], validate=True)
    if base64.b64encode(data).decode() != body["content"]:
        raise ValueError("Noncanonical REA body base64")
    if len(data) != body["bytes"] or hashlib.sha256(data).hexdigest() != body["sha256"]:
        raise ValueError("REA body digest/length mismatch")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def rea_records(document):
    if "error" in document:
        raise ValueError("REA error/partial_observation is not a successful capture")
    if "normalized_result" in document and document.get("operation") != "capture_browser_scenario":
        raise ValueError("Expected capture_browser_scenario Evidence")
    capture = document.get("normalized_result", document)
    events = capture["events"]["items"]
    by_sequence = {e["sequence"]: e for e in events}
    if len(by_sequence) != len(events):
        raise ValueError("Duplicate REA event sequence")
    requests, responses, content = {}, {}, {}
    for event in events:
        kind = event["kind"]
        if kind in ("request", "response"):
            target = requests if kind == "request" else responses
            tid = event["transaction_id"]
            if tid in target:
                raise ValueError("Ambiguous REA transaction phase")
            target[tid] = event
        elif kind == "network-content":
            source = by_sequence.get(event["source_event_sequence"], {})
            if (source.get("kind") != event["phase"]
                    or source.get("transaction_id") != event["transaction_id"]
                    or source.get("sequence", event["sequence"]) >= event["sequence"]):
                raise ValueError("REA content references a different/missing transaction phase")
            key = (event["transaction_id"], event["phase"])
            if key in content:
                raise ValueError("Duplicate REA content phase")
            content[key] = (event, body_text(event["body"]))
    rows = []
    for tid, request in requests.items():
        response = responses.get(tid, {})
        req_content, req_text = content.get((tid, "request"), ({}, None))
        resp_content, resp_text = content.get((tid, "response"), ({}, None))
        req_body, resp_body = req_content.get("body", {}), resp_content.get("body", {})
        rows.append({
            "url": request["url"]["url"], "method": request["method"],
            "status": response.get("status"), "type": request["resource_type"],
            "request_body": req_text, "response_body": resp_text,
            "request_mime": req_body.get("media_type"), "response_mime": resp_body.get("media_type"),
            "source": {"transaction_id": tid, "request_sequence": request["sequence"],
                       "response_sequence": response.get("sequence"),
                       "request_body_state": req_body.get("state", "not_recorded"),
                       "response_body_state": resp_body.get("state", "not_recorded")},
        })
    return rows, {"format": "rea-scenario", "evidence_id": document.get("evidence_id"),
                  "completeness": capture.get("completeness"), "limitations": capture.get("limitations", [])}


def har_records(document):
    log = document["log"]
    if log.get("version") != "1.2" or not isinstance(log.get("entries"), list):
        raise ValueError("Expected HAR 1.2 entries")
    rows = []
    for ordinal, entry in enumerate(log["entries"]):
        request, response = entry["request"], entry["response"]
        data = response.get("content", {})
        text = data.get("text")
        if text is not None and data.get("encoding") == "base64":
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8")
            except UnicodeDecodeError:
                text = None
        elif data.get("encoding") not in (None, ""):
            raise ValueError("Unsupported HAR response encoding")
        post = request.get("postData", {})
        rows.append({
            "url": request["url"], "method": request["method"], "status": response["status"],
            "type": entry.get("_resourceType", "other"),
            "request_body": post.get("text"), "response_body": text,
            "request_mime": post.get("mimeType"), "response_mime": data.get("mimeType"),
            "source": {"har_ordinal": ordinal,
                       "request_body_state": "text_present" if "text" in post else "not_recorded",
                       "response_body_state": "text_present" if "text" in data else "not_recorded"},
        })
    return rows, {"format": "har-1.2", "evidence_id": None,
                  "completeness": "unknown", "limitations": ["Historical capture; current replay is unverified."]}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def convert(document, output, allowed_origins):
    rows, metadata = har_records(document) if "log" in document else rea_records(document)
    selected = []
    omissions = {"outside_origins": 0, "missing_response_status": 0}
    for row in rows:
        if origin(row["url"]) not in allowed_origins:
            omissions["outside_origins"] += 1
        elif not isinstance(row["status"], int) or isinstance(row["status"], bool) or not 100 <= row["status"] <= 599:
            omissions["missing_response_status"] += 1
        else:
            selected.append(row)
    if not selected:
        raise ValueError("No observed responses within the selected origins")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    network = output / "cdp" / "network"
    network.mkdir(parents=True)
    requests, responses, sources = [], [], []
    for index, row in enumerate(selected):
        rid = f"adapted-{index:06d}"
        resource_type = {"xhr": "XHR", "fetch": "Fetch", "document": "Document"}.get(row["type"].lower(), "Other")
        req = {"url": row["url"], "method": row["method"], "headers": {}}
        if row["request_mime"]:
            req["headers"]["content-type"] = row["request_mime"]
        if row["request_body"] is not None:
            req["postData"] = row["request_body"]
        resp = {"url": row["url"], "status": row["status"], "headers": {}}
        if row["response_mime"]:
            resp["headers"]["content-type"] = row["response_mime"]
        requests.append({"params": {"requestId": rid, "type": resource_type, "request": req}})
        responses.append({"params": {"requestId": rid, "type": resource_type, "response": resp}})
        bodies = network / "bodies" / rid
        bodies.mkdir(parents=True)
        write_json(bodies / "request.json", {"id": rid, "body": row["request_body"]})
        write_json(bodies / "response.json", {"body": row["response_body"]})
        sources.append({"adapter_pair_id": rid, **row["source"],
                        "request_text_joined": row["request_body"] is not None,
                        "response_text_joined": row["response_body"] is not None})
    for name, events in (("requests", requests), ("responses", responses)):
        (network / f"{name}.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    metadata.update({"selected_records": len(selected), "omissions": omissions,
                     "pairing": "synthetic adapter keys, not original CDP request IDs",
                     "headers": "Only Content-Type is propagated; authentication requirements are unknown.",
                     "origins": sorted(allowed_origins), "records": sources})
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--origin", action="append", required=True)
    args = parser.parse_args()
    if args.capture.stat().st_size > 64 * 1024 * 1024:
        parser.error("Capture exceeds 64 MiB; select relevant records with an existing tool first")
    allowed = set()
    for value in args.origin:
        parsed = urlsplit(value)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            parser.error("--origin must be an exact origin, without a path/query/fragment")
        allowed.add(origin(value))
    node = shutil.which("node")
    if not node:
        parser.error("Node 18+ is required by the upstream inference scripts")
    os.umask(0o077)
    raw = args.capture.read_bytes()
    document = json.loads(raw, object_pairs_hook=unique_object)
    metadata = convert(document, args.out.resolve(), allowed)
    metadata.update({"source_file": str(args.capture.resolve()), "source_sha256": hashlib.sha256(raw).hexdigest()})
    write_json(args.out / "conversion.json", metadata)
    subprocess.run([node, str(Path(__file__).parent / "browserbase" / "discover.mjs"),
                    "--run", str(args.out.resolve())], check=True, timeout=60)
    print(f"Source: {metadata['format']}; {metadata['selected_records']} observed response records converted")
    print("Candidate schemas/client are inferred; live replay and coverage remain to be verified.")


if __name__ == "__main__":
    main()
