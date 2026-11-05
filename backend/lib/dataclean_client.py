"""DataClean API client — Service Hub side (integration spec 2026-11-05).

The hub's half of the empire-wide DataClean contract (see
``docs/DATACLEAN_INTEGRATION.md``). DataClean is a compute-only cleanup /
dedupe / normalize API hosted by the bookkeeping app:

    POST {base}/cleanup/preview   {kind, rows}            -> mess report
    POST {base}/cleanup/clean     {kind, rows}            -> cleaned rows
    POST {base}/dedupe/scan       {records, records_b?}   -> dupe pairs
    POST {base}/normalize         {type, values}          -> normalized
    GET  {base}/usage                                     -> metering

Safety invariants (non-negotiable, from the spec):

* **Suggestions only.** This client returns suggestions. Nothing in the
  hub ever auto-merges identities or auto-overwrites customer data with
  a cleaned value; applying a suggestion is a human operator decision.
* **Off by default.** Every call raises :class:`DataCleanDisabled`
  unless ``DATACLEAN_ENABLED`` is truthy (``1/true/yes/on``). There are
  no live request-path hooks in the hub — the only caller is the
  operator-invoked report script (``scripts/dataclean_report.py``),
  and even that runs offline in this pass.
* **No secrets in code.** The key comes from ``DATACLEAN_API_KEY``. It
  is minted in the books operator console (``POST /api/dataclean/keys``,
  books JWT with ``ledger:admin``); the plaintext shows once, so live
  mode waits on the owner minting a key and placing it in the hub env.

Chunking (API caps: cleanup 5,000 rows/call, dedupe 500 records/side,
normalize 1,000 values/call): ``preview``/``clean`` split rows into
5,000-row chunks and aggregate; ``normalize`` splits into 1,000-value
chunks; ``dedupe`` splits oversize within-list scans into 500-record
windows **blocked on a contact sort key** (normalized email, else phone
digits, else name) so likely duplicates land in the same window, and
additionally cross-scans each pair of *consecutive* windows so a pair
straddling a window boundary is still seen. Stated limitation of that
blocking strategy: two records whose sort keys differ and that fall in
non-consecutive windows are not compared in live mode (the offline
dry-run in ``lib/dataclean_suggest.py`` has no such limit — it runs the
real engine in-process over the full list).
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "https://bookkeeping-api-ymh6.onrender.com/api/v1/dataclean"
CLEANUP_CHUNK = 5000
DEDUPE_CHUNK = 500
NORMALIZE_CHUNK = 1000

_TRUTHY = ("1", "true", "yes", "on")


class DataCleanError(Exception):
    """Base class for DataClean client errors."""


class DataCleanDisabled(DataCleanError):
    """Raised by every live call while DATACLEAN_ENABLED is off."""


class DataCleanNotConfigured(DataCleanError):
    """Raised when live mode is enabled but no API key is configured."""


class DataCleanAPIError(DataCleanError):
    """The API answered with a non-2xx status."""

    def __init__(self, status, body):
        super().__init__(f"DataClean API error {status}: {body!r:.200}")
        self.status = status
        self.body = body


def enabled() -> bool:
    """Live DataClean calls are allowed only when explicitly enabled."""
    return os.environ.get("DATACLEAN_ENABLED", "0").strip().lower() in _TRUTHY


def api_base() -> str:
    return (os.environ.get("DATACLEAN_API_BASE") or DEFAULT_BASE).rstrip("/")


def api_key() -> str | None:
    return os.environ.get("DATACLEAN_API_KEY") or None


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _block_key(record: dict) -> str:
    """Sort key for dedupe blocking windows (see module docstring)."""
    email = str(record.get("email") or "").strip().lower()
    if email:
        return "e:" + email
    digits = "".join(ch for ch in str(record.get("phone") or "") if ch.isdigit())
    if digits:
        return "p:" + digits[-10:]
    return "n:" + str(record.get("name") or "").strip().lower()


class DataCleanClient:
    """Thin, chunking client for the DataClean API.

    ``transport`` is injectable for tests: a callable
    ``(method, url, headers, payload) -> (status, dict)``. The default
    transport is stdlib ``urllib`` so the client adds no dependency.
    ``enabled`` may be passed explicitly (tests, scripts); by default it
    is read from the environment at call time.
    """

    def __init__(self, base: str | None = None, key: str | None = None,
                 transport=None, timeout: float = 30.0,
                 enabled_override: bool | None = None):
        self.base = (base or api_base()).rstrip("/")
        self.key = key if key is not None else api_key()
        self.transport = transport
        self.timeout = timeout
        self._enabled_override = enabled_override

    # ------------------------------------------------------------ plumbing
    def _require_live(self):
        is_enabled = (self._enabled_override if self._enabled_override
                      is not None else enabled())
        if not is_enabled:
            raise DataCleanDisabled(
                "DATACLEAN_ENABLED is off — DataClean live calls are inert. "
                "Offline dry-run reports (scripts/dataclean_report.py) "
                "remain available and never call the API.")
        if self.transport is None and not self.key:
            raise DataCleanNotConfigured(
                "DATACLEAN_API_KEY is not set. Keys are minted in the "
                "books operator console and placed in the hub env by ops.")

    def _request(self, method: str, path: str, payload: dict | None = None):
        self._require_live()
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        if self.transport is not None:
            status, body = self.transport(method, url, headers, payload)
        else:
            data = (json.dumps(payload).encode() if payload is not None
                    else None)
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    body = json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors="replace")
                try:
                    body = json.loads(raw or "{}")
                except ValueError:
                    body = {"detail": raw}
                raise DataCleanAPIError(exc.code, body) from exc
        if status >= 400:
            raise DataCleanAPIError(status, body)
        return body

    # -------------------------------------------------------------- surface
    def preview(self, kind: str, rows: list[dict]) -> dict:
        """Mess report + schema verdict per 5,000-row chunk. Stores nothing."""
        parts = [self._request("POST", "/cleanup/preview",
                               {"kind": kind, "rows": chunk})
                 for chunk in _chunks(rows, CLEANUP_CHUNK)] or [
                    self._request("POST", "/cleanup/preview",
                                  {"kind": kind, "rows": []})]
        return {"kind": kind, "rows": len(rows), "chunks": len(parts),
                "reports": parts}

    def clean(self, kind: str, rows: list[dict]) -> dict:
        """Cleaned rows + explainable per-field changes, chunk-aggregated."""
        records, changes, by_rule = [], [], {}
        calls = 0
        for chunk in _chunks(rows, CLEANUP_CHUNK):
            resp = self._request("POST", "/cleanup/clean",
                                 {"kind": kind, "rows": chunk})
            calls += 1
            records.extend(resp.get("records") or [])
            changes.extend(resp.get("changes") or [])
            for rule, n in (resp.get("changes_by_rule") or {}).items():
                by_rule[rule] = by_rule.get(rule, 0) + int(n)
        return {"kind": kind, "records": records, "changes": changes,
                "changes_by_rule": by_rule, "chunks": calls}

    @staticmethod
    def _with_ids(records: list[dict]) -> list[dict]:
        """Copies carrying an ``id`` (the API honors it, else row-N)."""
        out = []
        for i, record in enumerate(records):
            copy = dict(record)
            copy.setdefault("id", f"row-{i}")
            out.append(copy)
        return out

    def dedupe(self, records: list[dict],
               records_b: list[dict] | None = None) -> dict:
        """Duplicate *suggestions* — pairs + review pairs, never merges.

        Without ``records_b``: within-list scan. With it: cross mode —
        pairs form only across the two lists.
        """
        records = self._with_ids(records)
        if records_b is not None:
            records_b = self._with_ids(records_b)
            pairs, review = [], []
            for a in _chunks(records, DEDUPE_CHUNK):
                for b in _chunks(records_b, DEDUPE_CHUNK):
                    resp = self._request(
                        "POST", "/dedupe/scan",
                        {"records": a, "records_b": b, "mode": "cross"})
                    pairs.extend(resp.get("pairs") or [])
                    review.extend(resp.get("review_pairs") or [])
            return {"pairs": pairs, "review_pairs": review,
                    "mode": "cross", "blocking": "500-record windows, "
                    "every A window cross-scanned against every B window"}
        if len(records) <= DEDUPE_CHUNK:
            resp = self._request("POST", "/dedupe/scan",
                                 {"records": records})
            return {"pairs": resp.get("pairs") or [],
                    "review_pairs": resp.get("review_pairs") or [],
                    "mode": "within", "blocking": "single window (<=500)"}
        # Oversize within-list: blocked windows + consecutive cross-scans.
        ordered = sorted(records, key=_block_key)
        windows = list(_chunks(ordered, DEDUPE_CHUNK))
        seen: set[tuple] = set()
        pairs, review = [], []

        def absorb(resp, sink):
            for pair in resp.get("pairs") or []:
                key = (pair.get("a_id"), pair.get("b_id"))
                if key not in seen:
                    seen.add(key)
                    sink.append(pair)
            for pair in resp.get("review_pairs") or []:
                key = (pair.get("a_id"), pair.get("b_id"))
                if key not in seen:
                    seen.add(key)
                    review.append(pair)

        for i, window in enumerate(windows):
            absorb(self._request("POST", "/dedupe/scan",
                                 {"records": window}), pairs)
            if i + 1 < len(windows):
                absorb(self._request(
                    "POST", "/dedupe/scan",
                    {"records": window, "records_b": windows[i + 1],
                     "mode": "cross"}), pairs)
        return {"pairs": pairs, "review_pairs": review, "mode": "within",
                "blocking": "500-record windows sorted by contact block "
                            "key (email > phone > name); each window "
                            "scanned within-list plus cross-scanned "
                            "against the next window. Pairs in "
                            "non-consecutive windows are not compared."}

    def normalize(self, type: str, values: list) -> dict:  # noqa: A002
        """Normalize ``values`` of ``type`` (date|phone|address), chunked."""
        results = []
        for chunk in _chunks(values, NORMALIZE_CHUNK):
            resp = self._request("POST", "/normalize",
                                 {"type": type, "values": chunk})
            results.extend(resp.get("results") or [])
        return {"type": type, "results": results}
