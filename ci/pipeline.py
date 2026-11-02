"""Synthetic single-repository adapter; core policy is backend independent."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import config
import release_core as core

COMPONENTS = ("base", "app-a", "app-b")
OUT = Path("out")
REPO = os.environ.get("GITHUB_REPOSITORY", "projectXcloud/infinity-release-ci-lab")


def run(*args, input=None, env=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[-3000:]}")
    return result.stdout.strip()


def save(name, value):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def api(path, method="GET", data=None):
    args = ["gh", "api", f"repos/{REPO}/{path}", "--method", method]
    if data is not None:
        args += ["--input", "-"]
    return json.loads(run(*args, input=json.dumps(data) if data is not None else None) or "null")


class Ledger:
    """Every write is a new commit parented to the exact read revision.

    GitHub's non-force ref update rejects a sibling writer, providing real CAS.
    Initialization is explicit, never an implicit reset after a failed read.
    """
    def __init__(self, initialize=False):
        refs = api("git/matching-refs/heads/release-ledger")
        if not refs:
            if not initialize:
                raise RuntimeError("Missing ledger; explicit bootstrap required")
            self.head = None
            self.data = {"schema": 1, "core": core.new_state(), "sources": {},
                         "requests": {}, "jobs": {}, "materials": {}, "active_jobs": {}}
            self.write("Initialize synthetic ledger")
        else:
            if len(refs) != 1 or refs[0]["ref"] != "refs/heads/release-ledger":
                raise RuntimeError("Ambiguous ledger ref")
            self.head = refs[0]["object"]["sha"]
            content = api(f"contents/ledger.json?ref={self.head}")
            self.data = core.strict_json_loads(base64.b64decode(content["content"]).decode())
            core.validate_state(self.data["core"])

    def write(self, message):
        tree = api("git/trees", "POST", {"tree": [{"path": "ledger.json", "mode": "100644",
            "type": "blob", "content": json.dumps(self.data, sort_keys=True, indent=2) + "\n"}]})
        commit = api("git/commits", "POST", {"message": message, "tree": tree["sha"],
                     "parents": [self.head] if self.head else []})
        if self.head:
            api("git/refs/heads/release-ledger", "PATCH", {"sha": commit["sha"], "force": False})
        else:
            api("git/refs", "POST", {"ref": "refs/heads/release-ledger", "sha": commit["sha"]})
        self.head = commit["sha"]
        save("ledger.json", self.data)

    def transition(self, fn, *args):
        state = self.data["core"]
        self.data["core"] = fn(state, *args, expected_revision=state["revision"])
        self.write(f"{fn.__name__}: {args[0]}")


def config_snapshot(text):
    value = config.parse_jsonc(text)
    if type(value) is not dict or set(value) != set(COMPONENTS):
        raise ValueError("CI config must contain exactly base, app-a, app-b")
    return {component: config.validate_config(value[component], ci=True) for component in COMPONENTS}


def check_files(files):
    for name in files:
        parts = Path(name).parts
        if any(p in {"VERSION", "config.local.jsonc", "config.jsonc", ".local"} for p in parts):
            raise ValueError(f"Forbidden tracked file: {name}")


def check():
    event = core.strict_json_loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    request = core.parse_request(pr.get("body") or "")
    check_files(run("git", "ls-files").splitlines())
    config.load_config(False, None, None, Path("config.example.jsonc"))
    changed = run("git", "diff", "--name-only", pr["base"]["sha"], "HEAD").splitlines()
    touched = {name.split("/")[1] for name in changed if name.startswith("fixtures/")
               and len(name.split("/")) > 2 and name.split("/")[1] in COMPONENTS}
    if request["release_kind"] == "source" and touched - {request["component"]}:
        raise ValueError("Request component does not cover changed fixture sources")
    tags = run("git", "tag", "--list").splitlines()
    preview = core.allocate_source(request["requested_version"], tags)
    print(json.dumps({"request": request, "allocation_preview_only": preview, "changed": changed}))


def source_record(ledger, channel, pr_number, bootstrap=False):
    if pr_number:
        pr = api(f"pulls/{pr_number}")
        if not pr["merged"] or pr["base"]["ref"] != channel or pr["base"]["repo"]["full_name"].lower() != REPO.lower():
            raise ValueError("Source PR must be merged into the selected channel")
        request = core.parse_request(pr.get("body") or "")
        sha = pr["merge_commit_sha"]
        # Never infer tested head == resulting merge commit; preserve both explicitly.
        key = f"pr-{pr_number}"
        audit = {"pr": pr["html_url"], "head_sha": pr["head"]["sha"], "merge_sha": sha}
    elif bootstrap:
        sha = api(f"git/ref/heads/{channel}")["object"]["sha"]
        request = {"component": "base", "requested_version": "1.0.1", "release_kind": "integration"}
        key, audit = f"bootstrap-{channel}", {}
    else:
        return None
    run("git", "fetch", "origin", channel, "--tags")
    run("git", "merge-base", "--is-ancestor", sha, f"origin/{channel}")
    binding = {"source_commit": sha, "request": request, "channel": channel}
    old = ledger.data["requests"].get(key)
    if old:
        if old["binding"] != binding:
            raise ValueError("Release request identity reused with different source/metadata")
        record = old
    else:
        tags = [x["ref"].removeprefix("refs/tags/") for x in api("git/matching-refs/tags/v")]
        tags += [r["tag"] for r in ledger.data["requests"].values()]
        tag = core.allocate_source(request["requested_version"], tags)
        record = {"binding": binding, "tag": tag, "audit": audit}
        ledger.data["requests"][key] = record
        ledger.write(f"Reserve {tag} for {key}")
    found = [r for r in api(f"git/matching-refs/tags/{record['tag']}") if r["ref"] == f"refs/tags/{record['tag']}"]
    if found:
        if found[0]["object"]["sha"] != sha:
            raise ValueError("Existing canonical tag has another commit; refusing overwrite")
    else:
        api("git/refs", "POST", {"ref": f"refs/tags/{record['tag']}", "sha": sha})
    components = COMPONENTS if request["release_kind"] in {"integration", "promotion"} else (request["component"],)
    for component in components:
        ledger.data["sources"][f"{component}/{channel}"] = {"sha": sha, "tag": record["tag"]}
    ledger.write(f"Record source {record['tag']} on {channel}")
    return key


def registry_digest(repository, tag):
    scope = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    credentials = base64.b64encode(f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()).decode()
    req = urllib.request.Request(f"https://ghcr.io/token?{scope}", headers={"Authorization": "Basic " + credentials})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            token = json.load(response)["token"]
        req = urllib.request.Request(f"https://ghcr.io/v2/{repository}/manifests/{tag}", headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if digest != response.headers["Docker-Content-Digest"]:
                raise ValueError("Registry manifest bytes disagree with digest")
            return digest
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def verify_image(ref, component, material, base_material=None):
    run("docker", "pull", ref)
    observed = core.strict_json_loads(run("docker", "run", "--rm", "--network", "none", ref))
    if observed.get(component) != material:
        raise ValueError("Pulled image does not contain expected locked material")
    if component != "base" and observed.get("base") != base_material:
        raise ValueError("App does not contain exact expected base material")
    save(f"verified-{component}.json", {"ref": ref, "observed": observed})


def publish(component, channel, lock, source, deps, base_ref, base_material):
    material = {"lock": lock, "fixture": core.strict_json_loads(run("git", "show", f"{source['sha']}:fixtures/{component}/payload.json"))}
    key = config.lock_hash(lock).removeprefix("sha256:")
    repository = f"{REPO.lower()}/candidate/{component}/{channel}"
    tag = "build-" + key
    ref = f"ghcr.io/{repository}:{tag}"
    digest = registry_digest(repository, tag)
    if digest is None:
        context = Path(".local/contexts") / component
        if context.exists():
            shutil.rmtree(context)
        (context / "materials").mkdir(parents=True)
        (context / "materials" / f"{component}.json").write_text(json.dumps(material, sort_keys=True))
        for dep in deps:
            if not dep["publishable"]:
                raise ValueError("Local dependency is not publishable")
            shutil.copytree(dep["context_path"], context / "deps" / component / dep["name"],
                            symlinks=False, ignore=shutil.ignore_patterns(".git"))
        dockerfile = "FROM " + (base_ref if base_ref else "scratch") + "\n"
        if component == "base":
            env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"}
            run("go", "build", "-trimpath", "-ldflags=-buildid=", "-o", str(context / "inspect"), "ci/inspect.go", env=env)
            dockerfile += "COPY inspect /inspect\nENTRYPOINT [\"/inspect\"]\n"
        dockerfile += "COPY materials/ /materials/\n"
        if deps:
            dockerfile += "COPY deps/ /deps/\n"
        dockerfile += f'LABEL org.opencontainers.image.source="https://github.com/{REPO}"\n'
        dockerfile += f'LABEL io.infinity.material="{key}"\n'
        (context / "Dockerfile").write_text(dockerfile)
        run("docker", "build", "--platform=linux/amd64", "--tag", ref, str(context))
        run("docker", "push", ref)
        digest = registry_digest(repository, tag)
        if not digest:
            raise ValueError("Pushed candidate missing from registry")
    exact = f"ghcr.io/{repository}@{digest}"
    verify_image(exact, component, material, base_material)
    return digest, exact, material


def release_tag(component, channel, version, exact, digest):
    repository = f"{REPO.lower()}/{component}/{channel}"
    ref = f"ghcr.io/{repository}:{version}"
    existing = registry_digest(repository, version)
    if existing and existing != digest:
        raise ValueError("Numeric release tag already bound to a different digest")
    if not existing:
        run("docker", "tag", exact, ref)
        run("docker", "push", ref)
    if registry_digest(repository, version) != digest:
        raise ValueError("Numeric release digest differs from verified candidate")
    return f"ghcr.io/{repository}@{digest}"


def causes_for(old, new, old_source, new_source, base_causes):
    causes = list(base_causes)
    if old_source != new_source:
        before, after = core.parse_version(old_source["tag"][1:]), core.parse_version(new_source["tag"][1:])
        field = "major" if before[0] != after[0] else "minor" if before[1] != after[1] else "patch"
        causes.append("app_" + field)
    if old["dependencies"] != new["dependencies"]:
        causes.append("dependency")
    return sorted(set(causes or ["material"]))


def builder_identity():
    # Stable across fixture-only commits; workflow/builder changes invalidate builds.
    return run("git", "log", "-1", "--format=%H", "--", "ci", ".github/workflows")


def release():
    channel, action = os.environ["CHANNEL"], os.environ["ACTION"]
    if channel not in {"dev", "main"}:
        raise ValueError("Unknown channel")
    ledger = Ledger(initialize=action == "bootstrap")
    if action == "cas-test":
        stale = Ledger()
        ledger.write("CAS contender one")
        try:
            stale.write("CAS contender two must fail")
        except RuntimeError as error:
            if "fast forward" not in str(error).lower():
                raise
            save("cas-test.json", {"rejected_stale_writer": True, "error": str(error)})
            return
        raise AssertionError("Stale Git writer unexpectedly succeeded")
    if action == "rollback":
        ledger.transition(core.rollback, os.environ["ROLLBACK_IMAGE"], channel, os.environ["ROLLBACK_VERSION"])
        return
    pr_number = os.environ.get("PR_NUMBER", "")
    key = f"pr-{pr_number}" if pr_number else os.environ["REQUEST_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
        raise ValueError("Invalid request ID")
    job_key = channel + "/" + key
    previous = ledger.data["jobs"].get(job_key)
    if previous and ledger.data["active_jobs"].get(channel) != job_key:
        raise ValueError("Stale request superseded by a newer channel generation")
    if previous and previous["status"] == "complete":
        save("result.json", {"idempotent_retry": True, "job": previous})
        return
    text = previous["config_text"] if previous else os.environ.get("CONFIG_" + channel.upper(), "")
    configs = config_snapshot(text)  # Reject before tags or artifact side effects.
    check_files(run("git", "ls-files").splitlines())
    if not previous:
        source_record(ledger, channel, pr_number, action == "bootstrap")
        sources = {c: copy.deepcopy(ledger.data["sources"][f"{c}/{channel}"]) for c in COMPONENTS}
        job = {"config_text": text, "config_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "sources": sources, "builder_sha": builder_identity(),
               "status": "pending", "completed": [], "base_causes": []}
        ledger.data["jobs"][job_key] = job
        ledger.data["active_jobs"][channel] = job_key
        ledger.write(f"Snapshot {job_key}")
    else:
        job = previous
        # Resume old accepted inputs; never silently substitute a newer source/builder.
        if builder_identity() != job["builder_sha"]:
            raise ValueError("Resume requires the original builder commit")
    save("snapshot.json", job)
    run("docker", "login", "ghcr.io", "--username", os.environ["GITHUB_ACTOR"], "--password-stdin", input=os.environ["GH_TOKEN"])
    for component in COMPONENTS:
        slot = f"{component}/{channel}"
        if component in job["completed"]:
            continue
        source = job["sources"][component]
        deps = config.resolve_dependencies(configs[component], Path(".local/deps"), {"https://github.com/" + REPO}, ci=True)
        base = ledger.data["materials"].get(f"base/{channel}") if component != "base" else None
        if component != "base" and base is None:
            raise ValueError("Apps require a verified base digest")
        lock = config.material_lock(source["sha"], configs[component], deps, base["digest"] if base else None, job["builder_sha"])
        material_key = config.lock_hash(lock).removeprefix("sha256:")
        old = ledger.data["materials"].get(slot)
        if old and old["lock"] == lock:
            job["completed"].append(component)
            ledger.write(f"Unchanged {slot}")
            continue
        ledger.transition(core.set_desired, component, channel, material_key)
        causes = causes_for(old["lock"], lock, old["source"], source, job["base_causes"] if base else []) if old else ["material"]
        if component == "base":
            causes = [x.replace("app_", "base_") for x in causes]
            job["base_causes"] = causes
        intent_id = core.canonical_hash({"job": job_key, "component": component})
        if old:
            payload = {"image": component, "channel": channel, "source_tag": source["tag"],
                       "source_commit": source["sha"], "material_key": material_key,
                       "plan_hash": core.canonical_hash({"lock": lock, "config": job["config_sha256"]}),
                       "desired_generation": ledger.data["core"]["desired"][slot]["generation"],
                       "causes": causes, "requires_base": base is not None}
            ledger.transition(core.prepare, intent_id, payload)
            if base:
                ledger.transition(core.accept_base, intent_id, base["digest"])
            ledger.transition(core.reserve, intent_id)
            version = ledger.data["core"]["intents"][intent_id]["version"]
        else:
            if action != "bootstrap":
                raise ValueError("Missing image history; explicit bootstrap required")
            version = "1.0.0"
        digest, exact, material = publish(component, channel, lock, source, deps,
                                          base["ref"] if base else None, base["material"] if base else None)
        public = release_tag(component, channel, version, exact, digest)
        verify_image(public, component, material, base["material"] if base else None)
        if os.environ.get("FAILPOINT") == "after-push":
            raise RuntimeError("Deliberate lab failure after verified push, before index commit")
        if old:
            ledger.transition(core.published, intent_id, digest, public)
            ledger.transition(core.commit, intent_id)
        else:
            entry = ledger.data["core"]["entries"].get(slot, {}).get(version)
            if entry:
                if (entry["digest"], entry["material_key"]) != (digest, material_key):
                    raise ValueError("Bootstrap recovery conflicts with verified history")
            else:
                ledger.transition(core.bootstrap, component, channel, version, digest, public)
        ledger.data["materials"][slot] = {"lock": lock, "digest": digest, "ref": public,
                                           "material": material, "source": source}
        job["completed"].append(component)
        ledger.write(f"Complete material {slot}:{version}")
    job["status"] = "complete"
    ledger.write(f"Complete {job_key}")
    save("result.json", {"job": job, "selected": ledger.data["core"]["channels"]})


if __name__ == "__main__":
    try:
        {"check": check, "release": release}[sys.argv[1]]()
    except Exception as error:
        save("failure.json", {"type": type(error).__name__, "message": str(error)})
        raise
