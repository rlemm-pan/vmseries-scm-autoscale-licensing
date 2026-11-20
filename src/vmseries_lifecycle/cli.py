"""Operator CLI for the one-time and teardown steps (Phase 0 and full removal).

Credentials come from env vars, or from a Secrets Manager secret via --api-secret:
  CSP_CLIENT_ID / CSP_CLIENT_SECRET            (CSP OAuth client, fwflex-service scope)
  SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_TSG_ID  (SCM service account; optional)

Examples:
  python -m vmseries_lifecycle.cli pools
  python -m vmseries_lifecycle.cli create --spec examples/profile-spec.json \
      --bootstrap-secret nvidia-vmseries/bootstrap
  python -m vmseries_lifecycle.cli serials D1234567
  python -m vmseries_lifecycle.cli deactivate D1234567 007951000123456
  python -m vmseries_lifecycle.cli teardown D1234567 --yes

Strata Cloud Manager:
  python -m vmseries_lifecycle.cli scm-check                      # auth + folders + device count
  python -m vmseries_lifecycle.cli scm-setup --spec examples/scm-setup.json --dry-run
  python -m vmseries_lifecycle.cli scm-push AWS-NVIDIA-GWLB --wait
  python -m vmseries_lifecycle.cli scm-status --auth-code D1234567 --folder AWS-NVIDIA-GWLB
  python -m vmseries_lifecycle.cli link-status D1234567           # is the profile linked to the SCM tenant?
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from . import csp, scm


def _creds(args) -> Dict[str, str]:
    if args.api_secret:
        from .aws import AwsOps
        return AwsOps().get_secret_json(args.api_secret)
    return {
        "csp_client_id": os.environ.get("CSP_CLIENT_ID", ""),
        "csp_client_secret": os.environ.get("CSP_CLIENT_SECRET", ""),
        "scm_client_id": os.environ.get("SCM_CLIENT_ID", ""),
        "scm_client_secret": os.environ.get("SCM_CLIENT_SECRET", ""),
        "scm_tsg_id": os.environ.get("SCM_TSG_ID", ""),
    }


def _csp(args) -> csp.CspLicensingClient:
    c = _creds(args)
    if not c.get("csp_client_id") or not c.get("csp_client_secret"):
        sys.exit("CSP credentials missing (CSP_CLIENT_ID/CSP_CLIENT_SECRET or --api-secret)")
    return csp.CspLicensingClient(
        c["csp_client_id"], c["csp_client_secret"],
        token_url=os.environ.get("CSP_TOKEN_URL") or csp.DEFAULT_TOKEN_URL,
        token_auth_style=os.environ.get("CSP_TOKEN_AUTH_STYLE") or "post",
    )


def _scm(args) -> scm.ScmClient:
    c = _creds(args)
    if not (c.get("scm_client_id") and c.get("scm_client_secret") and c.get("scm_tsg_id")):
        sys.exit("SCM credentials missing (SCM_CLIENT_ID/SCM_CLIENT_SECRET/SCM_TSG_ID or --api-secret)")
    return scm.ScmClient(
        c["scm_client_id"], c["scm_client_secret"], c["scm_tsg_id"],
        token_url=os.environ.get("SCM_TOKEN_URL") or scm.DEFAULT_TOKEN_URL,
        base_url=os.environ.get("SCM_BASE_URL") or scm.DEFAULT_BASE_URL,
    )


def _has_scm(args) -> bool:
    c = _creds(args)
    return bool(c.get("scm_client_id") and c.get("scm_client_secret") and c.get("scm_tsg_id"))


def _spec(path: str) -> csp.DeploymentProfileSpec:
    with open(path) as fh:
        return csp.DeploymentProfileSpec.from_dict(json.load(fh))


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=lambda o: o.__dict__))


def _confirm(args, what: str) -> None:
    if not args.yes:
        reply = input(f"{what} Type 'yes' to continue: ")
        if reply.strip().lower() != "yes":
            sys.exit("aborted")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="vmseries-lifecycle", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-secret", help="Secrets Manager id holding API credentials JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pools", help="list Software NGFW credit pools")
    sp = sub.add_parser("profiles", help="list deployment profiles in a credit pool")
    sp.add_argument("credit_pool_id")

    sp = sub.add_parser("create", help="create (or reuse by name) a deployment profile -> D- auth code")
    sp.add_argument("--spec", required=True, help="profile spec JSON (see examples/profile-spec.json)")
    sp.add_argument("--bootstrap-secret", help="write the auth code into this secret's 'authcodes' key")
    sp.add_argument("--dry-run", action="store_true", help="print the request body only")

    sp = sub.add_parser("show", help="show a deployment profile")
    sp.add_argument("auth_code")
    sp = sub.add_parser("serials", help="list firewall serials licensed on an auth code")
    sp.add_argument("auth_code")

    sp = sub.add_parser("deactivate", help="deactivate firewall serial(s); credits return to the profile")
    sp.add_argument("auth_code")
    sp.add_argument("serials", nargs="+")
    sp.add_argument("--decommission-folder", help="also move the SCM device(s) to this folder")

    sp = sub.add_parser("resize", help="change the profile's planned firewall count (frees pool credits)")
    sp.add_argument("auth_code")
    sp.add_argument("--spec", required=True)
    sp.add_argument("--quantity", type=int, required=True)

    sp = sub.add_parser("teardown", help="deactivate ALL firewalls on the auth code, then delete the profile")
    sp.add_argument("auth_code")
    sp.add_argument("--keep-profile", action="store_true")
    sp.add_argument("--decommission-folder", help="also move the SCM devices to this folder")
    sp.add_argument("--yes", action="store_true")

    sub.add_parser("scm-devices", help="list SCM devices (raw)")
    sub.add_parser("scm-check", help="verify SCM credentials: token, folders, device count")

    sp = sub.add_parser("scm-setup", help="create SCM folders/snippets/variables from a spec (idempotent)")
    sp.add_argument("--spec", required=True, help="see examples/scm-setup.json")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--push", action="store_true", help="push the spec's push_folders afterwards")

    sp = sub.add_parser("scm-push", help="push the SCM candidate config to folder(s)")
    sp.add_argument("folders", nargs="+")
    sp.add_argument("--description", default="vmseries-lifecycle push")
    sp.add_argument("--wait", action="store_true", help="poll the job until it finishes")

    sp = sub.add_parser("scm-status", help="firewall status in SCM; with --auth-code, cross-check licensed serials")
    sp.add_argument("--folder")
    sp.add_argument("--auth-code")

    sp = sub.add_parser("link-status", help="is the deployment profile linked (activated) to the SCM tenant?")
    sp.add_argument("auth_code")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "pools":
        _out(_csp(args).list_credit_pools())
    elif args.cmd == "profiles":
        _out(_csp(args).list_profiles(int(args.credit_pool_id)))
    elif args.cmd == "create":
        spec = _spec(args.spec)
        if args.dry_run:
            _out(spec.to_body())
            return 0
        auth_code, created = _csp(args).ensure_profile(spec)
        result: Dict[str, Any] = {"auth_code": auth_code, "created": created, "profile": spec.name}
        if args.bootstrap_secret:
            from .aws import AwsOps
            AwsOps().merge_secret_json(args.bootstrap_secret, {"authcodes": auth_code})
            result["bootstrap_secret"] = args.bootstrap_secret
        _out(result)
    elif args.cmd == "show":
        _out(_csp(args).get_profile(args.auth_code))
    elif args.cmd == "serials":
        _out(_csp(args).list_serials(args.auth_code))
    elif args.cmd == "deactivate":
        res = _csp(args).deactivate(args.auth_code, args.serials)
        out: Dict[str, Any] = {"deactivate": res.__dict__, "ok": res.ok}
        if args.decommission_folder:
            out["scm"] = _decommission(_scm(args), res.success + res.already_inactive, args.decommission_folder)
        _out(out)
        return 0 if res.ok else 1
    elif args.cmd == "resize":
        _out(_csp(args).resize_profile(args.auth_code, _spec(args.spec), args.quantity))
    elif args.cmd == "teardown":
        client = _csp(args)
        serials = client.list_serials(args.auth_code)
        _confirm(args, f"Deactivate {len(serials)} firewall(s) on {args.auth_code}"
                       f"{'' if args.keep_profile else ' and DELETE the deployment profile'}.")
        summary = client.teardown(args.auth_code, delete_profile=not args.keep_profile)
        if args.decommission_folder:
            summary["scm"] = _decommission(_scm(args), summary["deactivated"], args.decommission_folder)
        _out(summary)
    elif args.cmd == "scm-devices":
        _out(_scm(args).list_devices())
    elif args.cmd == "scm-check":
        client = _scm(args)
        folders = client.list_folders()
        devices = client.list_devices()
        _out({"tsg_id": client.tsg_id, "token": "ok",
              "folders": [{"name": f.get("name"), "parent": f.get("parent"), "type": f.get("type")} for f in folders],
              "devices": len(devices),
              "sample_device_keys": sorted(devices[0].keys()) if devices else []})
    elif args.cmd == "scm-setup":
        with open(args.spec) as fh:
            spec = json.load(fh)
        _out(scm_setup(_scm(args), spec, dry_run=args.dry_run, push=args.push))
    elif args.cmd == "scm-push":
        client = _scm(args)
        resp = client.push_candidate(args.folders, args.description)
        out = {"push": resp}
        job_id = (resp or {}).get("job_id") or (resp or {}).get("id")
        if args.wait and job_id:
            out["job"] = client.wait_job(str(job_id))
        _out(out)
        if args.wait and job_id and not out["job"].get("ok"):
            return 1
    elif args.cmd == "scm-status":
        client = _scm(args)
        statuses = client.device_statuses(args.folder)
        if not args.auth_code:
            _out(statuses)
        else:
            try:
                tenant = client.list_tenant_devices()
            except Exception:  # needs read access to the subscription service
                tenant = None
            _out(cross_check(_csp(args).list_serials(args.auth_code), statuses, tenant, args.auth_code))
    elif args.cmd == "link-status":
        _out(link_status(_csp(args), args.auth_code, _scm(args) if _has_scm(args) else None))
    return 0


# Deployment-profile status values seen live (2026-11-19): "Created" = new; "Transferred" = associated with
# a tenant, and its firewalls are added to the TSG automatically (tenant_devices source
# "deployment-profile", associated with strata_cloud_manager/logging_service/...). "Updated" (any edit)
# is inconclusive: the lab profile stayed "Updated" both before its association completed (firewalls NOT added)
# and after (firewalls added automatically, 2026-11-20), so the tenant device record is the real signal.
LINKED_STATUSES = ("Transferred",)
UNLINKED_STATUSES = ("Created",)
SCM_APP = "strata_cloud_manager"


def _ms(v) -> Optional[str]:
    import datetime
    if not v:
        return None
    return datetime.datetime.fromtimestamp(v / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _profile_devices(tenant_devices, auth_code: str):
    return [d for d in tenant_devices or []
            if d.get("source") == "deployment-profile"
            and (d.get("extra") or {}).get("deployment_profile_id") == auth_code]


def link_status(csp_client: csp.CspLicensingClient, auth_code: str, scm_client: Optional[scm.ScmClient]) -> Dict[str, Any]:
    """Is the profile associated with the SCM tenant, so that its firewalls join the TSG and SCM with no
    manual step? No public API answers this directly. Evidence used, strongest first:
      1. a firewall from this profile is in the TSG with source "deployment-profile" -> linked
      2. CSP profile status "Transferred" -> linked; "Created" -> not linked; anything else -> unknown
    """
    prof = csp_client.get_profile(auth_code)
    status = prof.get("status")
    linked: Any = True if status in LINKED_STATUSES else False if status in UNLINKED_STATUSES else "unknown"
    out: Dict[str, Any] = {"auth_code": auth_code, "profile": prof.get("profileName"), "csp_status": status}
    if scm_client is not None:
        out["scm_tsg_id"] = scm_client.tsg_id
        try:
            mine = _profile_devices(scm_client.list_tenant_devices(), auth_code)
            out["auto_added_devices"] = [d.get("serial_number") for d in mine]
            if mine:
                linked = True
        except Exception as exc:
            out["tenant_devices_error"] = str(exc)
        try:
            out["scm_instances"] = [{
                "app_id": i.get("app_id"), "provisioning_status": i.get("provisioning_status"),
                "updated": _ms((i.get("_internal") or {}).get("updated_at")),
            } for i in scm_client.list_instances()]
        except Exception as exc:
            out["scm_instances_error"] = str(exc)
    out["linked"] = linked
    if linked is not True:
        out["next_step"] = (
            "Finish associating the profile with the SCM tenant (TSG "
            f"{getattr(scm_client, 'tsg_id', '<tsg_id>')}): hub > Common Services > Subscriptions & Add-ons > "
            "Ready for Activation > Activate Now (or CSP > Software NGFW Credits > profile > Finish SetUp), "
            "until hub > Tenant Management > Deployment Profiles shows the association Complete and the CSP "
            "status is Transferred. Until then firewalls are licensed but NOT added to the tenant or SCM. "
            "No public API exists for this step (guide pp. 61-64).")
    return out


def cross_check(serials, statuses, tenant_devices=None, auth_code: Optional[str] = None) -> Dict[str, Any]:
    by_serial = {s["serial"]: s for s in statuses if s.get("serial")}
    licensed = set(serials)
    out: Dict[str, Any] = {
        "licensed_and_connected": sorted(s for s in licensed if by_serial.get(s, {}).get("connected")),
        "licensed_not_connected": sorted(s for s in licensed if s in by_serial and not by_serial[s]["connected"]),
        "licensed_not_in_scm": sorted(licensed - set(by_serial)),
        "in_scm_not_licensed_on_code": sorted(set(by_serial) - licensed),
    }
    if tenant_devices is not None:
        tenant = {d.get("serial_number"): d for d in tenant_devices}
        apps = {s: {a.get("app_id"): a.get("association_status") for a in tenant[s].get("associations", [])}
                for s in licensed if s in tenant}
        out["licensed_not_in_tenant"] = sorted(licensed - set(tenant))
        out["in_tenant_without_scm"] = sorted(s for s, a in apps.items() if a.get(SCM_APP) != "completed")
        out["added_manually"] = sorted(s for s in licensed if s in tenant
                                       and tenant[s].get("source") != "deployment-profile")
        out["tenant_associations"] = apps
    out["devices"] = [by_serial[s] for s in sorted(licensed & set(by_serial))]
    return out


def scm_setup(client: scm.ScmClient, spec: Dict[str, Any], *, dry_run: bool = False, push: bool = False) -> Dict[str, Any]:
    """Snippets first (folders may reference them), then folders, then folder-scoped variables."""
    parent = spec.get("parent", "ngfw-shared")
    out: Dict[str, Any] = {"snippets": [], "folders": [], "variables": []}
    for sn in spec.get("snippets", []):
        out["snippets"].append(client.ensure_snippet(sn["name"], sn.get("description", ""), dry_run=dry_run))
    for f in spec.get("folders", []):
        out["folders"].append(client.ensure_folder(f["name"], f.get("parent", parent), f.get("description", ""),
                                                   f.get("snippets"), dry_run=dry_run))
    pending = {f["folder"] for f in out["folders"] if f["action"] == "would-create"}
    for v in spec.get("variables", []):
        out["variables"].append(client.ensure_variable(v["name"], v["type"], v["value"], v["folder"],
                                                       v.get("description", ""), dry_run=dry_run,
                                                       folder_exists=v["folder"] not in pending))
    if push and spec.get("push_folders"):
        out["push"] = "skipped (dry-run)" if dry_run else client.push_candidate(
            spec["push_folders"], spec.get("push_description", "vmseries-lifecycle scm-setup"))
    return out


def _decommission(client: scm.ScmClient, serials, folder: str) -> Dict[str, str]:
    devices = client.list_devices()
    out = {}
    for s in serials:
        dev = client.find_device(serial=s, devices=devices)
        if not dev:
            out[s] = "not in SCM"
            continue
        try:
            client.move_to_folder(dev, folder)
            out[s] = f"moved to {folder}"
        except Exception as exc:
            out[s] = f"error: {exc}"
    return out


if __name__ == "__main__":
    sys.exit(main())
