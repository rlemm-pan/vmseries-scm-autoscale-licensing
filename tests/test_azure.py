"""Offline tests for the Azure lifecycle handler (fake ARM/blob, fake CSP + SCM HTTP)."""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_lifecycle import AUTH, CspState, FakeHttp, make_csp, make_scm  # noqa: E402
from vmseries_lifecycle import azure  # noqa: E402
from vmseries_lifecycle.handlers import azure_lifecycle as al  # noqa: E402
from vmseries_lifecycle.http import ApiError, Response  # noqa: E402


class FakeAz:
    def __init__(self, vms: List[Dict[str, Any]]):
        self.vms = vms
        self.tags: List[tuple] = []

    def scale_set_vms(self, name, idx=0):
        return [dict(v, tags=dict(v.get("tags", {}))) for v in self.vms]

    def tag(self, rid, k, v):
        self.tags.append((rid, k, v))


class FakeMap:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def all(self):
        return dict(self.records)

    def put(self, name, rec):
        self.records[name] = rec

    def delete(self, name):
        self.records.pop(name, None)


def vm(name, ip, tags=None):
    return {"name": name, "id": f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
            "vm_id": name + "-id", "mgmt_ip": ip, "tags": tags or {}}


def deps(http, az, smap, devices, dry_run=True):
    s = al.AzureSettings(subscription_id="s", resource_group="rg", vmss_name="fw", key_vault_uri="https://kv",
                         map_account="acct", auth_code=AUTH, dry_run=dry_run)
    return al.AzureDeps(s, az, smap, make_csp(http), make_scm(http, devices), AUTH)


class AzureLifecycleTests(unittest.TestCase):
    def test_maps_new_vm_by_mgmt_ip(self):
        http = FakeHttp()
        CspState(["S1"]).install(http)
        az, smap = FakeAz([vm("fw_1", "10.0.0.4")]), FakeMap()
        r = al.process(deps(http, az, smap, [{"id": "S1", "name": "S1", "ip_address": "10.0.0.4", "is_connected": True}]))
        self.assertEqual(r["mapped"], [{"vm": "fw_1", "serial": "S1"}])
        self.assertEqual(smap.records["fw_1"]["serial"], "S1")
        self.assertEqual(az.tags[0][1:], ("paloalto-serial", "S1"))
        self.assertEqual(r["orphans"], [])

    def test_gone_vm_is_deactivated_and_decommissioned(self):
        http = FakeHttp()
        st = CspState(["S1", "S2"]).install(http)
        az = FakeAz([vm("fw_2", "10.0.0.5", tags={"paloalto-serial": "S2"})])
        smap = FakeMap({"fw_1": {"serial": "S1"}, "fw_2": {"serial": "S2"}})
        devs = [{"id": "S1", "name": "S1", "is_connected": True, "folder": "F"},
                {"id": "S2", "name": "S2", "ip_address": "10.0.0.5", "is_connected": True}]
        r = al.process(deps(http, az, smap, devs))   # dry-run does NOT block the scale-in action
        self.assertEqual(r["gone_deactivated"], [{"vm": "fw_1", "serial": "S1", "scm_decommissioned": True}])
        self.assertEqual(st.serials, ["S2"])
        self.assertNotIn("fw_1", smap.records)
        put = [c for c in http.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2]["json"], {"folder": "Decommissioned"})

    def test_record_kept_when_deactivation_fails(self):
        http = FakeHttp()
        # serial still licensed, but the deactivate call reports it as failed (routes match first-wins)
        http.on("DELETE", "/firewall/deactivate", lambda **_: {"success": [], "failed": ["S9"], "auth_code": AUTH,
                                                              "failure_reason": ["backend error"]})
        CspState(["S9"]).install(http)
        smap = FakeMap({"fw_1": {"serial": "S9"}})
        r = al.process(deps(http, FakeAz([]), smap, []))
        self.assertEqual(r["gone_deactivated"], [])
        self.assertIn("fw_1", smap.records)
        self.assertTrue(r["errors"])

    def test_gone_serial_already_inactive_cleans_record(self):
        http = FakeHttp()
        CspState([]).install(http)   # no longer on the auth code (e.g. deactivated by hand)
        smap = FakeMap({"fw_1": {"serial": "S9"}})
        r = al.process(deps(http, FakeAz([]), smap, []))
        self.assertEqual(r["gone_deactivated"][0]["serial"], "S9")
        self.assertNotIn("fw_1", smap.records)

    def test_orphans_deferred_while_vm_unmapped(self):
        http = FakeHttp()
        st = CspState(["S7"]).install(http)
        r = al.process(deps(http, FakeAz([vm("fw_3", "10.0.0.9")]), FakeMap(), [], dry_run=False))
        self.assertEqual((r["orphans"], r["deferred"], r["unmapped_vms"]), (["S7"], True, ["fw_3"]))
        self.assertEqual(st.serials, ["S7"])

    def test_orphan_deactivated_when_not_dry_run(self):
        http = FakeHttp()
        st = CspState(["S7"]).install(http)
        r = al.process(deps(http, FakeAz([]), FakeMap(), [], dry_run=False))
        self.assertEqual(r["orphans_deactivated"], ["S7"])
        self.assertEqual(st.serials, [])


class SerialMapAndTokenTests(unittest.TestCase):
    def test_blob_list_parses_xml_and_404_is_none(self):
        class Http:
            def __init__(self):
                self.calls = []

            def request(self, method, url, *, headers=None, params=None, json_body=None, form=None, retry=True):
                self.calls.append((method, url, params, headers))
                if params and params.get("comp") == "list":
                    return Response(200, "<EnumerationResults><Blobs><Blob><Name>fw_1</Name></Blob>"
                                         "<Blob><Name>fw_2</Name></Blob></Blobs><NextMarker/></EnumerationResults>")
                if method == "GET" and url.endswith("/fw_2"):
                    raise ApiError("GET", url, 404, "BlobNotFound")
                return Response(200, '{"serial": "S1"}')

        class Tok:
            def get(self, r):
                return "t"

        ops = azure.AzureOps("s", "rg", http=Http(), tokens=Tok())
        m = azure.SerialMap("acct", "serial-map", ops)
        self.assertEqual(m.names(), ["fw_1", "fw_2"])
        self.assertEqual(m.all(), {"fw_1": {"serial": "S1"}})
        self.assertEqual(ops.http.calls[0][3]["x-ms-version"], azure.BLOB_VERSION)

    def test_token_uses_managed_identity_when_present(self):
        class Http:
            def request(self, method, url, *, headers=None, params=None, **_):
                assert headers["X-IDENTITY-HEADER"] == "h" and params["resource"] == azure.VAULT_RESOURCE
                return Response(200, '{"access_token": "mi-token", "expires_on": "9999999999"}')

        old = dict(os.environ)
        os.environ.update(IDENTITY_ENDPOINT="http://localhost/msi", IDENTITY_HEADER="h")
        try:
            self.assertEqual(azure.TokenProvider(Http()).get(azure.VAULT_RESOURCE), "mi-token")
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
