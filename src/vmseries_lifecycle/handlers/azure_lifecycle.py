"""Azure: one timer-triggered pass that replaces the AWS lifecycle hook + reconcile Lambdas.

Azure scale sets have no termination hook that waits for automation, so deletions are DETECTED
rather than intercepted. License deactivation doesn't need the VM to be running, so acting a few
minutes after the delete is fine.

Each run:
  1. Map    - every live VM in the scale set is matched to its firewall serial (mgmt IP -> SCM
              device), tagged `paloalto-serial`, and recorded in the serial map (blob per VM name).
  2. Gone   - every map record whose VM no longer exists: deactivate the serial (credits go back to the
              deployment profile), move the SCM device to the Decommissioned folder, delete the record.
              This is the scale-in action and is NOT gated by dry-run: the mapping is authoritative.
  3. Orphan - serials licensed on the auth code that belong to no live VM and no record (for example a
              VM deleted before it ever reached SCM). Same rules as the AWS reconciler: reported in
              dry-run, deferred while any live VM is unmapped, never touched while SCM shows them connected.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .. import csp, scm
from ..azure import SERIAL_TAG, AzureOps, SerialMap
from ..config import _bool
from ..scm import device_connected, device_ip, device_serial

log = logging.getLogger()
log.setLevel(logging.INFO)


@dataclass
class AzureSettings:
    subscription_id: str
    resource_group: str
    vmss_name: str
    key_vault_uri: str
    map_account: str
    map_container: str = "serial-map"
    api_secret_name: str = "api-credentials"
    auth_code: str = ""
    decommission_folder: str = "Decommissioned"
    mgmt_nic_index: int = 0
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> "AzureSettings":
        e = os.environ
        return cls(
            subscription_id=e["SUBSCRIPTION_ID"], resource_group=e["VMSS_RESOURCE_GROUP"],
            vmss_name=e["VMSS_NAME"], key_vault_uri=e["KEY_VAULT_URI"], map_account=e["MAP_STORAGE_ACCOUNT"],
            map_container=e.get("MAP_CONTAINER", "serial-map"),
            api_secret_name=e.get("API_SECRET_NAME", "api-credentials"),
            auth_code=e.get("AUTH_CODE", ""), decommission_folder=e.get("DECOMMISSION_FOLDER", "Decommissioned"),
            mgmt_nic_index=int(e.get("MGMT_NIC_INDEX", "0")), dry_run=_bool(e.get("DRY_RUN"), True),
        )


@dataclass
class AzureDeps:
    settings: AzureSettings
    az: AzureOps
    smap: SerialMap
    csp: csp.CspLicensingClient
    scm: Optional[scm.ScmClient]
    auth_code: str

    @classmethod
    def build(cls, settings: Optional[AzureSettings] = None) -> "AzureDeps":
        s = settings or AzureSettings.from_env()
        az = AzureOps(s.subscription_id, s.resource_group)
        creds = az.get_secret_json(s.key_vault_uri, s.api_secret_name)
        csp_client = csp.CspLicensingClient(creds["csp_client_id"], creds["csp_client_secret"])
        scm_client = None
        if creds.get("scm_client_id") and creds.get("scm_tsg_id"):
            scm_client = scm.ScmClient(creds["scm_client_id"], creds["scm_client_secret"], creds["scm_tsg_id"])
        if not s.auth_code:
            raise RuntimeError("AUTH_CODE is required")
        return cls(s, az, SerialMap(s.map_account, s.map_container, az), csp_client, scm_client, s.auth_code)


def process(deps: AzureDeps) -> Dict[str, Any]:
    s = deps.settings
    report: Dict[str, Any] = {
        "auth_code": deps.auth_code, "vmss": s.vmss_name, "dry_run": s.dry_run,
        "mapped": [], "unmapped_vms": [], "gone_deactivated": [], "orphans": [], "skipped_connected": [],
        "orphans_deactivated": [], "deferred": False, "errors": [],
    }
    vms = deps.az.scale_set_vms(s.vmss_name, s.mgmt_nic_index)
    records = deps.smap.all()
    devices: List[Dict[str, Any]] = deps.scm.list_devices() if deps.scm else []
    by_ip = {device_ip(d): d for d in devices if device_ip(d)}
    by_serial = {device_serial(d): d for d in devices if device_serial(d)}
    live_names = {vm["name"] for vm in vms}

    # 1. map
    live_serials = set()
    for vm in vms:
        serial = (records.get(vm["name"]) or {}).get("serial") or vm["tags"].get(SERIAL_TAG)
        if not serial:
            dev = by_ip.get(vm["mgmt_ip"]) if vm["mgmt_ip"] else None
            serial = device_serial(dev) if dev else None
            if not serial:
                report["unmapped_vms"].append(vm["name"])
                continue
            report["mapped"].append({"vm": vm["name"], "serial": serial})
        if vm["name"] not in records:
            deps.smap.put(vm["name"], {"serial": serial, "vm_id": vm["vm_id"], "mgmt_ip": vm["mgmt_ip"],
                                       "mapped_at": int(time.time())})
        if vm["tags"].get(SERIAL_TAG) != serial:
            try:
                deps.az.tag(vm["id"], SERIAL_TAG, serial)
            except Exception as exc:  # tags are informational; the map is authoritative
                report["errors"].append(f"tag {vm['name']}: {exc}")
        live_serials.add(serial)

    # 2. gone (scale-in)
    gone = {name: rec for name, rec in records.items() if name not in live_names}
    if gone:
        serials = sorted({rec["serial"] for rec in gone.values() if rec.get("serial")})
        res = deps.csp.deactivate(deps.auth_code, serials) if serials else None
        done = set(res.success + res.already_inactive) if res else set()
        if res and not res.ok:
            report["errors"].append(f"deactivate failed: {res.failed} {res.failure_reason}")
        for name, rec in gone.items():
            serial = rec.get("serial")
            if serial not in done:
                continue  # keep the record; retried next run
            entry = {"vm": name, "serial": serial, "scm_decommissioned": False}
            dev = by_serial.get(serial)
            if dev and deps.scm:
                try:
                    deps.scm.move_to_folder(dev, s.decommission_folder)
                    entry["scm_decommissioned"] = True
                except Exception as exc:
                    report["errors"].append(f"SCM move {serial}: {exc}")
            deps.smap.delete(name)
            report["gone_deactivated"].append(entry)

    # 3. orphans
    handled = live_serials | {e["serial"] for e in report["gone_deactivated"]} | \
        {rec.get("serial") for rec in gone.values()}
    licensed = deps.csp.list_serials(deps.auth_code)
    report["licensed_count"], report["live_count"] = len(licensed), len(vms)
    for serial in licensed:
        if serial in handled:
            continue
        dev = by_serial.get(serial)
        (report["skipped_connected"] if dev and device_connected(dev) else report["orphans"]).append(serial)
    if report["orphans"] and report["unmapped_vms"]:
        report["deferred"] = True
    elif report["orphans"] and not s.dry_run:
        res = deps.csp.deactivate(deps.auth_code, report["orphans"])
        report["orphans_deactivated"] = res.success
        if not res.ok:
            report["errors"].append(f"orphan deactivate failed: {res.failed} {res.failure_reason}")
    return report


_cached: Optional[AzureDeps] = None


def run(deps: Optional[AzureDeps] = None) -> Dict[str, Any]:
    global _cached
    try:
        if deps is None:
            _cached = _cached or AzureDeps.build()
            deps = _cached
        report = process(deps)
    except Exception as exc:
        log.exception("azure lifecycle run failed")
        report = {"errors": [f"unhandled: {exc}"]}
    log.info("azure lifecycle report: %s", report)
    return report
