"""Talks to the opencode HTTP API (opencode serve --port 4096).

Hardened version: never returns a silent empty reply. Provider errors reported inside
the message body (info.error), empty replies, rate limits and 5xx responses are either
retried with backoff or raised, so they show up in the "error" field of predictions.

Optional env vars:
  OPENCODE_URL          default http://localhost:4096
  OPENCODE_PROVIDER_ID  e.g. "google"           (only sent if BOTH are set)
  OPENCODE_MODEL_ID     e.g. "gemini-2.5-flash"
  OPENCODE_RETRIES      default 3
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://localhost:4096")
PROVIDER_ID = os.environ.get("OPENCODE_PROVIDER_ID")
MODEL_ID = os.environ.get("OPENCODE_MODEL_ID")
MAX_RETRIES = int(os.environ.get("OPENCODE_RETRIES", "3"))

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenCodeError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _post(path, body, timeout=120):
    req = urllib.request.Request(
        OPENCODE_URL + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise OpenCodeError(f"OpenCode HTTP {e.code} on {path}: {detail}",
                            retryable=e.code in RETRYABLE_STATUS)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OpenCodeError(f"OpenCode unreachable/timeout on {path}: {e}", retryable=True)


def new_session(title="spider-eval"):
    return _post("/session", {"title": title})["id"]


def _send(session_id, prompt_text, timeout):
    """Send one message. Returns (text, provider_error_or_None)."""
    body = {"parts": [{"type": "text", "text": prompt_text}]}
    if PROVIDER_ID and MODEL_ID:
        body["model"] = {"providerID": PROVIDER_ID, "modelID": MODEL_ID}

    msg = _post(f"/session/{session_id}/message", body, timeout=timeout)

    err = (msg.get("info") or {}).get("error")
    err_text = None
    if err:
        data = err.get("data") or {}
        err_text = f'{err.get("name", "Error")}: {data.get("message") or json.dumps(data)[:300]}'

    parts = msg.get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text, err_text


def ask(session_id, prompt_text, timeout=120):
    last = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            text, err = _send(session_id, prompt_text, timeout)
            if err:
                # provider-side failure (rate limit, auth, bad model...) hidden in a 200 body
                raise OpenCodeError(f"OpenCode model error: {err}", retryable=True)

            if not text.strip():
                # agent finished on a tool call without a final answer: nudge it once
                text, err = _send(
                    session_id,
                    "Now reply with ONLY the final SQL query in a single ```sql fenced code block.",
                    timeout,
                )
                if err:
                    raise OpenCodeError(f"OpenCode model error: {err}", retryable=True)

            if not text.strip():
                raise OpenCodeError("agent returned an empty reply (no text parts)", retryable=True)

            return text
        except OpenCodeError as e:
            last = e
            if e.retryable and attempt < MAX_RETRIES:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                print(f"  [retry {attempt + 1}/{MAX_RETRIES} in {wait}s] {e}")
                time.sleep(wait)
                continue
            raise
    raise last


def extract_sql(text):
    m = re.search(r"```sql\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";").strip()
    m = re.search(r"```\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip().rstrip(";").strip()
    return text.strip().rstrip(";").strip()