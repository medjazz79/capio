"""One token ledger for every engine the app can call.

Providers disagree about where they put their counts. OpenAI-compatible
gateways send `prompt_tokens` / `completion_tokens`, Gemini sends
`promptTokenCount` / `candidatesTokenCount`, Ollama sends `prompt_eval_count`
/ `eval_count`, and gateways add or omit `total`. A new provider therefore
needs no configuration here: `note()` reads whichever shape arrived, and a
model that reports nothing falls back to an estimate that is labelled as one.

Nothing here decides *when* to count. Each translator calls `note()` at the
single point where it has already parsed a successful reply, so a request
that failed or fell back is never counted.

The running totals live in their own file rather than in the settings
document: token counts are telemetry, and writing them must not take the
user's saved settings with them if the disk is full or the write fails. The
application clears this ledger on startup, so totals never carry into a new
application session.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("harmony_translator.translators.usage")

LEDGER_PATH = Path.home() / ".harmony_translator_usage.json"

# Fields kept per row. `estimated` is separate from the counts so a reader can
# tell a measured total from a guessed one without re-deriving it.
_FIELDS = ("in", "out", "reasoning", "cached", "image")
_MAX_ROWS = 60  # trim the longest tail so the file cannot grow without bound
_SAVE_MIN_INTERVAL = 5.0  # seconds; a page is many chunks in quick succession

_lock = threading.Lock()
_totals: dict = {"in": 0, "out": 0, "reasoning": 0, "cached": 0, "image": 0}
_models: dict[str, dict] = {}
_latest: dict[str, dict] = {}
_requests = 0
_last_save = 0.0


def _number(value) -> int:
    """Coerce a provider's count to a non-negative int, or 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _dig(source: dict, *names: str) -> int:
    for name in names:
        if name in source:
            value = _number(source[name])
            if value:
                return value
    return 0


def normalize(usage) -> dict:
    """Read any provider's usage block into our five fields.

    Returns a dict of zeros when nothing recognisable is present, so the
    caller can tell "reported zero" from "reported nothing" by the presence
    of a key.
    """
    if not isinstance(usage, dict):
        return {}

    # "in"/"out"/"estimated" are this module's own names, accepted so an
    # already-normalised row (an estimate) can be fed straight back in.
    prompt = _dig(usage, "prompt_tokens", "input_tokens",
                  "promptTokenCount", "prompt_eval_count", "in")
    completion = _dig(usage, "completion_tokens", "output_tokens",
                      "candidatesTokenCount", "eval_count", "out")
    total = _dig(usage, "total_tokens", "totalTokenCount", "total_eval_count")
    if not total:
        total = prompt + completion
    if not (prompt or completion or total):
        return {}

    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = _dig(prompt_details if isinstance(prompt_details, dict) else {},
                  "cached_tokens")
    cached += _dig(usage, "cached_tokens", "cachedContentTokenCount")
    reasoning = _dig(
        completion_details if isinstance(completion_details, dict) else {},
        "reasoning_tokens",
    )
    reasoning += _dig(usage, "reasoning_tokens", "thoughtsTokenCount")
    image = _dig(
        completion_details if isinstance(completion_details, dict) else {},
        "image_tokens",
    )
    image += _dig(usage, "image_tokens", "modalityTokenCount")

    return {
        "in": prompt,
        "out": completion,
        "reasoning": reasoning,
        "cached": cached,
        "image": image,
        # `total` is kept so a provider's own number wins over prompt+completion
        # when the two disagree; the running total sums this field.
        "total": total,
    }


def estimate(prompt_text: str = "", completion_text: str = "") -> dict:
    """A rough count for providers that report nothing.

    Four characters per token is the usual English approximation. It is
    marked so the UI can show it differently from a measured count — an
    estimate that looked exact would be worse than none.
    """
    rows = {
        "in": max(0, len(prompt_text)) // 4,
        "out": max(0, len(completion_text)) // 4,
        "reasoning": 0,
        "cached": 0,
        "image": 0,
        "estimated": True,
    }
    rows["total"] = rows["in"] + rows["out"]
    return rows


