"""Portable release policy and pure CAS ledger transitions (no registry or Git I/O).

Canonical JSON: UTF-8, sorted keys, separators ','/':', ensure_ascii=False;
only null, bool, arbitrary-precision int, Unicode scalar strings, lists and
string-keyed dicts. Floats and duplicate parsed keys are rejected.
A verification string identifies caller-owned evidence: this module does not
establish that bytes were actually pushed, pulled or cryptographically checked.
"""
import copy
import hashlib
import json
import re

VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SEVERITY = {"app_patch": 1, "app_minor": 1, "base_patch": 1,
            "dependency": 1, "material": 1, "rebuild": 1,
            "app_major": 2, "base_minor": 2, "base_major": 3, "breaking": 3}
STATE_KEYS = {"schema", "revision", "desired", "intents", "counters", "entries", "channels", "source_tags"}
PAYLOAD_KEYS = {"image", "channel", "source_tag", "source_commit", "material_key",
                "plan_hash", "desired_generation", "causes", "requires_base"}


class PolicyError(ValueError):
    """Malformed policy input or invalid release transition."""


class Conflict(PolicyError):
    """Stale revision, generation, immutable identity or allocation conflict."""


def _require(ok, message):
    if not ok:
        raise PolicyError(message)


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def parse_version(text):
    _require(_matches(VERSION, text), "version must be canonical X.Y.Z")
    return tuple(int(part) for part in text.split("."))


def _tag(tag):
    _require(type(tag) is str and tag.startswith("v"), "source tag must start with v")
    return parse_version(tag[1:])


def allocate_source(requested, existing_tags):
    major, minor, patch = parse_version(requested)
    _require(type(existing_tags) is list, "existing_tags must be a list")
    highest = -1
    for tag in existing_tags:
        _require(type(tag) is str, "tag must be a string")
        if "/" in tag:  # Explicit fixture namespaces do not occupy source versions.
            continue
        if not tag.startswith("v"):
            continue
        try:
            found = _tag(tag)
        except PolicyError as error:
            raise PolicyError("ambiguous malformed release tag: " + tag) from error
        if found[:2] == (major, minor):
            highest = max(highest, found[2])
    return f"v{major}.{minor}.{max(patch, highest + 1)}"


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        _require(key not in out, "duplicate JSON key: " + key)
        out[key] = value
    return out


def _bad_number(value):
    raise PolicyError("floating point and nonfinite JSON numbers are forbidden")


def strict_json_loads(text):
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_float=_bad_number, parse_constant=_bad_number)
    except (ValueError, TypeError) as error:
        raise PolicyError(str(error)) from error
    canonical_hash(value)  # Also rejects unpaired surrogates.
    return value


def parse_request(body):
    _require(type(body) is str, "PR body must be text")
    marker = "<!-- release-request -->"
    lines = body.splitlines()
    indexes = [i for i, line in enumerate(lines) if line == marker]
    _require(len(indexes) == 1 and body.count(marker) == 1, "exactly one release-request marker required")
    i = indexes[0]
    _require(i + 1 < len(lines) and lines[i + 1] == "```json", "marker must introduce a JSON fence")
    start = i + 2
    endings = [j for j in range(start, len(lines)) if lines[j] == "```"]
    _require(bool(endings), "missing closing JSON fence")
    value = strict_json_loads("\n".join(lines[start:endings[0]]))
    _require(type(value) is dict and set(value) == {"component", "requested_version", "release_kind"},
             "request must have exactly component, requested_version, release_kind")
    _require(value["component"] in ("base", "app-a", "app-b"), "unknown component")
    _require(value["release_kind"] in ("source", "promotion", "integration"), "unknown release kind")
    parse_version(value["requested_version"])
    return value


def _json_value(value):
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise PolicyError("Unicode strings must contain scalar values") from error
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, "JSON object keys must be strings")
            _json_value(key)
            _json_value(item)
        return
    raise PolicyError("noncanonical JSON value: " + type(value).__name__)


def canonical_hash(value):
    _json_value(value)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def release_bump(previous, causes):
    major, minor, patch = parse_version(previous)
    _require(type(causes) is list and all(type(x) is str and x in SEVERITY for x in causes),
             "unknown release cause")
    level = max((SEVERITY[x] for x in causes), default=0)
    if level == 0:
        return None
    if level == 3:
        return f"{major + 1}.0.0"
    if level == 2:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def new_state():
    return {"schema": 1, "revision": 0, "desired": {}, "intents": {},
            "counters": {}, "entries": {}, "channels": {}, "source_tags": {}}


def _slot(image, channel):
    _require(_matches(IDENTIFIER, image) and _matches(IDENTIFIER, channel), "unsafe image/channel")
    return image + "/" + channel


