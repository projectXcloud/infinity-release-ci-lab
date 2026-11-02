import copy
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
import pipeline as p


class AdapterTests(unittest.TestCase):
    def test_whole_config_snapshot(self):
        config = {"schemaVersion": 1, "dependencies": {}, "build": {"platform": "linux/amd64"}}
        whole = {k: copy.deepcopy(config) for k in p.COMPONENTS}
        self.assertEqual(set(p.config_snapshot(json.dumps(whole))), set(p.COMPONENTS))
        for bad in ["", "{}", json.dumps(config)]:
            with self.assertRaises(ValueError):
                p.config_snapshot(bad)
        whole["app-a"]["dependencies"] = {"test": {
            "repository": "https://github.com/example/dep", "branch": "dev", "commit": "a" * 40, "local-path": ""}}
        with self.assertRaises(ValueError):
            p.config_snapshot(json.dumps(whole))

    def test_forbidden_files(self):
        p.check_files(["config.example.jsonc", "fixtures/base/payload.json"])
        for path in ["VERSION", "a/VERSION", "config.local.jsonc", "nested/config.jsonc", ".local/x", "a/.local/x", "out/snapshot.json", "ci/__pycache__/config.pyc", "cache.pyc"]:
            with self.assertRaises(ValueError, msg=path):
                p.check_files([path])

    def test_app_bump_and_combined_base_causes(self):
        lock = {"dependencies": []}
        before = {"tag": "v1.0.1", "sha": "a" * 40}
        after = {"tag": "v2.0.0", "sha": "b" * 40}
        causes = p.causes_for(lock, lock, before, after, ["base_major", "dependency"])
        self.assertEqual(causes, ["app_major", "base_major", "dependency"])
        self.assertEqual(p.core.release_bump("4.5.6", causes), "5.0.0")
        self.assertEqual(p.causes_for(lock, lock, before, before, []), ["material"])

    def test_pull_checks_exact_material(self):
        material = {"lock": {"base_digest": "sha256:" + "a" * 64, "dependencies": []}}
        base = {"lock": {"base_digest": None, "dependencies": []}}
        def fake(*args, **kw):
            return json.dumps({"app-a": material, "base": base}) if args[1] == "run" else ""
        with patch.object(p, "run", side_effect=fake), patch.object(p, "save"):
            p.verify_image("ref", "app-a", material, base)
            with self.assertRaises(ValueError):
                p.verify_image("ref", "app-a", material, {"wrong": True})

    def test_numeric_tag_cannot_overwrite(self):
        with patch.object(p, "registry_digest", return_value="sha256:" + "a" * 64), patch.object(p, "run") as run:
            with self.assertRaises(ValueError):
                p.release_tag("base", "dev", "1.0.0", "candidate", "sha256:" + "b" * 64)
            run.assert_not_called()

    def test_cas_commit_parent_is_exact_read(self):
        ledger = object.__new__(p.Ledger)
        ledger.head, ledger.data = "old", {}
        calls = []
        def fake(path, method="GET", data=None):
            calls.append((path, data))
            return {"sha": "new"}
        with patch.object(p, "api", side_effect=fake), patch.object(p, "save"):
            ledger.write("test")
        self.assertEqual(calls[1][1]["parents"], ["old"])
        self.assertEqual(calls[2][1], {"sha": "new", "force": False})

    def test_late_first_source_requires_forward_ancestry(self):
        sources = {"app-a/dev": {"sha": "new", "tag": "v1.0.2"}}
        def ancestry(*args):
            if args[-2:] == ("new", "old"):
                raise RuntimeError("not ancestor")
            return ""
        with patch.object(p, "run", side_effect=ancestry):
            with self.assertRaises(RuntimeError):
                p.require_source_progression(sources, "dev", ("app-a",), "old")
            p.require_source_progression(sources, "dev", ("app-a",), "new")
            p.require_source_progression(sources, "dev", ("app-a",), "newer")

    def test_publisher_binds_checked_metadata_and_exact_merge_parents(self):
        request = {"component": "app-a", "requested_version": "1.0.2", "release_kind": "source"}
        checked = {"request": request, "head_sha": "head", "base_sha": "base", "base_ref": "dev"}
        body = "<!-- release-request -->\n```json\n" + json.dumps(request) + "\n```"
        pr = {"number": 3, "head": {"sha": "head"}, "base": {"ref": "dev"}, "merge_commit_sha": "merged", "body": body}
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("checked-request.json", json.dumps(checked))
        def api(path, *args):
            if path.startswith("actions/workflows/"):
                return {"workflow_runs": [{"id": 17, "head_sha": "head", "pull_requests": [{"number": 3}]}]}
            return {"artifacts": [{"id": 9, "name": "checked-request", "expired": False}]}
        def run(*args, **kwargs):
            return "merged base head" if args[1] == "rev-list" else "fixtures/app-a/payload.json"
        with patch.object(p, "api", side_effect=api), patch.object(p.subprocess, "check_output", return_value=data.getvalue()), patch.object(p, "run", side_effect=run):
            self.assertEqual(p.checked_request(pr), (checked, 17))
            changed = copy.deepcopy(pr)
            changed["body"] = body.replace('"app-a"', '"app-b"')
            with self.assertRaisesRegex(ValueError, "metadata changed"):
                p.checked_request(changed)
            with patch.object(p, "run", return_value="merged untested-base head"):
                with self.assertRaisesRegex(ValueError, "merge parents"):
                    p.checked_request(pr)


if __name__ == "__main__":
    unittest.main()
