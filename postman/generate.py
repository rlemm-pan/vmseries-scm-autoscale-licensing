"""Regenerate the Postman collection + environment template: python3 postman/generate.py"""
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
OK = ['pm.test("HTTP 200", () => pm.response.to.have.status(200));',
      '// TMS returns HTTP 200 with {"status":400,"error":...} on failures, so check the body too',
      'pm.test("no error envelope", () => { const b = pm.response.json(); '
      'pm.expect(b && b.error && b.status >= 400, b && b.message).to.not.be.ok; });']
T = [{"key": "token", "value": "{{csp_token}}"}]
S = [{"key": "Authorization", "value": "Bearer {{scm_token}}"}]
BASE = "https://api.paloaltonetworks.com/tms/v1"
SCMB = "https://api.strata.paloaltonetworks.com/config/setup/v1"


def basic(user_var, pass_var):
    return {"type": "basic", "basic": [{"key": "username", "value": "{{%s}}" % user_var},
                                       {"key": "password", "value": "{{%s}}" % pass_var}]}


def req(name, method, url, desc, headers=None, body=None, tests=None, auth=None, form=None):
    r = {"method": method, "header": list(headers or []), "url": url, "description": desc}
    if body is not None:
        # creditPoolId is numeric: leave the variable unquoted in the raw JSON.
        raw = json.dumps(body, indent=2).replace('"{{credit_pool_id}}"', "{{credit_pool_id}}")
        r["body"] = {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}
        r["header"].append({"key": "Content-Type", "value": "application/json"})
    if form:
        r["body"] = {"mode": "urlencoded", "urlencoded": [{"key": k, "value": v} for k, v in form.items()]}
    if auth:
        r["auth"] = auth
    item = {"name": name, "request": r}
    if tests:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return item


PROFILE = {"creditPoolId": "{{credit_pool_id}}", "name": "{{profile_name}}", "type": "VM",
           "panOS": "10.0.4_or-above", "firewallQuantity": 1, "vSysQuantity": 1, "vCpuQuantity": 4,
           "panorama": [], "subscriptions": ["ATP"]}  # panorama required (omitting it -> HTTP 500)

CSP = [
    req("01 Get CSP token", "POST", "{{csp_token_url}}",
        "OAuth2 client credentials for the fwflex-service scope. The endpoint and format come from KB article "
        "kA14u000000XhzQCAS (the activation guide omits them): client_id/client_secret go in the form body.",
        form={"client_id": "{{csp_client_id}}", "client_secret": "{{csp_client_secret}}",
              "scope": "fwflex-service", "grant_type": "client_credentials"},
        auth={"type": "noauth"},
        tests=OK + ['const j = pm.response.json();',
                    'pm.test("has access_token", () => pm.expect(j.access_token).to.be.a("string"));',
                    'pm.collectionVariables.set("csp_token", j.access_token);']),
    req("02 List credit pools", "GET", BASE + "/creditPool", "Pick the creditPoolId for the profile.", T,
        tests=OK + ['const d = pm.response.json().data || [];',
                    'pm.test("has pools", () => pm.expect(d.length).to.be.above(0));',
                    'if (!pm.variables.get("credit_pool_id") && d.length) '
                    'pm.collectionVariables.set("credit_pool_id", d[0].creditPoolId);']),
    req("03 List deployment profiles in pool", "GET", BASE + "/creditPool/{{credit_pool_id}}/deploymentProfile",
        "Existing profiles and their D- auth codes.", T, tests=OK),
    req("04 Create deployment profile (auth code)", "POST", BASE + "/deploymentProfile",
        "CREATES an auth code and allocates credits. Returns auth_code (D-xxxxxxx).", T, body=PROFILE,
        tests=OK + ['const j = pm.response.json();',
                    'pm.test("success", () => pm.expect(j.success).to.eql(true));',
                    'pm.test("auth_code returned", () => pm.expect(j.auth_code).to.match(/^D/));',
                    'pm.collectionVariables.set("auth_code", j.auth_code);']),
    req("05 Get deployment profile", "GET", BASE + "/deploymentProfile/{{auth_code}}", "", T, tests=OK),
    req("06 List serials on auth code", "GET", BASE + "/firewallserialnumbers?auth_code={{auth_code}}",
        "Firewalls currently licensed on the auth code.", T,
        tests=OK + ['const v = pm.response.json().vm_series || [];',
                    'if (v.length && !pm.variables.get("serial")) pm.collectionVariables.set("serial", v[0]);']),
    req("07 Deactivate firewall", "DELETE",
        BASE + "/firewall/deactivate?auth_code={{auth_code}}&serial_numbers={{serial}}",
        "DESTRUCTIVE: deactivates the license; credits return to the deployment profile. "
        "Comma-separate multiple serials.", T,
        tests=OK + ['const j = pm.response.json();',
                    'pm.test("no failures", () => pm.expect(j.failed || []).to.be.empty);']),
    req("08 Shrink profile (release pool credits)", "PATCH", BASE + "/deploymentProfile/{{auth_code}}",
        "Lower firewallQuantity so allocated credits return to the pool. The body must be the full profile.",
        T, body=PROFILE, tests=OK),
    req("09 Delete deployment profile", "DELETE", BASE + "/deploymentProfile/{{auth_code}}",
        "DESTRUCTIVE: deactivate all firewalls first.", T,
        tests=OK + ['pm.test("isDeleted", () => pm.expect(pm.response.json().isDeleted).to.eql(true));']),
]