def note(model: str, usage, provider: str = "") -> dict:
    """Add one request's counts to the ledger. Returns the normalised row.

    Call this once per request, with that request's totals. Streaming
    endpoints disagree about how often they repeat the block — OpenAI sends
    one `usage` frame at the end, Gemini re-sends the same `usageMetadata` in
    every chunk — so a caller that forwards each frame would multiply a real
    figure by the length of the reply. Collect the latest block in the loop
    and call this once when the stream ends.

    The newest row is also remembered per (provider, model) so a badge can
    show this request's numbers without the caller having to thread a report
    dictionary through a generator that yields deltas.
    """
    global _requests
    rows = normalize(usage)
    if not rows:
        return {}
    with _lock:
        _requests += 1
        key = (provider or "?") + "|" + (model or "?")
        row = _models.get(key)
        if row is None:
            row = {"provider": provider, "model": model,
                   "in": 0, "out": 0, "reasoning": 0, "cached": 0,
                   "image": 0, "total": 0, "requests": 0, "estimated": False}
            _models[key] = row
        row["requests"] += 1
        for field in _FIELDS:
            row[field] = _number(row.get(field)) + _number(rows.get(field))
        row["total"] = _number(row.get("total")) + _number(rows.get("total"))
        if rows.get("estimated") or usage.get("estimated"):
            row["estimated"] = True
        _latest[key] = dict(rows)
        for field in _FIELDS:
            _totals[field] = _number(_totals.get(field)) + _number(
                rows.get(field))
    return rows


def note_estimate(model: str, provider: str = "",
                  prompt_text: str = "", completion_text: str = "") -> dict:
    """Record an explicitly-labelled estimate for a provider that reports none."""
    return note(model, estimate(prompt_text, completion_text), provider)


def last(model: str, provider: str = "") -> dict:
    """The most recent single request's counts for this model, or {}.

    This is what a badge reads: the numbers for the reply it is labelling,
    not the running total, which would be the sum of everything so far.
    """
    with _lock:
        for key, row in reversed(list(_latest.items())):
            head, _, name = key.partition("|")
            if name == (model or "") and (not provider or head == provider):
                return dict(row)
    return {}


def accumulated(model: str, provider: str = "") -> dict:
    """Running total for one model, across every request since the counter
    was last reset.

    `last()` is the newest request; this is the sum for that same model, which
    is what a reader wants while working through a book: what has this model
    cost me so far, not what did the paragraph I just selected cost.
    """
    with _lock:
        for key, row in _models.items():
            head, _, name = key.partition("|")
            if name == (model or "") and (not provider or head == provider):
                return dict(row)
    return {}


def snapshot() -> dict:
    """A copy of the current totals. Safe to call from any thread."""
    with _lock:
        return {
            "totals": dict(_totals),
            "total": sum(_number(_totals.get(f)) for f in _FIELDS),
            "requests": _requests,
            "models": [
                dict(row) for row in
                sorted(_models.values(), key=lambda r: -_number(r.get("total")))
            ],
        }


def reset() -> None:
    """Clear the in-memory ledger and remove its file."""
    global _requests, _last_save
    with _lock:
        _totals.update({field: 0 for field in _FIELDS})
        _models.clear()
        _latest.clear()
        _requests = 0
        _last_save = 0.0
    try:
        LEDGER_PATH.unlink()
    except (OSError, AttributeError):
        pass


def start_session() -> None:
    """Start a fresh token-counting session for a new application run."""
    reset()


def load() -> None:
    """Restore the previous totals so the number survives a restart."""
    global _requests
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    with _lock:
        totals = raw.get("totals")
        if isinstance(totals, dict):
            for field in _FIELDS:
                _totals[field] = _number(totals.get(field))
        rows = raw.get("models")
        if isinstance(rows, list):
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                key = f"{entry.get('provider', '?')}|{entry.get('model', '?')}"
                _models[key] = {
                    "provider": str(entry.get("provider", "")),
                    "model": str(entry.get("model", "")),
                    **{field: _number(entry.get(field)) for field in _FIELDS},
                    "total": _number(entry.get("total")),
                    "requests": _number(entry.get("requests")),
                    "estimated": bool(entry.get("estimated")),
                }
        _requests = _number(raw.get("requests"))


def save(force: bool = False) -> None:
    """Write the ledger out, at most once per few seconds unless forced.

    Same write-then-rename dance as the settings file, so a crash mid-write
    cannot leave a half-written ledger behind.
    """
    global _last_save
    import time

    now = time.monotonic()
    if not force and now - _last_save < _SAVE_MIN_INTERVAL:
        return
    _last_save = now
    payload = snapshot()
    payload["version"] = 1
    if len(payload["models"]) > _MAX_ROWS:
        payload["models"] = payload["models"][:_MAX_ROWS]
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(LEDGER_PATH.parent), prefix=".usage-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(temporary, LEDGER_PATH)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as error:
        # Telemetry must never take the app down with it.
        log.debug("could not write the token ledger: %s", error)


load()
