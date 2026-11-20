"""Scheduled reconciliation (EventBridge schedule -> this Lambda).

Step 1 - Mapping: tag each live ASG instance with ``paloalto:serial`` by matching
         its mgmt IP to an SCM device. Scale-in then gets the serial without a lookup.
Step 2 - Orphans: serials licensed on the auth code that don't belong to any live
         instance (e.g. scale-in Lambda failed, or someone terminated an instance
         by hand). Deactivated only when DRY_RUN=false.

Safety rules (the auth code MUST be dedicated to this ASG):
  - If any live instance is still unmapped, orphan deactivation is deferred: an
    unknown licensed serial could belong to a firewall that is still booting.
  - Serials whose SCM device reports connected are never deactivated.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..aws import SERIAL_TAG, instance_tag, mgmt_ip
from ..config import Deps, get_deps
from ..scm import device_connected, device_ip, device_serial

log = logging.getLogger()
log.setLevel(logging.INFO)


def process(deps: Deps) -> Dict[str, Any]:
    s = deps.settings
    report: Dict[str, Any] = {
        "auth_code": deps.auth_code, "dry_run": s.dry_run,
        "tagged": [], "unmapped_instances": [], "orphans": [],
        "skipped_connected": [], "deactivated": [], "deferred": False,
        "stale_scm_devices": [], "errors": [],
    }

    instances = deps.aws.describe_instances(deps.aws.asg_instance_ids(s.asg_name))
    devices: List[Dict[str, Any]] = deps.scm.list_devices() if deps.scm else []
    by_ip = {device_ip(d): d for d in devices if device_ip(d)}
    by_serial = {device_serial(d): d for d in devices if device_serial(d)}

    # Step 1: mapping
    live_serials = set()
    for inst in instances:
        iid = inst["InstanceId"]
        serial = instance_tag(inst, SERIAL_TAG)
        if not serial:
            dev = by_ip.get(mgmt_ip(inst, s.mgmt_device_index))
            serial = device_serial(dev) if dev else None
            if serial:
                deps.aws.tag_instance(iid, SERIAL_TAG, serial)
                report["tagged"].append({"instance_id": iid, "serial": serial})
            else:
                report["unmapped_instances"].append(iid)
        if serial:
            live_serials.add(serial)

    # Step 2: orphans
    licensed = deps.csp.list_serials(deps.auth_code)
    report["licensed_count"] = len(licensed)
    report["live_count"] = len(instances)
    for serial in licensed:
        if serial in live_serials:
            continue
        dev = by_serial.get(serial)
        if dev and device_connected(dev):
            report["skipped_connected"].append(serial)
        else:
            report["orphans"].append(serial)

    if report["orphans"] and report["unmapped_instances"]:
        report["deferred"] = True
        log.info("deferring orphan deactivation: %d live instances still unmapped", len(report["unmapped_instances"]))
    elif report["orphans"] and not s.dry_run:
        res = deps.csp.deactivate(deps.auth_code, report["orphans"])
        report["deactivated"] = res.success
        if not res.ok:
            report["errors"].append(f"deactivate failed: {res.failed} {res.failure_reason}")
        if deps.scm:
            for serial in res.success:
                dev = by_serial.get(serial)
                if dev:
                    try:
                        deps.scm.move_to_folder(dev, s.decommission_folder)
                    except Exception as exc:
                        report["errors"].append(f"SCM move {serial}: {exc}")

    # Report-only: SCM records that are disconnected, unlicensed and not yet parked.
    licensed_set = set(licensed)
    for serial, dev in by_serial.items():
        if (serial not in licensed_set and not device_connected(dev)
                and dev.get("folder") != s.decommission_folder):
            report["stale_scm_devices"].append(serial)
    return report


def handler(event: Dict[str, Any], context: Any = None, deps: Optional[Deps] = None) -> Dict[str, Any]:
    deps = deps or get_deps()
    try:
        report = process(deps)
    except Exception as exc:
        log.exception("reconciliation failed")
        report = {"errors": [f"unhandled: {exc}"]}
    log.info("reconcile report: %s", report)
    if report.get("errors") or (report.get("orphans") and report.get("dry_run")):
        deps.aws.notify(deps.settings.sns_topic_arn, "VM-Series reconciliation report", report)
    return report
