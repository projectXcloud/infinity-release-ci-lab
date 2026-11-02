import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci.config import (ConfigError, load_config, lock_hash, material_lock,
                       parse_jsonc, resolve_dependencies, validate_config)

REPOSITORY = "https://github.com/example/synthetic"
BASE = {"schemaVersion": 1, "dependencies": {}, "build": {"platform": "linux/amd64"}}
DIGEST = "sha256:" + "d" * 64


class ParserTests(unittest.TestCase):
    def test_comments_trailing_commas_and_strings(self):
        value = parse_jsonc(r'''{// comment
          "url":"https://github.com/example/r", "literal":"/* x */ // x",
          "quote":"\" // still inside", "array":[1,2,/* end */],
        }''')
        self.assertEqual(value["array"], [1, 2])
        self.assertEqual(value["quote"], '" // still inside')
        self.assertEqual(value["url"], REPOSITORY.replace("synthetic", "r"))

    def test_duplicate_keys_in_nested_objects(self):
        with self.assertRaises(ConfigError):
            parse_jsonc('{"a":{"b":1,"b":2}}')

    def test_unterminated_comments(self):
        with self.assertRaises(ConfigError):
            parse_jsonc('{"a":1}/* missing')

    def test_nonfinite_numbers(self):
        for text in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_jsonc(text)

    def test_invalid_json_not_repaired(self):
        for text in ("[, ]", "{,}", "[1,,]", "{'a':1}", "[01]", '"unterminated'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse_jsonc(text)

    def test_schema_types_unknown_fields(self):
        for obj in ({**BASE, "schemaVersion": True}, {**BASE, "extra": 1},
                    {**BASE, "dependencies": []}, {**BASE, "build": {"platform": "windows/amd64"}},
                    {**BASE, "build": {"platform": "linux/amd64", "args": {"SECRET": "x"}}}):
            with self.subTest(obj=obj), self.assertRaises(ConfigError):
                validate_config(obj, True)

    def test_effective_defaults_and_input_immutability(self):
        before = copy.deepcopy(BASE)
        result = validate_config(BASE, True)
        self.assertEqual(result["build"]["args"], {"MODE": "release"})
        self.assertEqual(BASE, before)

    def test_ci_requires_snapshot_and_rejects_empty_snapshot(self):
        for snapshot in (None, ""):
            with self.subTest(snapshot=snapshot), self.assertRaises(ConfigError):
                load_config(True, snapshot, Path("unused"), Path("missing"))
        self.assertEqual(load_config(True, json.dumps(BASE), Path("missing"), Path("missing"))["schemaVersion"], 1)

    def test_local_override_is_whole_document(self):
        with tempfile.TemporaryDirectory() as folder:
            example, local = Path(folder) / "example.jsonc", Path(folder) / "local.jsonc"
            example.write_text(json.dumps(BASE))
            local.write_text('{"schemaVersion":1}')
            with self.assertRaises(ConfigError):
                load_config(False, None, local, example)
            local.unlink()
            self.assertEqual(load_config(False, None, local, example)["schemaVersion"], 1)


class GitMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Synthetic Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "input.txt").write_text("initial\n")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        self.commit = self.commit_file()
        self.git("tag", "v1.2.3")
        self.config = copy.deepcopy(BASE)
        self.config["dependencies"]["shared"] = {
            "repository": REPOSITORY, "branch": "main", "commit": self.commit}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.STDOUT).strip()

    def commit_file(self):
        self.git("add", ".")
        self.git("commit", "-q", "-m", "synthetic")
        return self.git("rev-parse", "HEAD")

    def resolve(self, config=None, ci=True, root=None):
        return resolve_dependencies(config or self.config, root or self.root / "consumer",
                                    {REPOSITORY}, ci, transport={REPOSITORY: str(self.repo)})

    def test_exact_ancestor_pin_and_tag_not_branch_head(self):
        (self.repo / "input.txt").write_text("later\n")
        later = self.commit_file()
        records = self.resolve()
        self.assertEqual(records[0]["commit"], self.commit)
        self.assertEqual(records[0]["audit"]["initial_branch_tip"], later)
        self.assertEqual(records[0]["version"], "1.2.3")
        self.assertEqual((Path(records[0]["context_path"]) / "input.txt").read_text(), "initial\n")
        self.assertTrue(records[0]["publishable"])
        self.assertEqual(self.resolve(), records)

    def test_annotated_explicit_fixture_tag(self):
        self.git("tag", "-a", "fixture/shared/v2.0.0", "-m", "fixture")
        self.config["dependencies"]["shared"]["version_tag"] = "fixture/shared/v2.0.0"
        self.assertEqual(self.resolve()[0]["version"], "2.0.0")

    def test_no_branch_substitution_for_missing_pin(self):
        self.config["dependencies"]["shared"]["commit"] = "0" * 40
        with self.assertRaises(ConfigError):
            self.resolve()

    def test_failed_fetch_does_not_poison_retry(self):
        self.config["dependencies"]["shared"]["branch"] = "missing"
        with self.assertRaises(ConfigError):
            self.resolve()
        self.config["dependencies"]["shared"]["branch"] = "main"
        self.assertEqual(self.resolve()[0]["commit"], self.commit)
        self.assertEqual(len(list((self.root / "consumer" / ".local").glob("*/.fetch-*"))), 1)

    def test_wrong_lineage_rejected(self):
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        (self.repo / "other.txt").write_text("other")
        self.commit_file()
        self.config["dependencies"]["shared"]["branch"] = "unrelated"
        with self.assertRaises(ConfigError):
            self.resolve()

    def test_same_repo_commit_different_branches_have_distinct_lineage_caches(self):
        self.git("branch", "alternate", self.commit)
        second = copy.deepcopy(self.config["dependencies"]["shared"])
        second["branch"] = "alternate"
        self.config["dependencies"]["second"] = second
        records = self.resolve()
        self.assertEqual(len(records), 2)
        self.assertEqual({r["commit"] for r in records}, {self.commit})
        self.assertEqual(len({r["context_path"] for r in records}), 2)
        self.assertEqual(self.resolve(), records)

    def test_tag_must_target_exact_pin(self):
        (self.repo / "input.txt").write_text("later")
        self.commit_file()
        self.git("tag", "v1.2.4")
        self.config["dependencies"]["shared"]["version_tag"] = "v1.2.4"
        with self.assertRaises(ConfigError):
            self.resolve()

    def test_missing_and_ambiguous_tags_fail(self):
        self.git("tag", "v1.2.4")
        with self.assertRaises(ConfigError):
            self.resolve(root=self.root / "ambiguous")
        self.git("tag", "-d", "v1.2.3", "v1.2.4")
        with self.assertRaises(ConfigError):
            self.resolve(root=self.root / "untagged")

    def test_repository_allowlist_enforced(self):
        with self.assertRaises(ConfigError):
            resolve_dependencies(self.config, self.root, set(), True)

    def test_invalid_repository_branch_pin_and_paths(self):
        cases = [("repository", "https://secret@github.com/example/r"),
                 ("repository", "https://evil.invalid/example/r"),
                 ("repository", "https://github.com/example/r?q=x"),
                 ("branch", "--upload-pack=evil"), ("branch", "../main"),
                 ("commit", self.commit[:12]), ("subdirectory", "../outside"),
                 ("subdirectory", "/tmp"), ("subdirectory", ".git")]
        for field, value in cases:
            config = copy.deepcopy(self.config)
            config["dependencies"]["shared"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                validate_config(config, True)

    def test_ci_rejects_local_path_even_empty(self):
        for path in ("", str(self.repo)):
            self.config["dependencies"]["shared"]["local-path"] = path
            with self.subTest(path=path), self.assertRaises(ConfigError):
                self.resolve()

    def test_local_dirty_untracked_and_ignored_materials_hashed(self):
        self.config["dependencies"]["shared"]["local-path"] = str(self.repo)
        first = self.resolve(ci=False)[0]
        self.assertFalse(first["publishable"])
        previous = first["material_hash"]
        for name in ("input.txt", "untracked.txt", "ignored.txt"):
            (self.repo / name).write_text("modified " + name)
            current = self.resolve(ci=False)[0]["material_hash"]
            self.assertNotEqual(current, previous)
            previous = current

    def test_dirty_remote_cache_is_rejected(self):
        records = self.resolve()
        cache = Path(records[0]["context_path"])
        (cache / "ignored.txt").write_text("not tracked")
        with self.assertRaises(ConfigError):
            self.resolve()

    def test_local_permissions_and_empty_directories_are_material(self):
        self.config["dependencies"]["shared"]["local-path"] = str(self.repo)
        first = self.resolve(ci=False)[0]["material_hash"]
        (self.repo / "input.txt").chmod(0o600)
        second = self.resolve(ci=False)[0]["material_hash"]
        self.assertNotEqual(first, second)
        (self.repo / "empty").mkdir()
        self.assertNotEqual(second, self.resolve(ci=False)[0]["material_hash"])

    def test_cache_symlink_rejected(self):
        consumer = self.root / "consumer"
        consumer.mkdir()
        (consumer / ".local").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.resolve()

    def test_unsupported_materials_fail(self):
        self.config["dependencies"]["shared"]["local-path"] = str(self.repo)
        for name, data in ((".gitmodules", "[submodule]"),
                           ("large.bin", "version https://git-lfs.github.com/spec/v1\noid sha256:abc"),
                           (".gitattributes", "*.bin filter=lfs diff=lfs")):
            path = self.repo / name
            path.write_text(data)
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.resolve(ci=False)
            path.unlink()
        (self.repo / "link").symlink_to("input.txt")
        with self.assertRaises(ConfigError):
            self.resolve(ci=False)

    def test_gitlink_without_gitmodules_rejected(self):
        self.config["dependencies"]["shared"]["local-path"] = str(self.repo)
        self.git("update-index", "--add", "--cacheinfo", "160000", self.commit, "module")
        with self.assertRaises(ConfigError):
            self.resolve(ci=False)

    def test_material_lock_excludes_audit_but_binds_every_material(self):
        records = self.resolve()
        initial = material_lock(self.commit, self.config, records, DIGEST, "b" * 40)
        changed = copy.deepcopy(records)
        changed[0]["audit"] = {"branch": "other", "timestamp": "later", "local_path": "/other"}
        changed[0]["context_path"] = "/other"
        self.assertEqual(lock_hash(initial), lock_hash(material_lock(self.commit, self.config, changed, DIGEST, "b" * 40)))
        for field, value in (("material_hash", "sha256:" + "a" * 64), ("version", "9.9.9")):
            changed = copy.deepcopy(records)
            changed[0][field] = value
            self.assertNotEqual(lock_hash(initial), lock_hash(material_lock(self.commit, self.config, changed, DIGEST, "b" * 40)))
        for source, base, builder in (("a" * 40, DIGEST, "b" * 40),
                                      (self.commit, "sha256:" + "a" * 64, "b" * 40),
                                      (self.commit, DIGEST, "a" * 40)):
            self.assertNotEqual(lock_hash(initial), lock_hash(material_lock(source, self.config, records, base, builder)))
        config = copy.deepcopy(self.config)
        config["build"]["args"] = {"MODE": "debug"}
        self.assertNotEqual(lock_hash(initial), lock_hash(material_lock(self.commit, config, records, DIGEST, "b" * 40)))

    def test_root_scratch_base_with_dependencies_has_null_base_digest(self):
        lock = material_lock(self.commit, self.config, self.resolve(), None, "b" * 40)
        self.assertIsNone(lock["base_digest"])
        self.assertEqual(len(lock["dependencies"]), 1)

    def test_lock_rejects_unresolved_and_mismatched_records(self):
        records = self.resolve()
        for replacement in ([], records + records, [{**records[0], "commit": "a" * 40}],
                            [{**records[0], "publishable": False}]):
            with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                material_lock(self.commit, self.config, replacement, DIGEST, "b" * 40)
        with self.assertRaises(ConfigError):
            material_lock(self.commit, self.config, records, "latest", "b" * 40)


if __name__ == "__main__":
    unittest.main()