def _positive(value):
    return type(value) is int and value > 0


def _evidence(value):
    _require(type(value) is str and 0 < len(value) <= 2048 and value == value.strip(),
             "verification evidence identifier required")


def _payload(payload):
    _require(type(payload) is dict and set(payload) == PAYLOAD_KEYS, "invalid intent payload schema")
    slot = _slot(payload["image"], payload["channel"])
    _tag(payload["source_tag"])
    _require(_matches(COMMIT, payload["source_commit"]), "full Git commit required")
    for key in ("material_key", "plan_hash"):
        _require(_matches(HEX64, payload[key]), key + " must be full SHA256")
    _require(_positive(payload["desired_generation"]), "positive desired generation required")
    _require(type(payload["requires_base"]) is bool, "requires_base must be boolean")
    causes = payload["causes"]
    release_bump("0.0.0", causes)
    _require(bool(causes) and len(set(causes)) == len(causes), "unique nonempty release causes required")
    return slot


def validate_state(state):
    """Reject malformed or unsupported persisted state before any transition."""
    _require(type(state) is dict and set(state) == STATE_KEYS, "invalid ledger schema")
    _require(type(state["schema"]) is int and state["schema"] == 1, "unsupported ledger schema")
    _require(type(state["revision"]) is int and state["revision"] >= 0, "invalid ledger revision")
    canonical_hash(state)
    for key in STATE_KEYS - {"schema", "revision"}:
        _require(type(state[key]) is dict, key + " must be an object")
    for slot in set(state["desired"]) | set(state["counters"]) | set(state["entries"]) | set(state["channels"]):
        parts = slot.split("/")
        _require(len(parts) == 2, "invalid slot")
        _slot(*parts)
    for desired in state["desired"].values():
        _require(type(desired) is dict and set(desired) == {"generation", "material_key"}, "invalid desired schema")
        _require(_positive(desired["generation"]) and _matches(HEX64, desired["material_key"]), "invalid desired value")
    for tag, commit_sha in state["source_tags"].items():
        _tag(tag)
        _require(_matches(COMMIT, commit_sha), "invalid source tag commit")
    for slot, counter in state["counters"].items():
        _require(type(counter) is dict and set(counter) == {"serial", "last_version"}, "invalid counter")
        _require(type(counter["serial"]) is int and counter["serial"] >= 0, "invalid allocation serial")
        parse_version(counter["last_version"])
        _require(slot in state["entries"] and bool(state["entries"][slot]), "counter missing explicit history")
    for slot, entries in state["entries"].items():
        _require(type(entries) is dict and bool(entries) and slot in state["counters"], "invalid history")
        for version, entry in entries.items():
            parse_version(version)
            _require(type(entry) is dict and set(entry) == {"digest", "verification", "intent_id", "material_key", "serial"}, "invalid index entry")
            _require(_matches(DIGEST, entry["digest"]) and _matches(HEX64, entry["material_key"]), "invalid index digest/material")
            _evidence(entry["verification"])
            _require(type(entry["serial"]) is int and 0 <= entry["serial"] <= state["counters"][slot]["serial"], "invalid entry serial")
            _require(parse_version(version) <= parse_version(state["counters"][slot]["last_version"]), "history exceeds allocation")
            if entry["serial"] == 0:
                _require(entry["intent_id"] is None, "bootstrap cannot claim a build intent")
            else:
                owner = state["intents"].get(entry["intent_id"]) if type(entry["intent_id"]) is str else None
                _require(type(owner) is dict and owner.get("status") == "committed", "index entry missing committed intent")
                _require(owner.get("version") == version and owner.get("serial") == entry["serial"]
                         and owner.get("digest") == entry["digest"] and owner.get("verification") == entry["verification"],
                         "index entry differs from verified intent")
        _require(sum(entry["serial"] == 0 for entry in entries.values()) == 1, "history requires exactly one bootstrap")
    _require(set(state["channels"]) == set(state["entries"]), "channel/history mismatch")
    for slot, selected in state["channels"].items():
        _require(type(selected) is str and selected in state["entries"][slot], "channel selects missing history")
    reserved = set()
    for intent_id, intent in state["intents"].items():
        _require(_matches(IDENTIFIER, intent_id), "unsafe intent ID")
        _require(type(intent) is dict and set(intent) == {"payload", "payload_hash", "status", "base_digest", "version", "serial", "digest", "verification", "abort_reason"}, "invalid intent schema")
        slot = _payload(intent["payload"])
        _require(intent["payload_hash"] == canonical_hash(intent["payload"]), "intent payload hash mismatch")
        _require(state["source_tags"].get(intent["payload"]["source_tag"]) == intent["payload"]["source_commit"], "source tag binding mismatch")
        status = intent["status"]
        _require(status in ("prepared", "reserved", "published", "committed", "aborted"), "invalid intent status")
        _require(intent["base_digest"] is None or (intent["payload"]["requires_base"] and _matches(DIGEST, intent["base_digest"])), "invalid accepted base")
        desired = state["desired"].get(slot)
        _require(desired is not None and intent["payload"]["desired_generation"] <= desired["generation"], "intent has impossible generation")
        if intent["payload"]["desired_generation"] == desired["generation"]:
            _require(intent["payload"]["material_key"] == desired["material_key"], "intent/desired material mismatch")
        if intent["version"] is not None:
            parse_version(intent["version"])
            _require(slot in state["counters"] and _positive(intent["serial"]) and intent["serial"] <= state["counters"][slot]["serial"], "invalid reservation")
            _require((slot, intent["version"]) not in reserved, "duplicate reserved version")
            reserved.add((slot, intent["version"]))
            _require(not intent["payload"]["requires_base"] or intent["base_digest"] is not None, "reserved intent missing base")
            _require(status != "prepared", "prepared intent already allocated")
        else:
            _require(intent["serial"] is None and status in ("prepared", "aborted"), "missing reservation")
        if intent["digest"] is not None:
            _require(_matches(DIGEST, intent["digest"]) and intent["version"] is not None, "invalid publication digest")
            _evidence(intent["verification"])
            _require(status in ("published", "committed", "aborted"), "publication/status mismatch")
        else:
            _require(intent["verification"] is None and status not in ("published", "committed"), "missing verified publication")
        _require((status == "aborted") == (intent["abort_reason"] is not None), "invalid abort reason")
        if status == "aborted":
            _evidence(intent["abort_reason"])
        if status == "committed":
            entry = state["entries"].get(slot, {}).get(intent["version"], {})
            _require(entry.get("intent_id") == intent_id and entry.get("digest") == intent["digest"]
                     and entry.get("material_key") == intent["payload"]["material_key"], "committed intent/history mismatch")
    for slot, counter in state["counters"].items():
        allocations = [intent for intent in state["intents"].values()
                       if _slot(intent["payload"]["image"], intent["payload"]["channel"]) == slot and intent["serial"] is not None]
        serials = sorted(intent["serial"] for intent in allocations)
        _require(serials == list(range(1, counter["serial"] + 1)), "allocation history has missing/duplicate serials")
        ordered = sorted(allocations, key=lambda intent: intent["serial"])
        initial = next(version for version, entry in state["entries"][slot].items() if entry["serial"] == 0)
        versions = [initial] + [intent["version"] for intent in ordered]
        _require(all(parse_version(a) < parse_version(b) for a, b in zip(versions, versions[1:])), "allocation versions must increase")
        _require(counter["last_version"] == versions[-1], "counter differs from allocation history")
    return state


