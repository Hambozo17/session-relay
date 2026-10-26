import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import accounts
import controller


class ProbeQueue:
    def __init__(self, responses):
        self.responses = list(responses)

    def call(self, _requests):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {}


def quota_response(five, weekly, reset_at=None, weekly_reset_at=None, ordinary=True):
    return {
        "ordinaryUsageAllowed": ordinary,
        "rateLimits": {
            "planType": "plus",
            "primary": {"windowDurationMins": 300, "usedPercent": 100 - five, "resetsAt": reset_at},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 100 - weekly, "resetsAt": weekly_reset_at},
        }
    }


class AllowanceGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.record = {"key": "account-key", "home": str(self.root / "home")}
        self.stack = patch.object(accounts, "LOCALDATA", self.root)
        self.stack.start()
        self.manager = accounts.AccountManager(rpc=object())
        self.manager.records = [self.record]
        self.identity = patch.object(accounts, "identity", return_value={"key": "account-key", "account_id": "account-id"})
        self.identity.start()
        self.protected = patch.object(accounts, "protected_json", return_value={})
        self.protected.start()

    def tearDown(self):
        self.protected.stop()
        self.identity.stop()
        self.stack.stop()
        self.temp.cleanup()

    def probe(self, *responses):
        queue = ProbeQueue(responses)
        with patch.object(accounts, "Probe", side_effect=lambda _home: queue):
            return [self.manager._probe(self.record, force=True) for _ in responses]

    def set_threshold(self, value):
        (self.root / "config.json").write_text(json.dumps({"threshold": value}), encoding="utf-8")

    def test_depleted_invalid_healthy_without_reset_timestamps_survives_restart(self):
        depleted, failed = self.probe(
            quota_response(0, 70),
            RuntimeError("transient credential refresh failure"),
        )
        self.assertEqual(depleted["_allowance_generation"], 0)
        self.assertTrue(depleted["_allowance_depleted"])
        self.assertFalse(failed["valid"])
        self.assertEqual(failed["_allowance_generation"], 0)
        self.assertTrue(failed["_allowance_depleted"])
        self.assertEqual(failed["_allowance_depleted_threshold"], 2.0)

        restarted = accounts.AccountManager(rpc=object())
        restarted.records = [self.record]
        healthy_queue = ProbeQueue([quota_response(60, 80)])
        with patch.object(accounts, "Probe", side_effect=lambda _home: healthy_queue):
            healthy = restarted._probe(self.record, force=True)
        self.assertIsNone(healthy["reset_at"])
        self.assertIsNone(healthy["weekly_reset_at"])
        self.assertEqual(healthy["_allowance_generation"], 1)
        self.assertFalse(healthy["_allowance_depleted"])
        self.assertNotIn("_allowance_depleted_threshold", healthy)
        self.assertTrue(controller.allowance_reset_since(
            {"allowance_generation": 0, "reset_at": None, "weekly_reset_at": None}, healthy))

    def test_healthy_probes_do_not_advance_generation_until_verified_depletion(self):
        healthy1, healthy2, depleted, healthy3 = self.probe(
            quota_response(60, 80), quota_response(55, 75),
            quota_response(0, 70), quota_response(50, 70),
        )
        self.assertEqual(healthy1["_allowance_generation"], 0)
        self.assertEqual(healthy2["_allowance_generation"], 0)
        self.assertFalse(healthy2["_allowance_depleted"])
        self.assertEqual(depleted["_allowance_generation"], 0)
        self.assertTrue(depleted["_allowance_depleted"])
        self.assertEqual(healthy3["_allowance_generation"], 1)

    def test_legacy_zero_cache_arms_transition_and_unknown_probe_preserves_it(self):
        self.manager.quotas[self.record["key"]] = {
            "valid": True, "plan": "plus", "ordinary_allowed": True,
            "five_remaining": 0, "weekly_remaining": 50,
            "reset_at": None, "weekly_reset_at": None, "updated_at": 1,
        }
        failed, partial = self.probe(
            RuntimeError("failed"),
            {"rateLimits": {"planType": "plus", "primary": {"windowDurationMins": 300, "usedPercent": 10}}},
        )
        self.assertTrue(failed["_allowance_depleted"])
        self.assertTrue(partial["_allowance_depleted"])
        self.assertEqual(partial["_allowance_generation"], 0)

    def test_verified_zero_arms_even_when_ordinary_usage_is_blocked(self):
        depleted, blocked_positive, usable = self.probe(
            quota_response(0, 70, ordinary=False),
            quota_response(40, 70, ordinary=False),
            quota_response(40, 70, ordinary=True),
        )
        self.assertFalse(depleted["ordinary_allowed"])
        self.assertTrue(depleted["_allowance_depleted"])
        self.assertIsNone(accounts._quota_capacity_state(blocked_positive))
        self.assertTrue(blocked_positive["_allowance_depleted"])
        self.assertEqual(blocked_positive["_allowance_generation"], 0)
        self.assertEqual(usable["_allowance_generation"], 1)
        self.assertFalse(usable["_allowance_depleted"])

    def test_default_threshold_depletes_at_one_and_two_but_not_three(self):
        self.assertEqual(accounts.normalize_threshold(None), 2.0)
        self.assertEqual(accounts.normalize_threshold(float("nan")), 2.0)
        self.assertEqual(accounts.normalize_threshold(100), 20.0)
        self.assertEqual(accounts.normalize_threshold(-1), 0.0)
        for remaining in (1, 2):
            self.assertEqual(accounts._quota_capacity_state({
                "valid": True, "plan": "plus", "ordinary_allowed": True,
                "five_remaining": remaining, "weekly_remaining": 70,
            }), "depleted")
        self.assertEqual(accounts._quota_capacity_state({
            "valid": True, "plan": "plus", "ordinary_allowed": True,
            "five_remaining": 3, "weekly_remaining": 70,
        }), "usable")

    def test_threshold_reset_releases_retry_and_destination_bans_after_restart(self):
        cases=((1,None,None),(2,1,1))
        for remaining,reset_at,weekly_reset_at in cases:
            with self.subTest(remaining=remaining,reset_at=reset_at):
                self.manager.quotas={}
                (self.root/"quota-cache.json").unlink(missing_ok=True)
                depleted,failed=self.probe(
                    quota_response(remaining,70,reset_at,weekly_reset_at),
                    RuntimeError("transient credential refresh failure"))
                self.assertEqual(depleted["_allowance_generation"],0)
                self.assertTrue(depleted["_allowance_depleted"])
                self.assertFalse(failed["valid"])
                self.assertEqual(failed["_allowance_generation"],0)
                self.assertTrue(failed["_allowance_depleted"])

                restarted=accounts.AccountManager(rpc=object())
                restarted.records=[self.record]
                healthy_queue=ProbeQueue([quota_response(100,100,reset_at,weekly_reset_at)])
                with patch.object(accounts,"Probe",side_effect=lambda _home:healthy_queue):
                    healthy=restarted._probe(self.record,force=True)
                self.assertEqual(healthy["_allowance_generation"],1)
                self.assertEqual(healthy.get("reset_at"),reset_at)
                self.assertEqual(healthy.get("weekly_reset_at"),weekly_reset_at)

                before_receipt={"allowance_generation":depleted["_allowance_generation"]}
                for field in ("reset_at","weekly_reset_at"):
                    if depleted.get(field) is not None:
                        before_receipt[field]=depleted[field]
                row={"id":"chat","status":"failed","error_kind":"quota",
                    "account_key":self.record["key"]}
                attempt={"app":"codex","state":"complete","target":self.record["key"],
                    "sessions":["chat"],"target_quota":before_receipt,
                    "quota_failed_sessions":{"chat":{"target_quota":before_receipt}}}

                self.assertFalse(controller.same_account_retry_allowed(
                    "codex",[row],depleted,[attempt]))
                self.assertEqual(controller.quota_failed_target_keys(
                    "codex",[row],[attempt],{self.record["key"]:depleted}),{self.record["key"]})
                self.assertTrue(controller.same_account_retry_allowed(
                    "codex",[row],healthy,[attempt]))
                self.assertEqual(controller.quota_failed_target_keys(
                    "codex",[row],[attempt],{self.record["key"]:healthy}),set())

    def test_configured_threshold_five_arms_and_releases_generation(self):
        self.set_threshold(5)
        depleted, healthy = self.probe(quota_response(5, 70), quota_response(6, 70))
        self.assertTrue(depleted["_allowance_depleted"])
        self.assertEqual(depleted["_allowance_depleted_threshold"], 5.0)
        self.assertEqual(healthy["_allowance_generation"], 1)
        self.assertFalse(healthy["_allowance_depleted"])

    def test_threshold_change_reclassifies_previous_quota_without_a_reset(self):
        self.set_threshold(5)
        (depleted,) = self.probe(quota_response(4, 70))
        self.assertTrue(depleted["_allowance_depleted"])
        self.assertEqual(depleted["_allowance_depleted_threshold"], 5.0)

        self.set_threshold(2)
        (same_quota,) = self.probe(quota_response(4, 70))
        self.assertEqual(same_quota["_allowance_generation"], 0)
        self.assertFalse(same_quota["_allowance_depleted"])
        self.assertNotIn("_allowance_depleted_threshold", same_quota)

    def test_legacy_zero_depletion_survives_invalid_probe_with_new_threshold(self):
        self.set_threshold(5)
        self.manager.quotas[self.record["key"]] = {
            "valid": True, "plan": "plus", "ordinary_allowed": True,
            "five_remaining": 0, "weekly_remaining": 50,
            "reset_at": None, "weekly_reset_at": None, "updated_at": 1,
            "_allowance_generation": 3, "_allowance_depleted": True,
        }
        (failed,) = self.probe(RuntimeError("offline"))
        self.assertEqual(failed["_allowance_generation"], 3)
        self.assertTrue(failed["_allowance_depleted"])
        self.assertEqual(failed["_allowance_depleted_threshold"], 0.0)
        (healthy,) = self.probe(quota_response(6, 70))
        self.assertEqual(healthy["_allowance_generation"], 4)
        self.assertFalse(healthy["_allowance_depleted"])

    def test_probe_desktop_outer_failure_persists_generation_metadata(self):
        (depleted,) = self.probe(quota_response(0, 70))
        with patch.object(self.manager, "_desktop_auth", side_effect=OSError("saved auth unavailable")):
            failed = self.manager.probe_desktop(self.record, force=True)
        self.assertFalse(failed["valid"])
        self.assertEqual(failed["_allowance_generation"], depleted["_allowance_generation"])
        self.assertTrue(failed["_allowance_depleted"])
        persisted = json.loads((self.root / "quota-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted[self.record["key"]]["_allowance_generation"], 0)
        self.assertTrue(persisted[self.record["key"]]["_allowance_depleted"])

    def test_generation_must_strictly_increase_to_release_an_attempt(self):
        receipt = {"allowance_generation": 4, "reset_at": None, "weekly_reset_at": None}
        self.assertFalse(controller.allowance_reset_since(receipt, {"_allowance_generation": 4}))
        self.assertTrue(controller.allowance_reset_since(receipt, {"_allowance_generation": 5}))
        self.assertTrue(controller.allowance_reset_since(
            {"allowance_generation": 4, "reset_at": 10}, {"_allowance_generation": 4, "reset_at": 11}))


if __name__ == "__main__":
    unittest.main()
