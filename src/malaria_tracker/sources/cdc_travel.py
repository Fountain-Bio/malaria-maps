"""CDC Travelers' Health feed client.

Endpoint returns a SOAP-style XML envelope: a <string> element whose text is the JSON
array of destinations, with HTML field values JSON-escaped (\\u003c ...). We unwrap the
envelope, json.loads the text, and validate into CdcDestination models. A direct-JSON
fallback covers the case where CDC ever serves the array unwrapped.
"""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from .. import __version__
from ..models import CdcDestination

DEFAULT_URL = "https://wwwnc.cdc.gov/travel/Services/xmlservices.asmx/YellowFeverInformationJson"
USER_AGENT = f"malaria-region-tracker/{__version__} (fountain-bio; contact alan@fountain-bio.com)"


@dataclass
class FetchResult:
    raw_bytes: bytes
    sha256: str
    http_status: int
    destinations: list[CdcDestination]


def _unwrap_to_json(raw_text: str) -> list[dict]:
    text = raw_text.lstrip("﻿").strip()
    # Try the XML <string> envelope first.
    if text.startswith("<?xml") or text.startswith("<string"):
        root = ET.fromstring(text)
        inner = root.text or ""
        return json.loads(inner)
    # Fallback: already JSON (possibly wrapped in {"d": [...]}).
    obj = json.loads(text)
    if isinstance(obj, dict) and "d" in obj:
        obj = obj["d"]
        if isinstance(obj, str):
            obj = json.loads(obj)
    return obj


def parse_payload(raw_text: str) -> list[CdcDestination]:
    data = _unwrap_to_json(raw_text)
    if not isinstance(data, list):
        raise ValueError("CDC payload did not decode to a list of destinations")
    return [CdcDestination.model_validate(d) for d in data]


def fetch(url: str = DEFAULT_URL, *, timeout: float = 60.0, retries: int = 3) -> FetchResult:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                             follow_redirects=True)
            raw = resp.content
            sha = hashlib.sha256(raw).hexdigest()
            destinations: list[CdcDestination] = []
            if resp.status_code == 200:
                destinations = parse_payload(raw.decode("utf-8", errors="replace"))
            return FetchResult(raw_bytes=raw, sha256=sha, http_status=resp.status_code,
                               destinations=destinations)
        except (httpx.HTTPError, ET.ParseError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"CDC fetch failed after {retries} attempts: {last_exc}") from last_exc
