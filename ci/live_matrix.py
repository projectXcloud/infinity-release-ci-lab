"""Bounded synthetic GHCR exercise; run only from a trusted protected branch."""
import copy
import hashlib
import json
import os
from pathlib import Path
import traceback

import pipeline as p
import release_core as core

REPOSITORY = "projectXcloud/infinity-release-ci-lab"
ROOT = Path("out/live-matrix")
IMAGES = ("base", "app-a", "app-b")
NEW_PINS = {
    "app-only": "2ad047fd880d978bc487344b2aec4693c7821458",
    "shared": "e8d12c1a8b26af8af605578cdd4d751976bf576f",
}


def require(value, message):
    if not value:
        raise AssertionError(message)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def text(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def snapshot(directory, name):
    ledger = p.Ledger()
    data = copy.deepcopy(ledger.data)
    summary = {}
    for image in IMAGES:
        slot = image + "/dev"
        state = data["core"]
        version = state["channels"][slot]
        summary[image] = {"counter": state["counters"][slot], "selected": version,
            "selected_entry": state["entries"][slot][version],
            "desired": state["desired"][slot], "material": data["materials"][slot]}
    path = directory / (name + ".json")
    write(path, {"head": ledger.head, "data": data, "summary": summary})
    return {"head": ledger.head, "data": data, "summary": summary, "evidence": str(path)}


def patch_after(version):
    parts = [int(part) for part in version.split(".")]
    return f"{parts[0]}.{parts[1]}.{parts[2] + 1}"


def compare(before, after, changed, directory):
    """Independent expected set: counters, selected index, materials and pull evidence."""
    expected = set(changed)
    verified = {i for i in IMAGES if (directory / f"verified-{i}.json").exists()}
    require(verified == expected, f"verified image set {verified} != {expected}")
    for image in IMAGES:
        old, new = before["summary"][image], after["summary"][image]
        if image not in expected:
            require(old == new, f"unaffected {image} changed")
            continue
        require(new["counter"]["serial"] == old["counter"]["serial"] + 1,
                f"{image} did not allocate exactly once")
        require(new["counter"]["last_version"] == patch_after(old["counter"]["last_version"]),
                f"{image} patch did not increment exactly once")
        require(new["selected"] == new["counter"]["last_version"], f"{image} selection mismatch")
        require(old["material"]["lock"] != new["material"]["lock"], f"{image} lock unchanged")
        require(old["material"]["digest"] != new["material"]["digest"], f"{image} digest unchanged")
        require(new["selected_entry"]["digest"] == new["material"]["digest"], f"{image} index digest mismatch")
        require(new["selected_entry"]["material_key"] == new["desired"]["material_key"],
                f"{image} desired/index material mismatch")
        evidence = json.loads((directory / f"verified-{image}.json").read_text())
        require(evidence["ref"].endswith("@" + new["material"]["digest"]), f"{image} pull digest mismatch")
        require(evidence["observed"][image] == new["material"]["material"], f"{image} pull material mismatch")


class Suite:
    def __init__(self):
        self.prefix = "matrix-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
        require(len(self.prefix) < 45, "run identity too long for MODE/request identifiers")
        self.results = {"suite": self.prefix, "status": "RUNNING", "cases": [],
            "scope": "Synthetic single-repository dev channel and GHCR only",
            "limits": ["No GAR, production, cross-repository authentication, or concurrent race claim",
                       "Persisted-state guard rejections do not demonstrate simultaneous remote races",
                       "Baseline normalization is not selective-change evidence",
                       "No cleanup on failure; allocations and orphan artifacts are retained"]}
        self.directory = ROOT

    def save(self):
        write(ROOT / "SUITE-RESULT.json", self.results)

    def case(self, name, operation):
        self.directory = ROOT / name
        self.directory.mkdir(parents=True, exist_ok=False)
        p.OUT = self.directory
        result = {"name": name, "status": "FAIL", "directory": str(self.directory)}
        self.results["cases"].append(result)
        try:
            before = snapshot(self.directory, "before")
            result["before"] = {k: before[k] for k in ("head", "evidence", "summary")}
            operation(before, result)
            after = snapshot(self.directory, "after")
            result["after"] = {k: after[k] for k in ("head", "evidence", "summary")}
            result["status"] = "PASS"
            return after
        except Exception as error:
            result["error"] = {"type": type(error).__name__, "message": str(error)}
            write(self.directory / "failure.json", {**result["error"], "traceback": traceback.format_exc()})
            try:
                failed = snapshot(self.directory, "failed-state")
                result["after"] = {k: failed[k] for k in ("head", "evidence", "summary")}
            except Exception as read_error:
                result["state_capture_error"] = str(read_error)
            self.results["status"] = "FAIL"
            raise
        finally:
            self.save()

    def invoke(self, config_text, request, failpoint="none", expected=None, action="reconcile", **extra):
        updates = {"CHANNEL": "dev", "ACTION": action, "PR_NUMBER": "", "REQUEST_ID": self.prefix + "-" + request,
                   "CONFIG_DEV": config_text, "FAILPOINT": failpoint, **extra}
        original = {key: os.environ.get(key) for key in updates}
        os.environ.update(updates)
        try:
            try:
                p.release()
            except Exception as error:
                write(self.directory / "adapter-failure.json", {"type": type(error).__name__, "message": str(error)})
                if expected and expected in str(error):
                    return str(error)
                raise
            require(expected is None, f"expected failure missing: {expected}")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def release(self, name, config_text, changed=None, request=None):
        def operation(before, result):
            self.invoke(config_text, request or name)
            after = snapshot(self.directory, "checked")
            if changed is not None:
                compare(before, after, changed, self.directory)
            result["expected_changed"] = changed
            result["config_sha256"] = hashlib.sha256(config_text.encode()).hexdigest()
        return self.case(name, operation)


def dependency(configs, image, kind):
    matches = [dep for dep in configs[image]["dependencies"].values()
               if dep.get("subdirectory") == "fixtures/deps/" + kind]
    require(len(matches) == 1, f"expected exactly one {kind} dependency in {image}")
    dep = matches[0]
    require(dep["repository"] == "https://github.com/" + REPOSITORY, "unexpected dependency repository")
    require(dep["branch"] == "fixture/" + kind, "unexpected dependency branch")
    require(dep["version_tag"] == f"fixture/{kind}/v1.0.0", "baseline must use old dependency tag")
    require(dep["commit"] != NEW_PINS[kind], "baseline already uses updated pin")
    dep.update(commit=NEW_PINS[kind], version_tag=f"fixture/{kind}/v1.0.1")


def mode(configs, value):
    result = copy.deepcopy(configs)
    result["app-a"]["build"]["args"]["MODE"] = value
    return text(result)


def pending(suite, name, config_text, request):
    def operation(before, result):
        result["intentional_failure"] = suite.invoke(config_text, request, "after-push",
            "Deliberate lab failure after verified push, before index commit")
        after = snapshot(suite.directory, "checked")
        state = after["data"]["core"]
        intent_id = core.canonical_hash({"job": "dev/" + suite.prefix + "-" + request, "component": "app-a"})
        intent = state["intents"][intent_id]
        require(intent["status"] == "reserved" and intent["digest"] is None, "unexpected pending status")
        require(after["summary"]["app-a"]["counter"]["serial"] == before["summary"]["app-a"]["counter"]["serial"] + 1,
                "pending request did not reserve exactly once")
        require(intent["version"] == patch_after(before["summary"]["app-a"]["counter"]["last_version"]), "wrong reservation")
        for image in IMAGES:
            old, new = before["summary"][image], after["summary"][image]
            require(old["selected"] == new["selected"] and old["selected_entry"] == new["selected_entry"]
                    and old["material"] == new["material"], "pending push changed selected material")
            if image != "app-a":
                require(old == new, f"pending push changed {image}")
        digest = p.registry_digest(REPOSITORY.lower() + "/app-a/dev", intent["version"])
        candidate = p.registry_digest(REPOSITORY.lower() + "/candidate/app-a/dev", "build-" + intent["payload"]["material_key"])
        require(digest is not None and digest == candidate, "reserved numeric tag/candidate digest missing or different")
        verified = json.loads((suite.directory / "verified-app-a.json").read_text())
        require(verified["ref"].endswith("@" + digest), "pending digest lacks pull evidence")
        result.update(intent_id=intent_id, reserved_version=intent["version"], pushed_digest=digest)
    return suite.case(name, operation)


def main():
    require(os.environ.get("CHANNEL") == "dev", "live matrix is dev only")
    require(p.REPO == REPOSITORY, "wrong repository")
    ROOT.mkdir(parents=True, exist_ok=False)
    suite = Suite()
    suite.save()
    baseline_text = os.environ["CONFIG_DEV"]
    configs = p.config_snapshot(baseline_text)
    require(not configs["base"]["dependencies"], "baseline base dependencies must be empty")
    # Validate all baseline assumptions before the first remote mutation.
    app = copy.deepcopy(configs)
    dependency(app, "app-a", "app-only")
    shared = copy.deepcopy(app)
    for image in ("app-a", "app-b"):
        dependency(shared, image, "shared")
    base = copy.deepcopy(shared)
    base["base"]["dependencies"]["matrix-shared"] = {
        "repository": "https://github.com/" + REPOSITORY, "branch": "fixture/shared",
        "commit": NEW_PINS["shared"], "version_tag": "fixture/shared/v1.0.1", "subdirectory": "fixtures/deps/shared"}
    suite.release("baseline", baseline_text)
    suite.release("app-only", text(app), ["app-a"])
    suite.release("shared", text(shared), ["app-a", "app-b"])
    latest = suite.release("base-dependency", text(base), list(IMAGES))
    suite.release("identical", text(base), [])
    def duplicate(before, result):
        suite.invoke(text(base), "identical")
        after = snapshot(suite.directory, "checked")
        require(before["head"] == after["head"] and before["data"] == after["data"], "duplicate wrote ledger")
        compare(before, after, [], suite.directory)
        require(json.loads((suite.directory / "result.json").read_text())["idempotent_retry"], "missing duplicate evidence")
    suite.case("duplicate", duplicate)
    invalid = copy.deepcopy(base)
    next(iter(invalid["app-a"]["dependencies"].values()))["local-path"] = ""
    def invalid_case(before, result):
        tags = p.run("git", "ls-remote", "origin", "refs/tags/v*")
        write(suite.directory / "source-tags-before.json", tags)
        result["intentional_failure"] = suite.invoke(text(invalid), "invalid", expected="CI forbids local-path, including empty values")
        after = snapshot(suite.directory, "checked")
        require(before["head"] == after["head"] and before["data"] == after["data"], "invalid config wrote ledger")
        tags_after = p.run("git", "ls-remote", "origin", "refs/tags/v*")
        write(suite.directory / "source-tags-after.json", tags_after)
        require(tags == tags_after, "invalid config changed source tags")
    suite.case("invalid-local-path", invalid_case)
    retry_text = mode(base, suite.prefix + "-retry")
    reserved = pending(suite, "after-push", retry_text, "retry")
    pending_result = suite.results["cases"][-1]
    def resume(before, result):
        suite.invoke('{"deliberately":"invalid snapshot"}', "retry")
        after = snapshot(suite.directory, "checked")
        job_key = "dev/" + suite.prefix + "-retry"
        old_job, job = reserved["data"]["jobs"][job_key], after["data"]["jobs"][job_key]
        require(job["config_text"] == retry_text == old_job["config_text"], "retry replaced exact snapshot")
        require(job["config_sha256"] == old_job["config_sha256"] == hashlib.sha256(retry_text.encode()).hexdigest(), "retry config hash changed")
        require(before["data"]["core"]["counters"] == after["data"]["core"]["counters"], "retry allocated again")
        entry = after["summary"]["app-a"]
        require(entry["selected"] == pending_result["reserved_version"] and entry["selected_entry"]["digest"] == pending_result["pushed_digest"], "retry changed reservation/digest")
        for image in ("base", "app-b"):
            require(before["summary"][image] == after["summary"][image], "retry changed unaffected image")
        require((suite.directory / "verified-app-a.json").exists(), "retry missing pull evidence")
        result.update(config_sha256=job["config_sha256"], reserved_version=entry["selected"], pushed_digest=entry["selected_entry"]["digest"])
    suite.case("resume-saved-config", resume)
    def rollback(before, result):
        version = latest["summary"]["app-a"]["selected"]
        suite.invoke(text(base), "rollback", action="rollback", ROLLBACK_IMAGE="app-a", ROLLBACK_VERSION=version)
        after = snapshot(suite.directory, "checked")
        require(before["data"]["core"]["counters"] == after["data"]["core"]["counters"], "rollback rewound allocations")
        require(before["data"]["materials"] == after["data"]["materials"], "rollback replaced latest material")
        require(after["summary"]["app-a"]["selected"] == version, "rollback selected wrong version")
        require(after["summary"]["app-a"]["selected_entry"] == latest["summary"]["app-a"]["selected_entry"], "rollback changed verified history")
        result["distinction"] = "channels selects historical version; materials retains latest successfully built material"
    suite.case("rollback", rollback)
    suite.release("after-rollback", mode(base, suite.prefix + "-post-rollback"), ["app-a"])
    a_text = mode(base, suite.prefix + "-aba-A")
    a_pending = pending(suite, "aba-pending-A", a_text, "aba-pending")
    suite.release("aba-B", mode(base, suite.prefix + "-aba-B"), ["app-a"])
    a_complete = suite.release("aba-new-A", a_text, ["app-a"])
    def stale(before, result):
        old, new = a_pending["summary"]["app-a"], a_complete["summary"]["app-a"]
        require(old["desired"]["material_key"] == new["desired"]["material_key"], "ABA did not return to same material")
        require(new["desired"]["generation"] == old["desired"]["generation"] + 2, "ABA generation did not advance twice")
        result["intentional_failure"] = suite.invoke(a_text, "aba-pending", expected="Stale request superseded")
        after = snapshot(suite.directory, "checked")
        require(before["head"] == after["head"] and before["data"] == after["data"], "stale job wrote ledger")
        result["burned_reservation"] = a_pending["summary"]["app-a"]["counter"]
    suite.case("aba-stale-rejection", stale)
    def guards(before, result):
        ledger = p.Ledger()
        selected = ledger.data["core"]["channels"]["app-a/dev"]
        intent_id = ledger.data["core"]["entries"]["app-a/dev"][selected]["intent_id"]
        intent = ledger.data["core"]["intents"][intent_id]
        payload = copy.deepcopy(intent["payload"])
        payload["plan_hash"] = ("0" if payload["plan_hash"][0] != "0" else "1") + payload["plan_hash"][1:]
        different = "sha256:" + ("0" * 64 if intent["base_digest"] != "sha256:" + "0" * 64 else "1" * 64)
        result["intentional_failures"] = []
        for fn, args, message in [(core.prepare, (intent_id, payload), "intent ID reused with different payload"),
                                  (core.accept_base, (intent_id, different), "accepted base digest is write-once")]:
            try:
                ledger.transition(fn, *args)
            except core.Conflict as error:
                require(message in str(error), "wrong guard rejection")
                result["intentional_failures"].append(str(error))
            else:
                raise AssertionError("conflicting transition succeeded")
            require(p.Ledger().head == before["head"], "guard rejection wrote ledger")
    suite.case("persisted-intent-guards", guards)
    suite.release("restore-baseline", baseline_text, list(IMAGES))
    restored = p.Ledger()
    require(restored.data["jobs"]["dev/" + suite.prefix + "-restore-baseline"]["config_text"] == baseline_text,
            "final baseline snapshot differs")
    suite.results["status"] = "PASS"
    suite.save()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        path = ROOT / "SUITE-RESULT.json"
        if path.exists():
            result = json.loads(path.read_text())
            result.update(status="FAIL", fatal_error={"type": type(error).__name__, "message": str(error)})
            write(path, result)
        raise
