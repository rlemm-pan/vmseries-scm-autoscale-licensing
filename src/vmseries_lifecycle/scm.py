"""Strata Cloud Manager (SCM) client.

Covers four things:
  * devices  - list/find/move (scale-in parks a device in a "Decommissioned" folder because
               SCM has no public device-delete API), plus a per-device status summary
  * setup    - idempotent creation of folders, snippets and variables (replaces UI steps)
  * push     - push the candidate config to folders and poll the job
  * licenses - read the TSG's license list (used to confirm the deployment profile is linked)

Endpoints and field names follow the published OpenAPI specs in github.com/PaloAltoNetworks/pan.dev
(openapi-specs/scm/config/ngfw/setup/config-setup_R3.yaml, .../operations/config-operations_R3.yaml,
subscription/Licenses.yaml). The spec calls device `id` a UUID, but live NGFW records (2026-11-19)
have id = name = serial_number = the serial. The serial is read from `serial_number`/`serial`/`name`/`id`,
accepting only serial-shaped values, so either form works.
"""
from __future__ import annotations

import logging
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from .http import ApiError, ClientCredentialsToken, HttpTransport

log = logging.getLogger(__name__)

DEFAULT_TOKEN_URL = "https://auth.apps.paloaltonetworks.com/oauth2/access_token"
DEFAULT_BASE_URL = "https://api.strata.paloaltonetworks.com"
DEFAULT_SASE_BASE_URL = "https://api.sase.paloaltonetworks.com"
SETUP = "/config/setup/v1"
DEVICES_PATH = SETUP + "/devices"
OPS = "/config/operations/v1"

_SERIAL_KEYS = ("serial_number", "serial", "name", "id")
_SERIAL_RE = re.compile(r"^[0-9A-Za-z]{1,20}$")   # serial-shaped; never matches a UUID (hyphens/length)
_IP_KEYS = ("ip_address", "mgmt_ip", "ip")


