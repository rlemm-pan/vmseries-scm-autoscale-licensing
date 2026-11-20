"""Azure operations for the VM-Series lifecycle (stdlib only, like the rest of the package).

Auth: inside an Azure Function the managed identity (IDENTITY_ENDPOINT / IDENTITY_HEADER) is used;
on a workstation it falls back to the signed-in Azure CLI (`az account get-access-token`).

What it needs:
  - ARM: list VMs and NICs of the scale set's resource group, merge tags  (Reader + Tag Contributor)
  - Key Vault: read the API-credentials secret                           (Key Vault Secrets User)
  - Blob storage: a tiny `vm name -> serial` map, because a deleted VM's tags are gone with it
                                                                          (Storage Blob Data Contributor)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

from .http import ApiError, HttpTransport

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
ARM_RESOURCE = "https://management.azure.com/"
VAULT_RESOURCE = "https://vault.azure.net"
STORAGE_RESOURCE = "https://storage.azure.com/"
SERIAL_TAG = "paloalto-serial"
BLOB_VERSION = "2021-08-06"
COMPUTE_API = "2024-07-01"
NETWORK_API = "2024-05-01"


class TokenProvider:
    """Managed identity in Azure Functions / App Service; Azure CLI elsewhere. Tokens cached per resource."""

    def __init__(self, http: HttpTransport, clock: Callable[[], float] = time.time,
                 runner: Callable[..., Any] = subprocess.run):
        self.http = http
        self._clock = clock
        self._run = runner
        self._cache: Dict[str, tuple] = {}

    def get(self, resource: str) -> str:
        tok, exp = self._cache.get(resource, (None, 0.0))
        if tok and self._clock() < exp - 120:
            return tok
        endpoint, header = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
        if endpoint and header:
            r = self.http.request("GET", endpoint, headers={"X-IDENTITY-HEADER": header},
                                  params={"resource": resource, "api-version": "2019-08-01"}).json()
            tok, exp = r["access_token"], float(r.get("expires_on") or self._clock() + 3000)
        else:
            out = self._run(["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
                            capture_output=True, text=True, check=True, env={**os.environ, "PYTHONWARNINGS": "ignore"})
            r = json.loads(out.stdout)
            tok = r["accessToken"]
            exp = float(r.get("expires_on") or self._clock() + 3000)
        self._cache[resource] = (tok, exp)
        return tok


class AzureOps:
    def __init__(self, subscription_id: str, resource_group: str, *, http: Optional[HttpTransport] = None,
                 tokens: Optional[TokenProvider] = None):
        self.http = http or HttpTransport()
        self.tokens = tokens or TokenProvider(self.http)
        self.sub = subscription_id
        self.rg = resource_group

    # -- ARM -------------------------------------------------------------------
    def _arm(self, method: str, url: str, *, params=None, json_body=None) -> Any:
        if not url.startswith("http"):
            url = ARM + url
        return self.http.request(method, url, params=params, json_body=json_body,
                                 headers={"Authorization": "Bearer " + self.tokens.get(ARM_RESOURCE)}).json()

    def _arm_list(self, path: str, api: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        resp = self._arm("GET", path, params={"api-version": api})
        while True:
            items.extend(resp.get("value", []))
            nxt = resp.get("nextLink")
            if not nxt:
                return items
            resp = self._arm("GET", nxt)

    def scale_set_vms(self, vmss_name: str, mgmt_nic_index: int = 0) -> List[Dict[str, Any]]:
        """VMs of a Flexible-orchestration scale set: [{name, id, vm_id, mgmt_ip, tags}].
        The management NIC is the scale set's NIC #mgmt_nic_index (0 = first NIC, i.e. no mgmt swap)."""
        rg_path = f"/subscriptions/{self.sub}/resourceGroups/{self.rg}/providers"
        vms = self._arm_list(rg_path + "/Microsoft.Compute/virtualMachines", COMPUTE_API)
        nics = {n["id"].lower(): n for n in self._arm_list(rg_path + "/Microsoft.Network/networkInterfaces", NETWORK_API)}
        out = []
        for vm in vms:
            props = vm.get("properties", {})
            vmss_id = (props.get("virtualMachineScaleSet") or {}).get("id", "")
            if not vmss_id.lower().endswith("/virtualmachinescalesets/" + vmss_name.lower()):
                continue
            refs = (props.get("networkProfile") or {}).get("networkInterfaces", [])
            ip = None
            if len(refs) > mgmt_nic_index:
                nic = nics.get(refs[mgmt_nic_index]["id"].lower(), {})
                cfgs = nic.get("properties", {}).get("ipConfigurations", [])
                ip = cfgs[0]["properties"].get("privateIPAddress") if cfgs else None
            out.append({"name": vm["name"], "id": vm["id"], "vm_id": props.get("vmId"),
                        "mgmt_ip": ip, "tags": vm.get("tags") or {}})
        return out

    def tag(self, resource_id: str, key: str, value: str) -> None:
        self._arm("PATCH", f"{resource_id}/providers/Microsoft.Resources/tags/default",
                  params={"api-version": "2021-04-01"},
                  json_body={"operation": "Merge", "properties": {"tags": {key: value}}})

    # -- Key Vault -------------------------------------------------------------
    def get_secret_json(self, vault_uri: str, name: str) -> Dict[str, Any]:
        r = self.http.request("GET", f"{vault_uri.rstrip('/')}/secrets/{name}", params={"api-version": "7.4"},
                              headers={"Authorization": "Bearer " + self.tokens.get(VAULT_RESOURCE)}).json()
        return json.loads(r["value"])


class SerialMap:
    """Blob container holding one JSON blob per VM name: {"serial", "vm_id", "mgmt_ip", "mapped_at"}."""

    def __init__(self, account: str, container: str, ops: AzureOps):
        self.base = f"https://{account}.blob.core.windows.net/{container}"
        self.ops = ops

    def _hdr(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Authorization": "Bearer " + self.ops.tokens.get(STORAGE_RESOURCE), "x-ms-version": BLOB_VERSION}
        h.update(extra or {})
        return h

    def _url(self, name: str) -> str:
        return f"{self.base}/{urllib.parse.quote(name)}"

    def put(self, name: str, record: Dict[str, Any]) -> None:
        self.ops.http.request("PUT", self._url(name), headers=self._hdr({"x-ms-blob-type": "BlockBlob"}),
                              json_body=record)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            return self.ops.http.request("GET", self._url(name), headers=self._hdr()).json()
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def delete(self, name: str) -> None:
        try:
            self.ops.http.request("DELETE", self._url(name), headers=self._hdr())
        except ApiError as exc:
            if exc.status != 404:
                raise

    def names(self) -> List[str]:
        names: List[str] = []
        marker = ""
        while True:
            params = {"restype": "container", "comp": "list"}
            if marker:
                params["marker"] = marker
            body = self.ops.http.request("GET", self.base, params=params, headers=self._hdr()).body
            root = ET.fromstring(body)
            names += [n.text for n in root.iter("Name") if n.text]
            marker = (root.findtext("NextMarker") or "").strip()
            if not marker:
                return names

    def all(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for n in self.names():
            rec = self.get(n)
            if rec:
                out[n] = rec
        return out