def _begin(state, expected_revision):
    validate_state(state)
    _require(type(expected_revision) is int, "expected_revision must be an integer")
    if state["revision"] != expected_revision:
        raise Conflict("stale ledger revision")
    return copy.deepcopy(state)


def _done(before, after):
    if before != after:
        after["revision"] = before["revision"] + 1
    validate_state(after)
    return after


def _intent(state, intent_id):
    _require(type(intent_id) is str and intent_id in state["intents"], "unknown intent")
    return state["intents"][intent_id]


def _current(state, payload):
    slot = _slot(payload["image"], payload["channel"])
    desired = state["desired"].get(slot)
    if desired != {"generation": payload["desired_generation"], "material_key": payload["material_key"]}:
        raise Conflict("stale desired generation or material")
    return slot


def set_desired(state, image, channel, material_key, *, expected_revision):
    after = _begin(state, expected_revision)
    slot = _slot(image, channel)
    _require(_matches(HEX64, material_key), "material_key must be full SHA256")
    previous = after["desired"].get(slot)
    if previous is None or previous["material_key"] != material_key:
        after["desired"][slot] = {"generation": (previous["generation"] if previous else 0) + 1,
                                  "material_key": material_key}
    return _done(state, after)


def bootstrap(state, image, channel, version, digest, verification, *, expected_revision):
    after = _begin(state, expected_revision)
    slot = _slot(image, channel)
    parse_version(version)
    _require(_matches(DIGEST, digest), "verified digest must be SHA256")
    _evidence(verification)
    _require(slot in after["desired"], "set desired material before explicit bootstrap")
    _require(slot not in after["entries"] and slot not in after["counters"], "history already initialized")
    after["counters"][slot] = {"serial": 0, "last_version": version}
    after["entries"][slot] = {version: {"digest": digest, "verification": verification,
        "intent_id": None, "material_key": after["desired"][slot]["material_key"], "serial": 0}}
    after["channels"][slot] = version
    return _done(state, after)