def _first(d: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


def device_serial(d: Dict[str, Any]) -> Optional[str]:
    for k in _SERIAL_KEYS:
        v = d.get(k)
        if v and _SERIAL_RE.match(str(v)):
            return str(v)
    return None


def device_ip(d: Dict[str, Any]) -> Optional[str]:
    return _first(d, _IP_KEYS)


def device_connected(d: Dict[str, Any]) -> bool:
    v = d.get("is_connected", d.get("connected"))
    if isinstance(v, str):
        return v.lower() in ("true", "yes", "connected")
    return bool(v)


STATUS_FIELDS = ("name", "display_name", "hostname", "folder", "ip_address", "model", "software_version",
                 "is_connected", "connected_since", "license_match", "dev_cert_expiry_date", "vm_state")


def device_status(d: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, operator-friendly view of one SCM device."""
    out = {k: d.get(k) for k in STATUS_FIELDS if k in d}
    out["serial"] = device_serial(d)
    out["connected"] = device_connected(d)
    return out


_JOB_TERMINAL = ("FIN", "DONE", "COMPLETED", "PUSHFAIL", "FAIL", "FAILED")


def content_ready(d: Dict[str, Any]) -> bool:
    """A freshly booted firewall has no threat content; pushing tenant config that references
    content objects (e.g. URL categories) then fails validation. Seen live 2026-11-19."""
    return bool(device_connected(d)) and str(d.get("threat_version") or "0") not in ("0", "")


def _job_summary(j: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: j.get(k) for k in ("id", "type_str", "status_str", "result_str", "device_name")}
    try:
        out["errors"] = (json.loads(j.get("details") or "{}").get("errors") or [])[:6]
    except (ValueError, AttributeError):
        pass
    return out


class ScmClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tsg_id: str,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        base_url: str = DEFAULT_BASE_URL,
        sase_base_url: str = DEFAULT_SASE_BASE_URL,
        http: Optional[HttpTransport] = None,
    ):
        self.http = http or HttpTransport()
        self.base_url = base_url.rstrip("/")
        self.sase_base_url = sase_base_url.rstrip("/")
        self.tsg_id = tsg_id
        self.token = ClientCredentialsToken(
            self.http, token_url, client_id, client_secret, scope=f"tsg_id:{tsg_id}", auth_style="basic"
        )

    def _call(self, method: str, path: str, *, params=None, json_body=None, base: Optional[str] = None) -> Any:
        for attempt in (1, 2):
            try:
                return self.http.request(
                    method, (base or self.base_url) + path,
                    headers={"Authorization": f"Bearer {self.token.get()}"},
                    params=params, json_body=json_body,
                ).json()
            except ApiError as exc:
                if exc.status == 401 and attempt == 1:
                    self.token.invalidate()
                    continue
                raise

    def _list(self, path: str, params: Optional[Dict[str, Any]] = None, page_size: int = 200) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            resp = self._call("GET", path, params={**(params or {}), "limit": page_size, "offset": offset})
            page = resp if isinstance(resp, list) else resp.get("data", [])
            items.extend(page)
            total = None if isinstance(resp, list) else resp.get("total")
            offset += len(page)
            if not page or len(page) < page_size or (total is not None and offset >= int(total)):
                return items

    # -- devices ------------------------------------------------------------
    def list_devices(self, page_size: int = 200) -> List[Dict[str, Any]]:
        return self._list(DEVICES_PATH, page_size=page_size)

    def device_statuses(self, folder: Optional[str] = None) -> List[Dict[str, Any]]:
        return [device_status(d) for d in self.list_devices() if folder is None or d.get("folder") == folder]

    def find_device(self, *, serial: Optional[str] = None, ip: Optional[str] = None,
                    devices: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        for d in devices if devices is not None else self.list_devices():
            if serial and device_serial(d) == serial:
                return d
            if ip and device_ip(d) == ip:
                return d
        return None

    def move_to_folder(self, device: Dict[str, Any], folder: str) -> Dict[str, Any]:
        device_id = device.get("id")
        if not device_id:
            raise ValueError(f"device has no id: {device}")
        if device.get("folder") == folder:
            return device
        log.info("moving SCM device %s to folder %r", device_id, folder)
        return self._call("PUT", f"{DEVICES_PATH}/{device_id}", json_body={"folder": folder})

    # -- setup: folders / snippets / variables (idempotent by name) ---------
    def list_folders(self) -> List[Dict[str, Any]]:
        return self._list(SETUP + "/folders")

    def list_snippets(self) -> List[Dict[str, Any]]:
        return self._list(SETUP + "/snippets")

    def list_variables(self, folder: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._list(SETUP + "/variables", params={"folder": folder} if folder else None)

    @staticmethod
    def _by_name(items: Iterable[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
        return next((i for i in items if i.get("name") == name), None)

    def ensure_folder(self, name: str, parent: str, description: str = "", snippets: Optional[List[str]] = None,
                      dry_run: bool = False) -> Dict[str, Any]:
        existing = self._by_name(self.list_folders(), name)
        if existing:
            return {"action": "exists", "folder": name, "id": existing.get("id"), "parent": existing.get("parent")}
        body: Dict[str, Any] = {"name": name, "parent": parent}
        if description:
            body["description"] = description
        if snippets:
            body["snippets"] = list(snippets)
        if dry_run:
            return {"action": "would-create", "folder": name, "body": body}
        resp = self._call("POST", SETUP + "/folders", json_body=body)
        log.info("created SCM folder %r under %r", name, parent)
        return {"action": "created", "folder": name, "id": (resp or {}).get("id")}

    def ensure_snippet(self, name: str, description: str = "", dry_run: bool = False) -> Dict[str, Any]:
        existing = self._by_name(self.list_snippets(), name)
        if existing:
            return {"action": "exists", "snippet": name, "id": existing.get("id")}
        body: Dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if dry_run:
            return {"action": "would-create", "snippet": name, "body": body}
        resp = self._call("POST", SETUP + "/snippets", json_body=body)
        log.info("created SCM snippet %r", name)
        return {"action": "created", "snippet": name, "id": (resp or {}).get("id")}

    def ensure_variable(self, name: str, type_: str, value: Any, folder: str, description: str = "",
                        dry_run: bool = False, folder_exists: bool = True) -> Dict[str, Any]:
        """SCM variable names start with '$'. Created at folder scope; existing values are left alone.
        Listing variables in a folder that doesn't exist yet is an HTTP 400, hence folder_exists."""
        name = name if name.startswith("$") else "$" + name
        existing = self._by_name(self.list_variables(folder), name) if folder_exists else None
        if existing:
            return {"action": "exists", "variable": name, "folder": folder, "value": existing.get("value")}
        body: Dict[str, Any] = {"name": name, "type": type_, "value": value, "folder": folder}
        if description:
            body["description"] = description
        if dry_run:
            return {"action": "would-create", "variable": name, "body": body}
        self._call("POST", SETUP + "/variables", json_body=body)
        log.info("created SCM variable %s in %r", name, folder)
        return {"action": "created", "variable": name, "folder": folder}

    # -- push ---------------------------------------------------------------
    def push_candidate(self, folders: List[str], description: str = "", skip_empty: bool = True) -> Dict[str, Any]:
        """Push pending config. A folder with no devices makes SCM reject the whole push with
        "push-config -> push-to is invalid" (seen live), so by default such folders are skipped."""
        if skip_empty:
            devices = self.list_devices()
            ready = {d.get("folder") for d in devices if content_ready(d)}
            populated = {d.get("folder") for d in devices}
            skipped = {f: ("content not installed yet" if f in populated else "no devices yet")
                       for f in folders if f not in ready}
            folders = [f for f in folders if f in ready]
            if not folders:
                return {"skipped": True, "reason": skipped}
        body: Dict[str, Any] = {"folders": list(folders)}  # live API rejects "folder" (spec ambiguous)
        if description:
            body["description"] = description
        return self._call("POST", OPS + "/config-versions/candidate:push", json_body=body)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        resp = self._call("GET", f"{OPS}/jobs/{job_id}")
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        return data[0] if isinstance(data, list) and data else data

    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        resp = self._call("GET", OPS + "/jobs", params={"limit": limit})
        return resp.get("data", []) if isinstance(resp, dict) else (resp or [])

    @staticmethod
    def _job_done(job: Dict[str, Any]) -> bool:
        return str(job.get("status_str", "")).upper() in _JOB_TERMINAL or str(job.get("job_status")) == "2"

    def wait_job(self, job_id: str, timeout: int = 900, interval: int = 15) -> Dict[str, Any]:
        """Wait for a push job AND its per-device child jobs. A CommitAndPush parent can finish "OK"
        while a child (the push to the firewall) ends in PUSHFAIL, so success needs every child OK."""
        deadline = time.time() + timeout
        while True:
            job = self.get_job(job_id)
            children = [j for j in self.list_jobs() if str(j.get("parent_id")) == str(job_id)]
            if self._job_done(job) and all(self._job_done(c) for c in children):
                job["children"] = [_job_summary(c) for c in children]
                job["ok"] = str(job.get("result_str")).upper() == "OK" and \
                    all(str(c.get("result_str")).upper() == "OK" for c in children)
                return job
            if time.time() >= deadline:
                job["timed_out"] = True
                job["ok"] = False
                job["children"] = [_job_summary(c) for c in children]
                return job
            time.sleep(interval)

    # -- licenses (tenant) --------------------------------------------------
    def list_licenses(self) -> Any:
        return self._call("GET", "/subscription/v1/licenses", base=self.sase_base_url)

    def list_tenant_devices(self) -> List[Dict[str, Any]]:
        """Devices added to the TSG (hub > Device Associations), with per-app associations.
        Readable with a service account; not in the published specs (subscription service)."""
        resp = self._call("GET", "/subscription/v1/tenant_devices", base=self.sase_base_url)
        return resp.get("items", []) if isinstance(resp, dict) else (resp or [])

    def list_instances(self) -> List[Dict[str, Any]]:
        """Service instances provisioned on the TSG (strata_cloud_manager, logging_service, ademx, ...)."""
        resp = self._call("GET", "/subscription/v1/instances", base=self.sase_base_url)
        return resp if isinstance(resp, list) else (resp or {}).get("data", [])
