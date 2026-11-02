"""Policy boundaries and state-machine regressions; standard-library unittest."""
import copy
import itertools
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "ci"))
import release_core as r

A = "a" * 64
B = "b" * 64
C = "c" * 64
D1 = "sha256:" + A
D2 = "sha256:" + B
D3 = "sha256:" + C


def request(value):
    return "Summary\n<!-- release-request -->\n```json\n" + json.dumps(value) + "\n```\n"


def payload(**changes):
    value = {"image": "app-a", "channel": "dev", "source_tag": "v1.0.0",
             "source_commit": "d" * 40, "material_key": A, "plan_hash": B,
             "desired_generation": 1, "causes": ["app_patch"], "requires_base": True}
    value.update(changes)
    return value


class Helpers(unittest.TestCase):
    def test_canonical_versions(self):
        self.assertEqual(r.parse_version("0.12.345"), (0, 12, 345))
        for bad in ("v1.2.3", "1.2.3\n", " 1.2.3", "01.2.3", "1.2", "1.2.3-rc.1", "+1.2.3", "1.2.3+build", 123, True):
            with self.subTest(bad=bad), self.assertRaises(r.PolicyError):
                r.parse_version(bad)

    def test_allocate_all_tags_in_line(self):
        tags = ["v1.2.3", "v1.2.20", "v1.3.50", "v2.2.80", "fixture/v1.2.100", "scratch"]
        self.assertEqual(r.allocate_source("1.2.5", tags), "v1.2.21")
        self.assertEqual(r.allocate_source("1.2.30", tags), "v1.2.30")
        self.assertEqual(r.allocate_source("0.0.0", []), "v0.0.0")
        self.assertEqual(tags[0], "v1.2.3")

    def test_allocate_ambiguous_tags_fail(self):
        for bad in ("v01.2.3", "v1.2.3-rc.1", "v1.2", "v1.2.3\n", "vnext"):
            with self.subTest(bad=bad), self.assertRaises(r.PolicyError):
                r.allocate_source("1.2.3", [bad])

    def test_request_valid(self):
        value = {"component": "base", "requested_version": "1.2.3", "release_kind": "integration"}
        self.assertEqual(r.parse_request(request(value)), value)

    def test_request_rejects_missing_duplicate_and_injected_fields(self):
        good = {"component": "base", "requested_version": "1.2.3", "release_kind": "source"}
        bads = ["", request(good) * 2, request(good).replace("```json", "```"),
                request(good).replace('"component": "base"', '"component": "base", "component": "app-a"'),
                request({**good, "command": "$(id)"}), request({**good, "component": "$(id)"}),
                request({**good, "requested_version": "1.2.3; id"}),
                request({**good, "release_kind": "unknown"}),
                request({k: v for k, v in good.items() if k != "component"}),
                request(good).rsplit("```", 1)[0]]
        for body in bads:
            with self.subTest(body=body), self.assertRaises(r.PolicyError):
                r.parse_request(body)

    def test_canonical_hash_and_numbers(self):
        self.assertEqual(r.canonical_hash({"b": [True, None, 42], "a": "é"}),
                         r.canonical_hash({"a": "é", "b": [True, None, 42]}))
        self.assertNotEqual(r.canonical_hash(1), r.canonical_hash(True))
        self.assertEqual(r.canonical_hash({}), "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")
        for bad in (1.0, float("inf"), float("nan"), {1: "a"}, (1, 2), "\ud800"):
            with self.subTest(value=repr(bad)), self.assertRaises(r.PolicyError):
                r.canonical_hash(bad)
        for bad in ('{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', '1.0', 'NaN', 'Infinity'):
            with self.subTest(value=bad), self.assertRaises(r.PolicyError):
                r.strict_json_loads(bad)

    def test_severity_full_power_set(self):
        # Independent tier oracle verifies every combination, not implementation order.
        names = list(r.SEVERITY)
        major = {"base_major", "breaking"}
        minor = {"app_major", "base_minor"}
        for selected in itertools.product((False, True), repeat=len(names)):
            causes = [name for name, yes in zip(names, selected) if yes]
            expected = "4.0.0" if major.intersection(causes) else "3.5.0" if minor.intersection(causes) else "3.4.6" if causes else None
            self.assertEqual(r.release_bump("3.4.5", causes), expected)
        with self.assertRaises(r.PolicyError):
            r.release_bump("1.2.3", ["typo"])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.state = r.new_state()
        self.call(r.set_desired, "app-a", "dev", A)
        self.call(r.bootstrap, "app-a", "dev", "1.0.0", D1, "pull:bootstrap")

    def call(self, function, *args):
        before = copy.deepcopy(self.state)
        result = function(self.state, *args, expected_revision=self.state["revision"])
        self.assertEqual(self.state, before, "transition mutated its input")
        self.assertIsNot(result, self.state)
        self.state = result
        return result

    def reject(self, function, *args, error=r.PolicyError, **kwargs):
        before = copy.deepcopy(self.state)
        kwargs.setdefault("expected_revision", self.state["revision"])
        with self.assertRaises(error):
            function(self.state, *args, **kwargs)
        self.assertEqual(self.state, before, "failed validation mutated input")

    def prepare(self, name="build-1", **changes):
        self.call(r.prepare, name, payload(**changes))

    def reserve(self, name="build-1", **changes):
        self.prepare(name, **changes)
        self.call(r.accept_base, name, D1)
        self.call(r.reserve, name)

    def publish(self, name="build-1", **changes):
        self.reserve(name, **changes)
        self.call(r.published, name, D2, "pull:" + name)

    def test_full_lifecycle_and_roundtrip(self):
        self.publish()
        self.call(r.commit, "build-1")
        self.assertEqual(self.state["channels"]["app-a/dev"], "1.0.1")
        self.assertEqual(self.state["entries"]["app-a/dev"]["1.0.1"]["digest"], D2)
        self.assertEqual(r.validate_state(r.strict_json_loads(json.dumps(self.state))), self.state)

    def test_all_transitions_retry_idempotently(self):
        self.publish()
        self.call(r.commit, "build-1")
        for function, args in [(r.prepare, ("build-1", payload())), (r.accept_base, ("build-1", D1)),
                               (r.reserve, ("build-1",)), (r.published, ("build-1", D2, "pull:build-1")),
                               (r.commit, ("build-1",)), (r.set_desired, ("app-a", "dev", A))]:
            before = copy.deepcopy(self.state)
            self.call(function, *args)
            self.assertEqual(self.state, before)

    def test_every_payload_field_bound(self):
        self.prepare()
        alternatives = {"image": "app-b", "channel": "main", "source_tag": "v1.0.1",
                        "source_commit": "e" * 40, "material_key": C, "plan_hash": C,
                        "desired_generation": 2, "causes": ["base_major"], "requires_base": False}
        for field, alternative in alternatives.items():
            with self.subTest(field=field):
                self.reject(r.prepare, "build-1", payload(**{field: alternative}), error=r.Conflict)

    def test_prepare_copies_caller_payload(self):
        data = payload()
        self.call(r.prepare, "build-1", data)
        data["causes"].append("base_major")
        self.assertEqual(self.state["intents"]["build-1"]["payload"]["causes"], ["app_patch"])

    def test_aba_generation_rejects_original_intent(self):
        self.publish()
        self.call(r.set_desired, "app-a", "dev", B)
        self.call(r.set_desired, "app-a", "dev", A)
        self.assertEqual(self.state["desired"]["app-a/dev"]["generation"], 3)
        self.reject(r.commit, "build-1", error=r.Conflict)
        self.assertNotIn("1.0.1", self.state["entries"]["app-a/dev"])
        self.reject(r.prepare, "new-old-build", payload(), error=r.Conflict)
        self.call(r.prepare, "fresh-build", payload(desired_generation=3))

    def test_write_once_base_digest(self):
        self.prepare()
        self.call(r.accept_base, "build-1", D1)
        self.reject(r.accept_base, "build-1", D2, error=r.Conflict)
        self.call(r.reserve, "build-1")
        self.reject(r.accept_base, "build-1", D2, error=r.Conflict)
        self.call(r.accept_base, "build-1", D1)

    def test_base_required_before_reservation(self):
        self.prepare()
        self.reject(r.reserve, "build-1")
        self.reject(r.accept_base, "build-1", "sha256:short")

    def test_base_image_can_publish_without_parent(self):
        self.prepare(requires_base=False)
        self.reject(r.accept_base, "build-1", D1)
        self.call(r.reserve, "build-1")
        self.call(r.published, "build-1", D2, "pull:base")
        self.call(r.commit, "build-1")

    def test_missing_history_fails_closed(self):
        self.state = r.set_desired(r.new_state(), "app-a", "dev", A, expected_revision=0)
        self.reject(r.prepare, "build-1", payload())

    def test_bootstrap_requires_verified_explicit_history(self):
        self.reject(r.bootstrap, "app-a", "dev", "1.0.0", D1, "pull:bootstrap")
        empty = r.new_state()
        with self.assertRaises(r.PolicyError):
            r.bootstrap(empty, "app-a", "dev", "1.0.0", D1, "pull", expected_revision=0)
        self.assertEqual(empty, r.new_state())

    def test_source_tag_collision(self):
        self.prepare()
        self.reject(r.prepare, "build-2", payload(source_commit="e" * 40), error=r.Conflict)
        self.call(r.prepare, "build-2", payload(source_tag="v1.0.1", source_commit="e" * 40))

    def test_concurrent_preparation_and_allocation_cas(self):
        revision = self.state["revision"]
        self.prepare("one")
        self.reject(r.prepare, "two", payload(), expected_revision=revision, error=r.Conflict)
        self.prepare("two")
        self.call(r.accept_base, "one", D1)
        self.call(r.accept_base, "two", D1)
        revision = self.state["revision"]
        self.call(r.reserve, "one")
        self.reject(r.reserve, "two", expected_revision=revision, error=r.Conflict)
        self.call(r.reserve, "two")
        self.assertEqual([self.state["intents"][i]["version"] for i in ("one", "two")], ["1.0.1", "1.0.2"])

    def test_retry_requires_current_revision(self):
        self.prepare()
        self.reject(r.prepare, "build-1", payload(), expected_revision=0, error=r.Conflict)
        self.reject(r.prepare, "build-1", payload(), expected_revision=True)

    def test_wrong_publication_digest_or_evidence_rejected(self):
        self.publish()
        self.reject(r.published, "build-1", D3, "pull:build-1", error=r.Conflict)
        self.reject(r.published, "build-1", D2, "different-evidence", error=r.Conflict)
        self.reject(r.published, "build-1", "latest", "pull:build-1")

    def test_unverified_or_unreserved_digest_never_enters_index(self):
        self.prepare()
        self.reject(r.published, "build-1", D2, "pull")
        self.reject(r.commit, "build-1")
        self.call(r.accept_base, "build-1", D1)
        self.call(r.reserve, "build-1")
        self.reject(r.published, "build-1", D2, "")
        self.reject(r.commit, "build-1")

    def test_stale_publication_retained_for_recovery(self):
        self.reserve()
        self.call(r.set_desired, "app-a", "dev", B)
        self.call(r.published, "build-1", D2, "orphan-pull")
        self.reject(r.commit, "build-1", error=r.Conflict)
        self.assertEqual(self.state["intents"]["build-1"]["status"], "published")
        self.call(r.abort, "build-1", "stale publication")
        self.assertEqual(self.state["intents"]["build-1"]["digest"], D2)

    def test_stale_prepared_intent_cannot_allocate(self):
        self.prepare()
        self.call(r.set_desired, "app-a", "dev", B)
        self.reject(r.accept_base, "build-1", D1, error=r.Conflict)

    def test_abort_never_reuses_reserved_version(self):
        self.reserve()
        self.call(r.abort, "build-1", "failed build")
        before = copy.deepcopy(self.state)
        self.call(r.abort, "build-1", "failed build")
        self.assertEqual(self.state, before)
        self.reject(r.abort, "build-1", "different reason", error=r.Conflict)
        self.reject(r.reserve, "build-1")
        self.reject(r.published, "build-1", D2, "pull")
        self.reserve("retry-new-intent")
        self.assertEqual(self.state["intents"]["retry-new-intent"]["version"], "1.0.2")

    def test_committed_release_cannot_abort(self):
        self.publish()
        self.call(r.commit, "build-1")
        self.reject(r.abort, "build-1", "undo")

    def test_rollback_preserves_counter_and_history(self):
        self.publish()
        self.call(r.commit, "build-1")
        before = copy.deepcopy(self.state)
        self.call(r.rollback, "app-a", "dev", "1.0.0")
        self.assertEqual(self.state["counters"], before["counters"])
        self.assertEqual(self.state["entries"], before["entries"])
        self.assertEqual(self.state["desired"], before["desired"])
        self.reserve("next")
        self.assertEqual(self.state["intents"]["next"]["version"], "1.0.2")
        self.reject(r.rollback, "app-a", "dev", "1.0.2")

    def test_out_of_order_commit_rejected_even_after_rollback(self):
        self.publish("old")
        self.publish("new")
        self.call(r.commit, "new")
        self.reject(r.commit, "old", error=r.Conflict)
        self.call(r.rollback, "app-a", "dev", "1.0.0")
        self.reject(r.commit, "old", error=r.Conflict)

    def test_bump_max_once_and_counters_are_per_slot(self):
        self.reserve(causes=["app_patch", "dependency", "base_minor", "breaking"])
        self.assertEqual(self.state["intents"]["build-1"]["version"], "2.0.0")
        self.call(r.set_desired, "app-a", "main", A)
        self.call(r.bootstrap, "app-a", "main", "7.5.3", D1, "pull:main")
        self.reserve("main-release", channel="main", causes=["base_minor", "app_major"])
        self.assertEqual(self.state["intents"]["main-release"]["version"], "7.6.0")

    def test_malformed_state_rejected_without_mutation(self):
        changes = [{"schema": 2}, {"revision": True}, {"surprise": {}},
                   {"channels": {"app-a/dev": "9.9.9"}}, {"counters": {}}]
        for change in changes:
            broken = {**copy.deepcopy(self.state), **change}
            before = copy.deepcopy(broken)
            with self.subTest(change=change), self.assertRaises(r.PolicyError):
                r.set_desired(broken, "app-a", "dev", B, expected_revision=broken["revision"])
            self.assertEqual(broken, before)

    def test_malformed_payload_is_atomic(self):
        for change in ({"causes": []}, {"causes": ["app_patch", "app_patch"]}, {"causes": ["bad"]},
                       {"source_commit": "abc123"}, {"desired_generation": True}, {"requires_base": "yes"},
                       {"material_key": "short"}, {"image": "../../escape"}, {"extra": 1}):
            with self.subTest(change=change):
                self.reject(r.prepare, "bad", payload(**change))

    def test_corrupted_durable_history_fails_closed(self):
        self.publish()
        self.call(r.commit, "build-1")
        for field, replacement in (("digest", D3), ("verification", "forged"),
                                   ("material_key", C), ("intent_id", "missing"), ("serial", 0)):
            broken = copy.deepcopy(self.state)
            broken["entries"]["app-a/dev"]["1.0.1"][field] = replacement
            with self.subTest(field=field), self.assertRaises(r.PolicyError):
                r.validate_state(broken)
        for replacement in ({"serial": 2, "last_version": "1.0.1"},
                            {"serial": 1, "last_version": "1.0.5"}):
            broken = copy.deepcopy(self.state)
            broken["counters"]["app-a/dev"] = replacement
            with self.subTest(counter=replacement), self.assertRaises(r.PolicyError):
                r.validate_state(broken)

    def test_corrupted_intent_binding_fails_closed(self):
        self.reserve()
        for field, replacement in (("payload_hash", C), ("status", "prepared"),
                                   ("base_digest", None), ("serial", 2), ("version", "0.9.0")):
            broken = copy.deepcopy(self.state)
            broken["intents"]["build-1"][field] = replacement
            with self.subTest(field=field), self.assertRaises(r.PolicyError):
                r.validate_state(broken)


if __name__ == "__main__":
    unittest.main()
