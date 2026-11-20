"""Scale-in: ASG terminate lifecycle hook -> EventBridge -> this Lambda.

1. Resolve the firewall serial: instance tag, else SCM lookup by mgmt IP, else elimination
   (licensed serials minus serials tagged on the ASG's other instances, when exactly one is left).
2. Deactivate it on the Software NGFW auth code -> credits go back to the deployment profile.
3. Move the SCM device record to the Decommissioned folder (no public delete API).
4. CompleteLifecycleAction CONTINUE so the instance terminates.

A failure in steps 1-3 sends an SNS alert but still lets the instance terminate.
The reconciliation job is the safety net for anything left licensed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..aws import SERIAL_TAG, instance_tag, mgmt_ip
from ..config import Deps, get_deps
from ..scm import device_serial

log = logging.getLogger()
log.setLevel(logging.INFO)


def resolve_serial(deps: Deps, instance_id: str) -> Dict[str, Optional[str]]:
    instances = deps.aws.describe_instances([instance_id])
    inst = instances[0] if instances else {}
    serial = instance_tag(inst, SERIAL_TAG)
    ip = mgmt_ip(inst, deps.settings.mgmt_device_index)
    source = "tag" if serial else None
    if not serial and ip and deps.scm:
        dev = deps.scm.find_device(ip=ip)
        if dev:
            serial = device_serial(dev)
            source = "scm-ip"
    if not serial:
        serial = _serial_by_elimination(deps, instance_id)
        source = "elimination" if serial else None
    return {"serial": serial, "mgmt_ip": ip, "source": source}


def _serial_by_elimination(deps: Deps, instance_id: str) -> Optional[str]:
    """Needed when the firewall licensed but never reached SCM, so it was never tagged (seen live
    2026-11-19). Only answers when it is unambiguous: every OTHER instance in the ASG carries a serial
    tag, and exactly one licensed serial on the auth code is not claimed by those tags."""
    others = [i for i in deps.aws.asg_instance_ids(deps.settings.asg_name) if i != instance_id]
    tags = {}
    if others:
        for inst in deps.aws.describe_instances(others):
            tags[inst.get("InstanceId")] = instance_tag(inst, SERIAL_TAG)
    if any(not tags.get(i) for i in others):
        log.info("elimination skipped: other untagged instances exist (%s)", [i for i in others if not tags.get(i)])
        return None
    candidates = set(deps.csp.list_serials(deps.auth_code)) - set(tags.values())
    if len(candidates) == 1:
        return candidates.pop()
    log.info("elimination skipped: %d candidate serials", len(candidates))
    return None


def process(deps: Deps, detail: Dict[str, Any]) -> Dict[str, Any]:
    instance_id = detail["EC2InstanceId"]
    report: Dict[str, Any] = {
        "instance_id": instance_id, "auth_code": deps.auth_code,
        "serial": None, "deactivated": False, "scm_decommissioned": False, "errors": [],
    }
    try:
        found = resolve_serial(deps, instance_id)
        report.update(found)
        serial = found["serial"]
        if not serial:
            report["errors"].append("serial not found (firewall may never have licensed); reconciler will verify")
        else:
            res = deps.csp.deactivate(deps.auth_code, [serial])
            report["deactivated"] = res.ok
            report["already_inactive"] = bool(res.already_inactive)
            if not res.ok:
                report["errors"].append(f"deactivate failed: {res.failed} {res.failure_reason}")
            if deps.scm:
                try:
                    dev = deps.scm.find_device(serial=serial)
                    if dev:
                        deps.scm.move_to_folder(dev, deps.settings.decommission_folder)
                        report["scm_decommissioned"] = True
                    else:
                        report["scm_note"] = "device not present in SCM"
                except Exception as exc:  # SCM cleanup must not block credit recovery
                    log.exception("SCM decommission failed")
                    report["errors"].append(f"SCM decommission failed: {exc}")
    except Exception as exc:
        log.exception("scale-in processing failed")
        report["errors"].append(f"unhandled: {exc}")
    return report


def handler(event: Dict[str, Any], context: Any = None, deps: Optional[Deps] = None) -> Dict[str, Any]:
    detail = event.get("detail", {})
    if "EC2InstanceId" not in detail:
        log.info("ignoring event without EC2InstanceId: %s", event)
        return {"ignored": True}

    report: Dict[str, Any] = {"instance_id": detail["EC2InstanceId"], "errors": []}
    try:
        deps = deps or get_deps()
        report = process(deps, detail)
        log.info("scale-in report: %s", report)
        if report["errors"]:
            deps.aws.notify(deps.settings.sns_topic_arn,
                            f"VM-Series scale-in issue: {report['instance_id']}", report)
    finally:
        # Always release the instance; never leave it stuck in Terminating:Wait.
        if deps is not None and detail.get("LifecycleHookName"):
            try:
                deps.aws.complete_lifecycle(
                    hook=detail["LifecycleHookName"], asg=detail["AutoScalingGroupName"],
                    instance_id=detail["EC2InstanceId"], token=detail.get("LifecycleActionToken"),
                )
            except Exception:
                log.exception("CompleteLifecycleAction failed (hook will time out with its default result)")
    return report