SCM = [
    req("01 Get SCM token", "POST", "https://auth.apps.paloaltonetworks.com/oauth2/access_token",
        "Service account, scope tsg_id:<tsg>.",
        form={"grant_type": "client_credentials", "scope": "tsg_id:{{scm_tsg_id}}"},
        auth=basic("scm_client_id", "scm_client_secret"),
        tests=OK + ['pm.collectionVariables.set("scm_token", pm.response.json().access_token);']),
    req("02 List devices", "GET", SCMB + "/devices?limit=200&offset=0",
        "Device `id` is a UUID; the serial is expected in `name` (or `serial_number`). Confirm against the "
        "first onboarded firewall; scm.py's device_serial() follows the same order.", S,
        tests=OK + ['const r = pm.response.json(); const d = Array.isArray(r) ? r : (r.data || []);',
                    'const s = pm.variables.get("serial");',
                    'const m = d.find(x => (x.serial_number || x.serial || x.name) === s);',
                    'if (m) pm.collectionVariables.set("scm_device_id", m.id);']),
    req("03 Move device to Decommissioned folder", "PUT", SCMB + "/devices/{{scm_device_id}}",
        "Workaround for the missing public delete API.", S, body={"folder": "{{decommission_folder}}"}, tests=OK),
    req("04 List folders", "GET", SCMB + "/folders?limit=200&offset=0",
        "The NGFW root folder ('All Firewalls' in the UI) is named ngfw-shared in the API.", S, tests=OK),
    req("05 Create folder", "POST", SCMB + "/folders",
        "WRITES: creates the folder the ASG firewalls bootstrap into (dgname). Use `cli scm-setup` for "
        "idempotent creation of folders, snippets and variables.", S,
        body={"name": "{{scm_folder}}", "parent": "ngfw-shared", "description": "VM-Series in the NVIDIA AWS ASG"},
        tests=OK),
    req("06 Push candidate config to folder", "POST",
        "https://api.strata.paloaltonetworks.com/config/operations/v1/config-versions/candidate:push",
        "WRITES: pushes pending config to the folder. Returns job_id.", S,
        body={"folders": ["{{scm_folder}}"], "description": "postman push"},
        tests=OK + ['const j = pm.response.json(); if (j.job_id) pm.collectionVariables.set("scm_job_id", j.job_id);']),
    req("07 Get push job", "GET", "https://api.strata.paloaltonetworks.com/config/operations/v1/jobs/{{scm_job_id}}",
        "Poll until status_str is FIN.", S, tests=OK),
    req("08 List tenant service instances", "GET", "https://api.sase.paloaltonetworks.com/subscription/v1/instances",
        "Service instances on the TSG. Associating an SCM Pro deployment profile updates strata_cloud_manager, "
        "logging_service and ademx (see _internal.updated_at). `cli link-status` shows this next to the CSP "
        "profile status.", S, tests=OK),
]

collection = {
    "info": {
        "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "nvidia-vmseries-licensing")),
        "name": "NVIDIA VM-Series Auto-Scale Licensing (CSP + SCM)",
        "description": "Validation collection for the Software NGFW Licensing API (auth code create, deactivate, "
                       "delete) and the SCM device decommission workaround. Requests marked DESTRUCTIVE change "
                       "licensing state, so only point them at a lab credit pool.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [{"name": "CSP Licensing API", "item": CSP}, {"name": "Strata Cloud Manager", "item": SCM}],
    "variable": [{"key": k, "value": v} for k, v in {
        "csp_token_url": "https://identity.paloaltonetworks.com/as/token.oauth2",
        "csp_token": "", "profile_name": "nvidia-poc-asg", "auth_code": "", "serial": "",
        "scm_token": "", "scm_device_id": "", "scm_job_id": "", "scm_folder": "AWS-NVIDIA-GWLB",
        "decommission_folder": "Decommissioned"}.items()],
}

environment = {"name": "NVIDIA VM-Series POC", "values": [
    {"key": k, "value": "", "type": "secret" if "secret" in k else "default", "enabled": True}
    for k in ["csp_client_id", "csp_client_secret", "credit_pool_id",
              "scm_client_id", "scm_client_secret", "scm_tsg_id"]]}

with open(os.path.join(HERE, "nvidia-vmseries-licensing.postman_collection.json"), "w") as fh:
    json.dump(collection, fh, indent=2)
with open(os.path.join(HERE, "poc.postman_environment.example.json"), "w") as fh:
    json.dump(environment, fh, indent=2)
print("wrote collection + environment template")
