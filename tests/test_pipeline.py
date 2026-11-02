import copy
import json
from pathlib import Path
import sys
import unittest
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
        for path in ["VERSION", "a/VERSION", "config.local.jsonc", "nested/config.jsonc", ".local/x", "a/.local/x"]:
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
        material = {"lock": {"base_digest": "sha256:" + "a" * 64}}
        base = {"lock": {"base_digest": None}}
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


if __name__ == "__main__":
    unittest.main()
