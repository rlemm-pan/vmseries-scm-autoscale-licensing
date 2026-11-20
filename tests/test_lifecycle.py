"""Offline tests: fake HTTP transport + fake AWS clients. Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vmseries_lifecycle import csp, scm  # noqa: E402
from vmseries_lifecycle.aws import AwsOps, extract_d_authcode, _parse_kv  # noqa: E402
from vmseries_lifecycle.config import Deps, Settings  # noqa: E402
from vmseries_lifecycle.handlers import reconcile, scale_in  # noqa: E402
from vmseries_lifecycle.http import ApiError, Response  # noqa: E402

AUTH = "D1234567"


class FakeHttp:
    """Routes requests to handler callables keyed by (METHOD, url-substring)."""

    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.routes: List[Tuple[str, str, Any]] = []

    def on(self, method: str, frag: str, fn):
        self.routes.append((method, frag, fn))
        return self

    def request(self, method, url, *, headers=None, params=None, json_body=None, form=None, retry=True):
        self.calls.append((method, url, {"headers": headers, "params": params, "json": json_body, "form": form}))
        for m, frag, fn in self.routes:
            if m == method and frag in url:
                out = fn(params=params, json_body=json_body, headers=headers, form=form)
                if isinstance(out, ApiError):
                    raise out
                return Response(200, __import__("json").dumps(out))
        raise AssertionError(f"no route for {method} {url}")


def token_route(**_):
    return {"access_token": "tok", "expires_in": 3600}


class CspState:
    """Stateful fake of the licensing service."""

    def __init__(self, serials):
        self.serials = list(serials)
        self.profiles: List[Dict[str, Any]] = []

    def install(self, http: FakeHttp):
        http.on("POST", "token.oauth2", token_route)
        http.on("GET", "/firewallserialnumbers", lambda **_: {"vm_series": list(self.serials)})
        http.on("DELETE", "/firewall/deactivate", self._deactivate)
        http.on("GET", "/deploymentProfile", lambda **_: {"data": self.profiles})
        http.on("POST", "/deploymentProfile", self._create)
        http.on("DELETE", "/deploymentProfile/", lambda **_: {"isDeleted": True, "dAuthcode": AUTH})
        return self

    def _deactivate(self, params, **_):
        req = params["serial_numbers"].split(",")
        ok = [s for s in req if s in self.serials]
        self.serials = [s for s in self.serials if s not in ok]
        return {"success": ok, "failed": [s for s in req if s not in ok], "auth_code": AUTH, "failure_reason": []}

    def _create(self, json_body, **_):
        self.profiles.append({"profileName": json_body["name"], "dAuthCode": AUTH})
        return {"profile_id": 1, "auth_code": AUTH, "success": True, "message": "saved"}


def make_csp(http) -> csp.CspLicensingClient:
    return csp.CspLicensingClient("id", "secret", http=http)


def make_scm(http, devices) -> scm.ScmClient:
    http.on("POST", "oauth2/access_token", token_route)
    http.on("GET", "/config/setup/v1/devices", lambda **_: {"data": devices, "total": len(devices)})
    http.on("PUT", "/config/setup/v1/devices/", lambda json_body, **_: json_body)
    return scm.ScmClient("sid", "ssecret", "1234567890", http=http)


class FakeAws(AwsOps):
    def __init__(self, instances, asg_ids=None):
        super().__init__()
        self.instances = {i["InstanceId"]: i for i in instances}
        self.asg_ids = asg_ids if asg_ids is not None else list(self.instances)
        self.tags: List[Tuple[str, str, str]] = []
        self.completed: List[Dict[str, Any]] = []
        self.alerts: List[Tuple[str, Any]] = []

    def describe_instances(self, ids):
        return [self.instances[i] for i in ids if i in self.instances]

    def asg_instance_ids(self, asg_name):
        return list(self.asg_ids)

    def tag_instance(self, iid, k, v):
        self.tags.append((iid, k, v))

    def complete_lifecycle(self, **kw):
        self.completed.append(kw)

    def notify(self, topic, subject, message):
        self.alerts.append((subject, message))


def inst(iid, ip, serial=None):
    i = {"InstanceId": iid, "NetworkInterfaces": [
        {"Attachment": {"DeviceIndex": 0}, "PrivateIpAddress": "10.0.0.1"},
        {"Attachment": {"DeviceIndex": 1}, "PrivateIpAddress": ip},
    ], "Tags": []}
    if serial:
        i["Tags"].append({"Key": "paloalto:serial", "Value": serial})
    return i


def deps(http, aws, devices=None, dry_run=True) -> Deps:
    settings = Settings(api_secret_id="x", asg_name="asg", dry_run=dry_run)
    scm_client = make_scm(http, devices) if devices is not None else None
    return Deps(settings, aws, make_csp(http), scm_client, AUTH)


LIFECYCLE_EVENT = {"detail": {
    "EC2InstanceId": "i-1", "LifecycleHookName": "hook", "AutoScalingGroupName": "asg",
    "LifecycleActionToken": "tok-1",
}}


class SpecTests(unittest.TestCase):
    def test_flex_body(self):
        body = csp.DeploymentProfileSpec(credit_pool_id=9, name="p", firewall_quantity=2, vcpu_quantity=16,
                                         subscriptions=["ATP"]).to_body()
        self.assertEqual(body["panOS"], "10.0.4_or-above")
        self.assertEqual(body["vCpuQuantity"], 16)
        self.assertNotIn("vmModel", body)

    def test_fixed_model_derives_panos(self):
        body = csp.DeploymentProfileSpec(credit_pool_id=9, name="p", firewall_quantity=1, vm_model=700).to_body()
        self.assertEqual(body["panOS"], "10.0.3_or-below")
        self.assertEqual(body["vmModel"], 700)

    def test_validation(self):
        with self.assertRaises(ValueError):
            csp.DeploymentProfileSpec(credit_pool_id=9, name="p", firewall_quantity=1).to_body()
        with self.assertRaises(ValueError):
            csp.DeploymentProfileSpec.from_dict({"credit_pool_id": 1, "name": "p", "firewall_quantity": 1,
                                                 "vcpu_quantity": 4, "bogus": 1})

    def test_example_spec_loads(self):
        path = os.path.join(os.path.dirname(__file__), "..", "examples", "profile-spec.json")
        import json
        with open(path) as fh:
            spec = csp.DeploymentProfileSpec.from_dict(json.load(fh))
        self.assertEqual(spec.vcpu_quantity, 16)


class CspClientTests(unittest.TestCase):
    def test_token_header_and_caching(self):
        http = FakeHttp()
        CspState(["S1"]).install(http)
        c = make_csp(http)
        c.list_serials(AUTH)
        c.list_serials(AUTH)
        token_calls = [x for x in http.calls if "token.oauth2" in x[1]]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(token_calls[0][2]["form"]["scope"], "fwflex-service")
        self.assertEqual(http.calls[-1][2]["headers"], {"token": "tok"})

    def test_401_refreshes_token_once(self):
        http = FakeHttp()
        state = {"n": 0}

        def flaky(**_):
            state["n"] += 1
            return ApiError("GET", "u", 401, "expired") if state["n"] == 1 else {"vm_series": []}

        http.on("POST", "token.oauth2", token_route).on("GET", "/firewallserialnumbers", flaky)
        self.assertEqual(make_csp(http).list_serials(AUTH), [])
        self.assertEqual(len([x for x in http.calls if "token.oauth2" in x[1]]), 2)

    def test_http200_error_envelope_raises_not_empty(self):
        """Real TMS behaviour: HTTP 200 + {"status":400,"message":"Invalid Token"}."""
        http = FakeHttp()
        envelope = {"timestamp": "t", "status": 400, "error": "Bad Request", "message": "Invalid Token", "path": ""}
        http.on("POST", "token.oauth2", token_route)
        http.on("GET", "/firewallserialnumbers", lambda **_: envelope)
        with self.assertRaises(ApiError) as ctx:
            make_csp(http).list_serials(AUTH)
        self.assertEqual(ctx.exception.status, 401)
        # treated as auth failure -> token refreshed once before giving up
        self.assertEqual(len([x for x in http.calls if "token.oauth2" in x[1]]), 2)

    def test_scale_in_does_not_report_false_success_on_bad_token(self):
        http = FakeHttp()
        envelope = {"status": 400, "error": "Bad Request", "message": "Invalid Token"}
        http.on("POST", "token.oauth2", token_route)
        http.on("GET", "/firewallserialnumbers", lambda **_: envelope)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws))
        self.assertFalse(r["deactivated"])
        self.assertEqual(len(aws.alerts), 1)

    def test_ensure_profile_idempotent(self):
        http = FakeHttp()
        st = CspState([]).install(http)
        c = make_csp(http)
        spec = csp.DeploymentProfileSpec(credit_pool_id=9, name="asg", firewall_quantity=2, vcpu_quantity=4)
        self.assertEqual(c.ensure_profile(spec), (AUTH, True))
        self.assertEqual(c.ensure_profile(spec), (AUTH, False))
        self.assertEqual(len(st.profiles), 1)

    def test_deactivate_skips_inactive(self):
        http = FakeHttp()
        CspState(["S1", "S2"]).install(http)
        res = make_csp(http).deactivate(AUTH, ["S1", "GONE"])
        self.assertTrue(res.ok)
        self.assertEqual(res.success, ["S1"])
        self.assertEqual(res.already_inactive, ["GONE"])
        deact = [x for x in http.calls if "/firewall/deactivate" in x[1]]
        self.assertEqual(deact[0][2]["params"], {"auth_code": AUTH, "serial_numbers": "S1"})

    def test_teardown_deactivates_then_deletes(self):
        http = FakeHttp()
        st = CspState(["S1", "S2"]).install(http)
        summary = make_csp(http).teardown(AUTH)
        self.assertEqual(sorted(summary["deactivated"]), ["S1", "S2"])
        self.assertTrue(summary["profile_deleted"])
        self.assertEqual(st.serials, [])
        methods = [(m, u.split("/tms/v1")[-1].split("?")[0]) for m, u, _ in http.calls if "tms" in u]
        self.assertEqual(methods[-1], ("DELETE", f"/deploymentProfile/{AUTH}"))


class HelperTests(unittest.TestCase):
    def test_authcode_extraction(self):
        self.assertEqual(extract_d_authcode("I1111111, D7654321"), "D7654321")
        self.assertIsNone(extract_d_authcode(""))

    def test_kv_parse(self):
        self.assertEqual(_parse_kv("authcodes=D1;panorama-server=cloud")["panorama-server"], "cloud")


class ScaleInTests(unittest.TestCase):
    def test_elimination_when_firewall_never_reached_scm(self):
        # i-1 (terminating) is untagged and not in SCM; i-2 is tagged S2; S1 is the only unclaimed serial.
        http = FakeHttp()
        st = CspState(["S1", "S2"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10"), inst("i-2", "10.1.1.20", serial="S2")])
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws, []))
        self.assertEqual((r["serial"], r["source"]), ("S1", "elimination"))
        self.assertTrue(r["deactivated"])
        self.assertEqual(st.serials, ["S2"])

    def test_elimination_refuses_when_ambiguous(self):
        # another untagged instance (i-3) could own either unclaimed serial -> do nothing
        http = FakeHttp()
        st = CspState(["S1", "S3"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10"), inst("i-3", "10.1.1.30")])
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws, []))
        self.assertIsNone(r["serial"])
        self.assertFalse(r["deactivated"])
        self.assertEqual(st.serials, ["S1", "S3"])
        self.assertEqual(len(aws.completed), 1)   # instance still released

    def test_tagged_instance(self):
        http = FakeHttp()
        st = CspState(["S1", "S2"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        devs = [{"id": "11111111-2222-3333-4444-555555555555", "name": "S1", "ip_address": "10.1.1.10", "folder": "AWS-NVIDIA-GWLB"}]
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws, devs))
        self.assertTrue(r["deactivated"])
        self.assertTrue(r["scm_decommissioned"])
        self.assertEqual(st.serials, ["S2"])
        self.assertEqual(aws.completed[0]["token"], "tok-1")
        self.assertEqual(aws.alerts, [])
        put = [x for x in http.calls if x[0] == "PUT"][0]
        self.assertEqual(put[2]["json"], {"folder": "Decommissioned"})

    def test_untagged_falls_back_to_scm_ip(self):
        http = FakeHttp()
        st = CspState(["S9"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.99")])
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws, [{"id": "11111111-2222-3333-4444-555555555555", "name": "S9", "ip_address": "10.1.1.99"}]))
        self.assertEqual((r["serial"], r["source"]), ("S9", "scm-ip"))
        self.assertEqual(st.serials, [])

    def test_failure_alerts_but_still_completes_lifecycle(self):
        http = FakeHttp()
        http.on("POST", "token.oauth2", token_route)
        http.on("GET", "/firewallserialnumbers", lambda **_: ApiError("GET", "u", 500, "boom"))
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        r = scale_in.handler(LIFECYCLE_EVENT, deps=deps(http, aws))
        self.assertFalse(r["deactivated"])
        self.assertEqual(len(aws.alerts), 1)
        self.assertEqual(len(aws.completed), 1)

    def test_scm_failure_does_not_block_deactivation(self):
        http = FakeHttp()
        st = CspState(["S1"]).install(http)
        http.on("POST", "oauth2/access_token", token_route)
        http.on("GET", "/config/setup/v1/devices", lambda **_: ApiError("GET", "u", 403, "nope"))
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        d = Deps(Settings(api_secret_id="x"), aws, make_csp(http),
                 scm.ScmClient("a", "b", "c", http=http), AUTH)
        r = scale_in.handler(LIFECYCLE_EVENT, deps=d)
        self.assertTrue(r["deactivated"])
        self.assertEqual(st.serials, [])
        self.assertEqual(len(aws.alerts), 1)


class ReconcileTests(unittest.TestCase):
    def test_maps_untagged_instances(self):
        http = FakeHttp()
        CspState(["S1"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10")])
        r = reconcile.handler({}, deps=deps(http, aws, [{"id": "11111111-2222-3333-4444-555555555555", "name": "S1", "ip_address": "10.1.1.10",
                                                         "is_connected": True}]))
        self.assertEqual(aws.tags, [("i-1", "paloalto:serial", "S1")])
        self.assertEqual(r["orphans"], [])

    def test_orphan_dry_run_reports_only(self):
        http = FakeHttp()
        st = CspState(["S1", "ORPHAN"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        r = reconcile.handler({}, deps=deps(http, aws, []))
        self.assertEqual(r["orphans"], ["ORPHAN"])
        self.assertIn("ORPHAN", st.serials)
        self.assertEqual(len(aws.alerts), 1)

    def test_orphan_live_mode_deactivates(self):
        http = FakeHttp()
        st = CspState(["S1", "ORPHAN"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        r = reconcile.handler({}, deps=deps(http, aws, [{"id": "11111111-2222-3333-4444-555555555555", "name": "ORPHAN", "is_connected": False}], dry_run=False))
        self.assertEqual(r["deactivated"], ["ORPHAN"])
        self.assertEqual(st.serials, ["S1"])

    def test_defers_when_instance_unmapped(self):
        http = FakeHttp()
        st = CspState(["S1", "NEWBOOT"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1"), inst("i-2", "10.1.1.11")])
        r = reconcile.handler({}, deps=deps(http, aws, [], dry_run=False))
        self.assertTrue(r["deferred"])
        self.assertIn("NEWBOOT", st.serials)

    def test_never_deactivates_connected_device(self):
        http = FakeHttp()
        st = CspState(["S1", "OTHER"]).install(http)
        aws = FakeAws([inst("i-1", "10.1.1.10", serial="S1")])
        r = reconcile.handler({}, deps=deps(http, aws, [{"id": "11111111-2222-3333-4444-555555555555", "name": "OTHER", "is_connected": True}], dry_run=False))
        self.assertEqual(r["skipped_connected"], ["OTHER"])
        self.assertIn("OTHER", st.serials)



class ScmSetupAndStatusTests(unittest.TestCase):
    UUID = "11111111-2222-3333-4444-555555555555"

    def scm(self, http, folders=(), snippets=(), variables=(), devices=({"name": "S1", "folder": "A", "is_connected": True, "threat_version": "9000-1"},),
            jobs=({"id": "43", "parent_id": "42", "status_str": "FIN", "result_str": "OK"},)):
        http.on("POST", "oauth2/access_token", token_route)
        http.on("GET", "/config/setup/v1/devices", lambda **_: {"data": list(devices), "total": len(devices)})
        http.on("GET", "/config/setup/v1/folders", lambda **_: {"data": list(folders), "total": len(folders)})
        http.on("GET", "/config/setup/v1/snippets", lambda **_: {"data": list(snippets), "total": len(snippets)})
        http.on("GET", "/config/setup/v1/variables", lambda **_: {"data": list(variables), "total": len(variables)})
        for kind in ("folders", "snippets", "variables"):
            http.on("POST", f"/config/setup/v1/{kind}", lambda json_body, **_: {"id": "new", **json_body})
        http.on("POST", "candidate:push", lambda json_body, **_: {"success": True, "job_id": "42"})
        http.on("GET", "/config/operations/v1/jobs/42",
                lambda **_: {"data": [{"id": "42", "status_str": "FIN", "result_str": "OK"}]})
        http.on("GET", "/config/operations/v1/jobs", lambda **_: {"data": list(jobs)})
        return scm.ScmClient("id", "secret", "123", http=http)

    def test_device_serial_never_returns_uuid(self):
        self.assertIsNone(scm.device_serial({"id": self.UUID}))
        self.assertEqual(scm.device_serial({"id": self.UUID, "name": "007951000123456"}), "007951000123456")
        self.assertEqual(scm.device_serial({"serial_number": "0079A", "name": "fw-1"}), "0079A")

    def test_device_status(self):
        st = scm.device_status({"id": self.UUID, "name": "0079", "folder": "F", "is_connected": True, "junk": 1})
        self.assertEqual(st["serial"], "0079")
        self.assertTrue(st["connected"])
        self.assertNotIn("junk", st)

    def test_profile_body_always_has_panorama(self):
        body = csp.DeploymentProfileSpec.from_dict(
            {"credit_pool_id": 1, "name": "n", "firewall_quantity": 1, "vcpu_quantity": 4}).to_body()
        self.assertEqual(body["panorama"], [])

    def test_setup_creates_missing_and_skips_existing(self):
        from vmseries_lifecycle.cli import scm_setup
        http = FakeHttp()
        client = self.scm(http, folders=[{"name": "Decommissioned", "id": "f1", "parent": "ngfw-shared"}])
        spec = {"snippets": [{"name": "S"}],
                "folders": [{"name": "A", "snippets": ["S"]}, {"name": "Decommissioned"}],
                "variables": [{"name": "cidr", "type": "ip-netmask", "value": "10.0.0.0/16", "folder": "A"}],
                "push_folders": ["A"]}
        out = scm_setup(client, spec, push=True)
        self.assertEqual([f["action"] for f in out["folders"]], ["created", "exists"])
        self.assertEqual(out["variables"][0]["variable"], "$cidr")
        posts = [(c[1].split("/v1/")[1], c[2]["json"]) for c in http.calls if c[0] == "POST" and "/config/" in c[1]]
        self.assertEqual([p[0] for p in posts], ["snippets", "folders", "variables", "config-versions/candidate:push"])
        self.assertEqual(posts[1][1], {"name": "A", "parent": "ngfw-shared", "snippets": ["S"]})
        self.assertEqual(posts[3][1], {"folders": ["A"], "description": "vmseries-lifecycle scm-setup"})

    def test_setup_dry_run_writes_nothing_and_skips_missing_folder_lookup(self):
        from vmseries_lifecycle.cli import scm_setup
        http = FakeHttp()
        client = self.scm(http)
        spec = {"folders": [{"name": "A"}],
                "variables": [{"name": "$v", "type": "count", "value": "1", "folder": "A"}]}
        out = scm_setup(client, spec, dry_run=True)
        self.assertEqual(out["variables"][0]["action"], "would-create")
        self.assertFalse([c for c in http.calls if c[0] == "POST" and "/config/" in c[1]])
        self.assertFalse([c for c in http.calls if "/variables" in c[1]])

    def test_push_skips_folders_without_devices(self):
        http = FakeHttp()
        client = self.scm(http, devices=[])
        r = client.push_candidate(["A"])
        self.assertTrue(r["skipped"])
        self.assertFalse([c for c in http.calls if "candidate:push" in c[1]])

    def test_wait_job(self):
        http = FakeHttp()
        client = self.scm(http)
        job = client.wait_job("42", interval=0)
        self.assertTrue(job["ok"])
        self.assertEqual(job["children"][0]["id"], "43")

    def test_wait_job_reports_failed_child_push(self):
        http = FakeHttp()
        client = self.scm(http, jobs=[{"id": "43", "parent_id": "42", "status_str": "PUSHFAIL", "result_str": "FAIL",
                                       "details": '{"errors": ["Validation Error:", "Commit failed"]}'}])
        job = client.wait_job("42", interval=0)
        self.assertFalse(job["ok"])
        self.assertIn("Commit failed", job["children"][0]["errors"])

    def test_push_skips_devices_without_content(self):
        http = FakeHttp()
        client = self.scm(http, devices=[{"name": "S1", "folder": "A", "is_connected": True, "threat_version": "0"}])
        r = client.push_candidate(["A"])
        self.assertEqual(r["reason"], {"A": "content not installed yet"})
        self.assertFalse([c for c in http.calls if "candidate:push" in c[1]])

    def test_cross_check(self):
        from vmseries_lifecycle.cli import cross_check
        statuses = [scm.device_status(d) for d in (
            {"name": "A", "is_connected": True}, {"name": "B", "is_connected": False}, {"name": "X"})]
        r = cross_check(["A", "B", "C"], statuses)
        self.assertEqual(r["licensed_and_connected"], ["A"])
        self.assertEqual(r["licensed_not_connected"], ["B"])
        self.assertEqual(r["licensed_not_in_scm"], ["C"])
        self.assertEqual(r["in_scm_not_licensed_on_code"], ["X"])

    def test_link_status_from_auto_added_device(self):
        from vmseries_lifecycle.cli import link_status
        http = FakeHttp()
        http.on("POST", "token.oauth2", token_route)
        http.on("GET", "/deploymentProfile/", lambda **_: {"data": {"profileName": "p", "status": "Updated"}})
        client = self.scm(http)
        http.on("GET", "/subscription/v1/instances", lambda **_: [])
        http.on("GET", "/subscription/v1/tenant_devices", lambda **_: {"items": [
            {"serial_number": "S1", "source": "deployment-profile", "extra": {"deployment_profile_id": AUTH}},
            {"serial_number": "S2", "source": "das-ui"}]})
        r = link_status(make_csp(http), AUTH, client)
        self.assertIs(r["linked"], True)
        self.assertEqual(r["auto_added_devices"], ["S1"])

    def test_cross_check_tenant_associations(self):
        from vmseries_lifecycle.cli import cross_check
        tenant = [{"serial_number": "A", "source": "deployment-profile",
                   "associations": [{"app_id": "strata_cloud_manager", "association_status": "completed"}]},
                  {"serial_number": "B", "source": "das-ui",
                   "associations": [{"app_id": "prisma_access", "association_status": "completed"}]}]
        r = cross_check(["A", "B", "C"], [], tenant, AUTH)
        self.assertEqual(r["licensed_not_in_tenant"], ["C"])
        self.assertEqual(r["in_tenant_without_scm"], ["B"])
        self.assertEqual(r["added_manually"], ["B"])

    def test_link_status(self):
        from vmseries_lifecycle.cli import link_status

        def run(status, with_scm=False):
            http = FakeHttp()
            http.on("POST", "token.oauth2", token_route)
            http.on("GET", "/deploymentProfile/", lambda **_: {"data": {"profileName": "p", "status": status}})
            client = None
            if with_scm:
                client = self.scm(http)
                http.on("GET", "/subscription/v1/instances", lambda **_: [
                    {"app_id": "strata_cloud_manager", "provisioning_status": "complete", "status": "running",
                     "_internal": {"updated_at": 1795090818000}}])
            return link_status(make_csp(http), AUTH, client)

        r = run("Created")
        self.assertIs(r["linked"], False)
        self.assertIn("Ready for Activation", r["next_step"])
        r = run("Updated", with_scm=True)
        self.assertEqual(r["linked"], "unknown")
        self.assertEqual(r["scm_instances"][0]["app_id"], "strata_cloud_manager")
        self.assertEqual(r["scm_instances"][0]["updated"], "2026-11-19T12:20Z")
        r = run("Transferred")
        self.assertIs(r["linked"], True)
        self.assertNotIn("next_step", r)

if __name__ == "__main__":
    unittest.main()