def prepare(state, intent_id, payload, *, expected_revision):
    after = _begin(state, expected_revision)
    _require(_matches(IDENTIFIER, intent_id), "unsafe intent ID")
    slot = _payload(payload)
    old = after["intents"].get(intent_id)
    if old is not None:
        if old["payload"] != payload:
            raise Conflict("intent ID reused with different payload")
        return after
    _current(after, payload)
    _require(slot in after["counters"], "missing explicit release history")
    old_sha = after["source_tags"].get(payload["source_tag"])
    if old_sha is not None and old_sha != payload["source_commit"]:
        raise Conflict("source tag already bound to another commit")
    after["source_tags"][payload["source_tag"]] = payload["source_commit"]
    after["intents"][intent_id] = {"payload": copy.deepcopy(payload), "payload_hash": canonical_hash(payload),
        "status": "prepared", "base_digest": None, "version": None, "serial": None,
        "digest": None, "verification": None, "abort_reason": None}
    return _done(state, after)


def accept_base(state, intent_id, digest, *, expected_revision):
    after = _begin(state, expected_revision)
    intent = _intent(after, intent_id)
    _require(intent["payload"]["requires_base"] and _matches(DIGEST, digest), "base digest not applicable or malformed")
    if intent["base_digest"] is not None:
        if intent["base_digest"] != digest:
            raise Conflict("accepted base digest is write-once")
        return after
    _require(intent["status"] == "prepared", "base acceptance requires prepared intent")
    _current(after, intent["payload"])
    intent["base_digest"] = digest
    return _done(state, after)


def reserve(state, intent_id, *, expected_revision):
    after = _begin(state, expected_revision)
    intent = _intent(after, intent_id)
    _require(intent["status"] != "aborted", "cannot reserve aborted intent")
    if intent["version"] is not None:
        return after
    slot = _current(after, intent["payload"])
    _require(not intent["payload"]["requires_base"] or intent["base_digest"] is not None, "accept base digest before reservation")
    counter = after["counters"][slot]
    version = release_bump(counter["last_version"], intent["payload"]["causes"])
    counter.update(serial=counter["serial"] + 1, last_version=version)
    intent.update(status="reserved", version=version, serial=counter["serial"])
    return _done(state, after)


def published(state, intent_id, digest, verification, *, expected_revision):
    after = _begin(state, expected_revision)
    intent = _intent(after, intent_id)
    _require(_matches(DIGEST, digest), "publication digest must be SHA256")
    _evidence(verification)
    _require(intent["status"] in ("reserved", "published", "committed"), "publication requires reservation")
    if intent["digest"] is not None:
        if (intent["digest"], intent["verification"]) != (digest, verification):
            raise Conflict("publication digest/evidence is immutable")
        return after
    # Retain publication evidence even if the build became stale; commit rejects it.
    intent.update(status="published", digest=digest, verification=verification)
    return _done(state, after)


def commit(state, intent_id, *, expected_revision):
    after = _begin(state, expected_revision)
    intent = _intent(after, intent_id)
    if intent["status"] == "committed":
        return after
    _require(intent["status"] == "published", "only a verified publication can be committed")
    slot = _current(after, intent["payload"])
    version = intent["version"]
    _require(version not in after["entries"][slot], "index version already exists")
    highest_committed = max(entry["serial"] for entry in after["entries"][slot].values())
    if intent["serial"] <= highest_committed:
        raise Conflict("newer publication already committed")
    after["entries"][slot][version] = {"digest": intent["digest"], "verification": intent["verification"],
        "intent_id": intent_id, "material_key": intent["payload"]["material_key"], "serial": intent["serial"]}
    after["channels"][slot] = version
    intent["status"] = "committed"
    return _done(state, after)


def abort(state, intent_id, reason, *, expected_revision):
    after = _begin(state, expected_revision)
    intent = _intent(after, intent_id)
    _evidence(reason)
    _require(intent["status"] != "committed", "committed release cannot be aborted")
    if intent["status"] == "aborted":
        if intent["abort_reason"] != reason:
            raise Conflict("abort reason is immutable")
        return after
    intent.update(status="aborted", abort_reason=reason)
    return _done(state, after)


def rollback(state, image, channel, version, *, expected_revision):
    after = _begin(state, expected_revision)
    slot = _slot(image, channel)
    parse_version(version)
    _require(version in after["entries"].get(slot, {}), "rollback requires committed verified history")
    after["channels"][slot] = version
    return _done(state, after)
