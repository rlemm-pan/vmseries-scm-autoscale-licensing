# NVIDIA VM-Series Auto-Scale Licensing Automation (POC)

This is an "art of the possible" reference for automating the VM-Series license lifecycle in an
AWS Auto Scaling Group (ASG) or an Azure Virtual Machine Scale Set (VMSS), managed by Strata Cloud
Manager (SCM). Both clouds were verified end to end; see [Azure](#azure) for the Azure specifics.

| Lifecycle | What happens | Implemented by |
|---|---|---|
| **Create** (one time) | Create a Software NGFW deployment profile, which returns a **D- auth code**, and store it in Secrets Manager for bootstrap | `cli create` → `POST /tms/v1/deploymentProfile` |
| **SCM setup** (one time) | Create the ASG folder, the `Decommissioned` folder, snippets and variables; push the folder | `cli scm-setup`, `cli scm-push` |
| **Link** (one time, UI) | Associate the deployment profile with the SCM tenant (TSG). No public API, so the tool checks it and prints the steps | `cli link-status` |
| **Scale-out** | The firewall boots with `authcodes=D…`, licenses itself, gets its device cert and onboards to the SCM folder | PAN-OS bootstrap (no code), `terraform/examples/` |
| **Map** | Tag each instance with `paloalto:serial`, matched by mgmt IP to the SCM device | `reconcile` Lambda (scheduled) |
| **Track** | Licensed serials vs. SCM devices: connected, not connected, missing from SCM, unlicensed | `cli scm-status --auth-code` |
| **Scale-in** | Lifecycle hook → deactivate the serial (credits return to the profile) → move the SCM device to `Decommissioned` → release the instance | `scale_in` Lambda |
| **Safety net** | Licensed serials with no live instance get deactivated | `reconcile` Lambda (dry-run by default) |
| **Teardown** | Deactivate every firewall on the auth code, then delete the deployment profile | `cli teardown` |

Sources: *VM-Series Activation and Onboarding* guide (Software NGFW credits pp. 57–66, Licensing API
pp. 92 and 102–117), the SCM OpenAPI specs published in
[PaloAltoNetworks/pan.dev](https://github.com/PaloAltoNetworks/pan.dev/tree/master/openapi-specs/scm),
and the automated flow design.

## Layout

```
src/vmseries_lifecycle/
  http.py        stdlib HTTPS + OAuth client-credentials (no third-party deps)
  csp.py         Software NGFW Licensing API: pools, profiles, serials, deactivate, resize, teardown
  scm.py         SCM: devices (find/move/status), folders/snippets/variables, config push + jobs, tenant licenses
  aws.py         boto3 helpers (secrets, tags, lifecycle, SNS)
  azure.py       Azure ARM/Key Vault/blob helpers (stdlib; managed identity or az CLI token)
  config.py      Lambda wiring (env vars + Secrets Manager)
  cli.py         operator CLI
  handlers/scale_in.py, handlers/reconcile.py      (AWS)
  handlers/azure_lifecycle.py                      (Azure)
terraform/       AWS: lifecycle hook, EventBridge, 2 Lambdas, IAM, secrets, SNS (attaches to an existing ASG)
terraform-azure/ Azure: Flex Consumption Function, Key Vault, locked-down storage, roles (attaches to an existing VMSS)
azure_function/  Azure Functions entry point (Python v2 model)
scripts/         azure-deploy-function.sh (Function code deployment)
postman/         request-by-request API validation collection (CSP + SCM)
tests/           offline unit tests (fake HTTP, fake AWS, fake Azure)
examples/profile-spec.json   deployment profile (CSP)
examples/scm-setup.json      SCM folders, snippets, variables
```

## Prerequisites

The full customer-facing checklist is in [PREREQUISITES.md](PREREQUISITES.md). Summary:

1. **CSP OAuth client:** CSP → Account Management → OAuth API Management → scope `fwflex-service`
   → Create OAuth Credentials. Gives a client ID (`dc-…`) and a secret key.
2. **SCM service account:** in the hub, Common Services → Identity & Access → Add Identity →
   Service Account, with a role that can edit configuration and read subscriptions. The CSV download has `client_id,client_secret`. The TSG ID is
   the number inside the client ID: `name@<TSG_ID>.iam.panserviceaccount.com`.
3. **A Software NGFW credit pool** with enough credits for the profile. Check that the pool hasn't
   expired (`cli pools` shows `expirationDate`).
4. **Manual steps in the UI** (no public API):
   - Associate the deployment profile with the SCM tenant (see [Link the profile](#link-the-profile-to-the-scm-tenant)).
   - Create the device-certificate registration PIN.

   The SCM folders no longer need the UI: `cli scm-setup` creates them.

## Runbook

```bash
cd vmseries-autoscale
python3 -m unittest discover -s tests            # offline tests

export PYTHONPATH=src
export CSP_CLIENT_ID=... CSP_CLIENT_SECRET=...
export SCM_CLIENT_ID=... SCM_CLIENT_SECRET=... SCM_TSG_ID=...
```

### 1. Deployment profile (CSP)

```bash
python3 -m vmseries_lifecycle.cli pools                       # pick a non-expired creditPoolId
# edit examples/profile-spec.json: credit_pool_id, firewall_quantity = ASG max, subscriptions
python3 -m vmseries_lifecycle.cli create --spec examples/profile-spec.json --dry-run
python3 -m vmseries_lifecycle.cli create --spec examples/profile-spec.json        # prints the auth code
python3 -m vmseries_lifecycle.cli show <auth_code>             # status "Created"
```

`create` is idempotent by profile name: running it again returns the existing auth code. Add
`--bootstrap-secret <arn>` to also write the code into the bootstrap secret's `authcodes` key. That
secret is created by Terraform (step 4), so on a first run create the profile, apply Terraform, then
run `create` again with `--bootstrap-secret`.

### 2. SCM setup

```bash
python3 -m vmseries_lifecycle.cli scm-check                    # token, folders, device count
python3 -m vmseries_lifecycle.cli scm-setup --spec examples/scm-setup.json --dry-run
python3 -m vmseries_lifecycle.cli scm-setup --spec examples/scm-setup.json --push
python3 -m vmseries_lifecycle.cli scm-push AWS-NVIDIA-GWLB --wait
```

`scm-setup` creates snippets, then folders (under `ngfw-shared`, which is "All Firewalls" in the UI),
then folder-scoped variables. Objects that already exist are reported and left unchanged. A push
targets firewalls, so SCM rejects a push to a folder with no devices ("push-to is invalid"); `scm-push`
skips empty folders and reports `skipped`. Push again once the first firewall has onboarded. It creates
empty snippets only; build the rules, zones and routing in SCM or with the Terraform `PaloAltoNetworks/scm`
provider. The folder name must match `dgname` in the bootstrap secret.

### 3. Link the profile to the SCM tenant

```bash
python3 -m vmseries_lifecycle.cli link-status <auth_code>
```

There is no public API for this step. In the hub: **Common Services → Subscriptions & Add-ons →
Ready for Activation → Activate Now** (or **Finish SetUp** next to the profile in the CSP), select the
tenant, **tick the deployment profile**, and click **Activate**. Provisioning takes 15–50 minutes
(guide pp. 61–64); wait for hub → Tenant Management → Deployment Profiles to show **Complete**.
**This step is what makes scale-out turnkey.** When the association is complete, every firewall licensed
on the auth code is added to the TSG automatically
(`source: deployment-profile`), already associated with Strata Cloud Manager, Strata Logging Service and
ADEM. When it isn't complete, firewalls still license and boot, but they **never reach SCM** until someone
adds and associates each one by hand in hub → Device Associations. That happened in the first lab run
(see below).

`link-status` reports `linked: true` only on strong evidence: a firewall from this profile in the TSG
with `source: deployment-profile`, or CSP status `Transferred`. **The CSP status isn't a reliable signal**:
the lab profile stayed `Updated` after its association was Complete and its firewalls were being added
automatically, so with no firewall running yet, `link-status` reports `unknown`. Check the hub page. It reads `GET /subscription/v1/tenant_devices` and `/subscription/v1/instances`; both
work with the SCM service account but aren't in the published specs.

A profile can be linked to only one TSG at a time. All profiles on one TSG must use the same SCM tier
(SCM Pro or Strata Logging Service, not a mix).

### 4. Terraform (AWS)

```bash
cd terraform && cp terraform.tfvars.example terraform.tfvars   # set asg_name
terraform init && terraform apply
terraform output bootstrap_secret_arn api_secret_arn
aws secretsmanager put-secret-value --secret-id <api_secret_arn> --secret-string file://api-creds.json
```

Terraform creates two secrets, `<name_prefix>/bootstrap` and `<name_prefix>/api-credentials`
(`name_prefix` defaults to `nvidia-vmseries`), unless you pass existing ARNs.

The API-credentials secret JSON, used by the Lambdas or by `--api-secret`:

```json
{"csp_client_id": "...", "csp_client_secret": "...",
 "scm_client_id": "...", "scm_client_secret": "...", "scm_tsg_id": "..."}
```

The bootstrap secret holds the PAN-OS key/values: `authcodes` (written by `cli create`),
`panorama-server=cloud`, `dgname=<SCM folder>`, `vm-series-auto-registration-pin-id`/`-value`, and so on.

### Lab end-to-end harness (optional)

`terraform/test-harness/` builds a throwaway VPC (private firewall subnet + NAT gateway, no inbound
access), a VM-Series launch template with the bootstrap in user data, an ASG (min 0, max 2, desired 0)
and attaches the lifecycle module to it. **Lab only:** the PIN and auth code end up in Terraform state.

Prerequisites: AWS credentials in the shell, the **VM-Series BYOL** AMI subscribed in AWS Marketplace,
and a registration PIN.

```bash
cd terraform/test-harness && cp terraform.tfvars.example terraform.tfvars   # auth_code, pin_id, pin_value
terraform init && terraform apply
aws secretsmanager put-secret-value --secret-id "$(terraform output -raw api_secret_arn)" \
    --secret-string file://../../api-creds.json
aws autoscaling set-desired-capacity --auto-scaling-group-name "$(terraform output -raw asg_name)" --desired-capacity 1
# wait ~15-20 min for boot, licensing and SCM onboarding, then:
python3 -m vmseries_lifecycle.cli serials <auth_code>
python3 -m vmseries_lifecycle.cli scm-status --auth-code <auth_code>
aws autoscaling set-desired-capacity --auto-scaling-group-name "$(terraform output -raw asg_name)" --desired-capacity 0
terraform destroy
```

### 5. Operate

```bash
python3 -m vmseries_lifecycle.cli serials <auth_code>
python3 -m vmseries_lifecycle.cli scm-status --auth-code <auth_code> --folder AWS-NVIDIA-GWLB
python3 -m vmseries_lifecycle.cli deactivate <auth_code> <serial> --decommission-folder Decommissioned
python3 -m vmseries_lifecycle.cli resize <auth_code> --spec examples/profile-spec.json --quantity 2
python3 -m vmseries_lifecycle.cli teardown <auth_code>        # asks for confirmation
```

**Teardown order:** `cli teardown <auth_code>` first, then `terraform destroy`, then delete the ASG.
If you destroy the ASG first, the firewalls stay licensed. The reconciler would catch them, but
only while it is still deployed.

### Postman validation

```bash
cp postman/poc.postman_environment.example.json postman/poc.env.json   # fill creds; don't commit
postman collection run postman/nvidia-vmseries-licensing.postman_collection.json \
  -e postman/poc.env.json -i "CSP Licensing API"
```

CSP requests 04 and 07–09 **change licensing state**; run them only against a lab pool. SCM requests
03, 05 and 06 **write to the tenant**. Regenerate the collection with `python3 postman/generate.py`.

## Azure

The licensing and SCM half (`csp.py`, `scm.py`, the CLI, profile association, `dgname` folders) is
identical on Azure. Only the cloud automation differs:

| AWS | Azure |
|---|---|
| ASG + launch template user data | **Flexible-orchestration** VMSS, same bootstrap keys in **custom data**, `;`-separated |
| Termination lifecycle hook → EventBridge → `scale_in` Lambda | **No hook exists**: a timer Function (`azure_lifecycle`, every 2 min) detects deleted VMs from its serial map and deactivates them. Deactivation doesn't need the VM, so acting after the delete is fine. |
| `reconcile` Lambda (EventBridge schedule) | Same timer Function: maps VM → serial (mgmt IP → SCM), tags `paloalto-serial`, reports orphans |
| Secrets Manager | Key Vault (RBAC) |
| EC2 tags | VM tags, plus a blob `serial-map` (a deleted VM's tags are gone with it) |

```bash
cd terraform-azure/test-harness && cp terraform.tfvars.example terraform.tfvars   # lab only
az login                                   # browser flow; device-code login may be blocked by Conditional Access
terraform init && terraform apply
# load the API-credentials JSON into Key Vault secret "api-credentials" (same JSON as the AWS secret)
az vmss scale -g <rg> -n <vmss> --new-capacity 1
```

Lessons from the Azure lab (all handled in `terraform-azure/`):
- **Flex Consumption plan (FC1)** for the Function: the lab subscription had **0 App Service B1 quota**, and FC1
  needs no dedicated-instance quota.
- **Storage must be network-restricted** (tenant policy *"Storage accounts should restrict network access"*):
  the storage account denies public access and is reachable only from the Function's VNet-integration subnet
  (service endpoints). The provider's storage data-plane calls are disabled for this reason.
- **`Microsoft.App` must be registered**: Flex VNet integration runs through it. Without it the Function App
  create is retried until timeout. The harness registers it.
- **Code deployment is a separate step** (`scripts/azure-deploy-function.sh`, run by Terraform): the provider's
  `zip_deploy_file` failed on Flex with "waiting for deployment service to be ready: 404".
- **Flexible VMSS deletes the OS disk and NICs with the VM**, so nothing was left behind (unlike the AWS AMI's root volume).

## Design decisions

- **One dedicated auth code per ASG.** Every instance in the ASG bootstraps with the same D- code,
  and the profile's `firewallQuantity` is set to the ASG max. The reconciler treats any serial on the
  code that has no matching instance as an orphan, so **don't share this code with NVIDIA's existing
  hand-built VM-700s**.
- **Deactivation returns credits to the deployment profile, not to the pool** (guide p.92; confirmed
  in the lab: after scale-in the profile showed 0 credits consumed and 0 firewalls deployed, while
  credits allocated stayed the same). To free pool credits, lower `firewallQuantity` with `cli resize`
  (PATCH). This is a manual or scheduled decision, not a per-scale-in action.
- **Scale-in never blocks termination.** On any error the Lambda sends an SNS alert and still calls
  `CompleteLifecycleAction CONTINUE`. The reconciler retries the deactivation later.
- **SCM delete:** there is no public API, so the device is moved to the `Decommissioned` folder
  For firewalls added through a linked deployment profile, deactivating the license
  removes the device from the tenant and SCM automatically about 2 minutes later (verified in run 3), so
  manual cleanup is only needed for firewalls added by hand. SCM failures never block license deactivation.
- **SCM tier: Pro.** A profile with no SCM Pro, Strata Logging Service, IoT or SaaS Inline subscription
  gets **SCM Essentials** automatically (guide p.60). The Licensing API subscription list (guide p.112)
  has no code for SCM Pro, so add it in the CSP UI: **Products → Software NGFW Credits → Details →**
  the profile **→ ⋮ → Edit → Strata Cloud Manager Pro**, then **Finish SetUp** to associate the profile
  with the tenant. All profiles on one TSG must use the same tier (guide pp. 63–66).
  **Caution:** `cli resize` sends the full profile from the spec file, which has no Pro code. After Pro
  is added in the UI, change the firewall count in the UI too, or confirm in the lab that a PATCH keeps Pro.
- **Manual fallback for orphans:** CSP → **Software NGFW Devices** → Search By **FW Not Checked-in for (Days)**
  → ⋮ → **Deactivate Firewall** ([Delicense Ungracefully Terminated Firewalls](https://docs.paloaltonetworks.com/vm-series/activation-and-onboarding/software-ngfw/delicense-ungracefully-terminated-firewalls)).
  That page has no API equivalent; the reconciler is the automated version. That page says credits return
  "to your credit pool", while p.92 says "to the deployment profile". Check which is right in the lab.
- **Reconciler guards:** it starts in dry-run. It also defers all orphan deactivation while any live
  instance is still unmapped, since that firewall may still be booting, and it never touches a serial
  whose SCM device reports connected.

## API behavior confirmed live, and remaining gaps

Confirmed against a real CSP account and SCM tenant on 2026-11-19 unless noted.

1. **CSP token endpoint — confirmed.** The guide omits it; [KB kA14u000000XhzQCAS](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA14u000000XhzQCAS&lang=en_US)
   has it: `POST https://identity.paloaltonetworks.com/as/token.oauth2`, form-encoded `client_id`,
   `client_secret`, `scope=fwflex-service`, `grant_type=client_credentials` (`CSP_TOKEN_AUTH_STYLE=post`).
   The legacy single-key Licensing API key is **not** accepted by `/tms/v1`.
2. **`panorama` is required on create — confirmed.** Leaving it out of `POST /tms/v1/deploymentProfile`
   returns HTTP 500 with error code `ENTFWFLEXUSDIUDIC`. Sending `"panorama": []` works. The guide's
   parameter table doesn't say this, but its sample includes the field. The code always sends it.
3. **`panOS` key and value — confirmed.** The parameter table says `panOs` / `10.0.4_or_above`; the samples
   say `panOS` / `10.0.4_or-above`. The sample form works.
4. **Profile status and the tenant link.** A new profile is `Created`; some linked
   profiles show `Transferred`. After adding SCM Pro in the CSP UI and associating the profile with the
   tenant, the status stayed `Updated`, even after the association was Complete and working, so the CSP
   status can't be used to detect the link. The tenant's
   `/subscription/v1/instances` showed the SCM, logging, ADEM and Prisma Access instances updated at
   the moment of association. `/subscription/v1/licenses` returned an empty list on the test tenant.
5. **Errors can come back as HTTP 200.** A bad token returns HTTP 200 with
   `{"status":400,"error":"Bad Request","message":"Invalid Token"}` (confirmed 2026-11-17). The client
   checks the body for this envelope.
6. **SCM auth and endpoints — confirmed.** Token from `https://auth.apps.paloaltonetworks.com/oauth2/access_token`
   (basic auth, `scope=tsg_id:<TSG>`); folders, snippets and variables under `/config/setup/v1`; push via
   `POST /config/operations/v1/config-versions/candidate:push`; licenses via
   `GET https://api.sase.paloaltonetworks.com/subscription/v1/licenses`. The NGFW root folder is `ngfw-shared`.
   Listing variables in a folder that doesn't exist returns HTTP 400. The push body field is `folders`
   (`folder` is rejected), even though the spec's property list says `folder`.
7. **SCM device record — confirmed.** The spec says device `id` is a UUID, but a live VM-Series record
   has `id` = `name` = `serial_number` = the serial. `ip_address` is the management IP (used to map
   instances), and `is_connected`, `dev_cert_detail`, `software_version` and the license list are
   populated. The record also has `deactivate_wait_hrs`, `to_be_deactivated_at` and `deactivated_by`,
   which may relate to the missing device-delete API.
8. **Delete-profile URL.** The guide lists both `/v1/deployment-profile/auth-code/{code}` and
   `/tms/v1/deploymentProfile/{code}`. The code uses the second, which matches the curl sample.
9. **The guide's sample JSON bodies have trailing commas,** which makes them invalid JSON.
10. **SCM device-delete API:** none is public. If one becomes available, add it in `ScmClient`
    and call it after `move_to_folder`.
11. **Model choice.** NVIDIA runs VM-700s today. The example spec uses **16 vCPU flex**, about VM-700
    capacity. For a fixed VM-700 profile, set `"vm_model": 700` and drop `vcpu_quantity`.

## End-to-end lab test (2026-11-19, AWS us-east-2, `terraform/test-harness`)

One VM-Series (PAN-OS 11.2.16-h1, m5.xlarge) through the full lifecycle:

| Step | Result | Time (UTC) |
|---|---|---|
| ASG 0 → 1, instance launched | `InService` | 13:36 |
| Bootstrap read, license installed with the auth code | serial licensed on the profile (1 FW, 4 vCPU) | 13:46 |
| Device certificate (registration PIN), SCM connection | connected, placed in the `dgname` folder, SCM Pro licenses present | 14:26 |
| `reconcile` Lambda | instance tagged `paloalto:serial` (matched by mgmt IP) | 14:27 |
| `scm-push` | CommitAndPush job `OK` in 38 s | 14:28 |
| ASG 1 → 0, `scale_in` Lambda | serial deactivated, SCM device moved to `Decommissioned`, hook released | 14:29 |
| Instance terminated | profile back to 0 deployed, 0 credits consumed | 14:33 |

Notes:
- **SCM onboarding took about 40 minutes after licensing in this run**, but only because of the manual
  step below. With the profile association complete (run 3), the firewall was in SCM **2 minutes** after
  licensing.
- **The firewall needed a manual step to reach SCM, and that's now explained.** The profile's tenant
  association hadn't finished (CSP status `Updated`, not `Transferred`), so the licensing service never
  added the firewall to the TSG. It was added and associated by hand in hub → Device Associations
  (`source: das-ui`), which registered it as a plain `ngfw` with only `prisma_access`. Firewalls from
  this tenant's fully linked profiles were added automatically with all apps. Fix: complete the
  association (step 3); `link-status` and `scm-status --auth-code` (`added_manually`,
  `licensed_not_in_tenant`, `in_tenant_without_scm`) now detect this.
- **The first config push failed on the firewall**, even though the parent job reported `OK`. The child
  job ended in `PUSHFAIL`: tenant config referenced a URL category that a freshly booted firewall
  doesn't know yet, because it has no threat content. `scm-push --wait` now follows child jobs, exits
  non-zero on failure, and skips firewalls whose `threat_version` is still `0`.
- **Each scaled-in firewall left its 60 GB root volume behind.** The VM-Series AMI sets
  `DeleteOnTermination=false`, and `terraform destroy` doesn't own those volumes. The harness launch
  template now overrides it, and `PREREQUISITES.md` tells customers to do the same.
- **Scale-in couldn't find the serial of a firewall that never reached SCM** (second run): the instance
  was never tagged, because tagging matches the mgmt IP in SCM, so the license stayed active. The reconciler
  flagged it as an orphan as designed. `scale_in` now falls back to elimination: licensed serials minus
  serials tagged on the ASG's other instances, used only when exactly one is left and no other instance
  is untagged.
- **Device Associations has no public write API.** This repo only reads the tenant's device list
  (`GET /subscription/v1/tenant_devices`) to verify onboarding; the supported path is the profile association.
- **After scale-in, this hand-added device stayed in the `Decommissioned` folder.** Firewalls added
  through a linked profile are removed from the tenant and SCM automatically after deactivation (run 3),
  so only hand-added devices need manual cleanup.

### Run 3: turnkey (2026-11-20, profile association Complete, no manual steps)

| Step | Result | Time (UTC) |
|---|---|---|
| Apply harness, ASG 0 → 1 | instance launched | 12:39 |
| License installed | serial licensed on the profile | 12:49 |
| **Added to the tenant automatically** | `source: deployment-profile`, `deployment_profile_id` = the auth code; identity engine and Prisma Access `completed`, SCM and logging `associating` | 12:49 |
| Connected in SCM | in the `dgname` folder | 12:51 |
| Threat content installed | `threat_version` set | 12:58 |
| Reconcile | instance tagged; `link-status` → `linked: true`; cross-check clean, `added_manually` empty | 13:00 |
| `scm-push --wait` | parent CommitAndPush `OK` **and** per-firewall CommitAll `OK` | 13:01 |
| ASG 1 → 0, `scale_in` Lambda | serial (from tag) deactivated, device moved to `Decommissioned`, hook released | 13:04 |
| **Device removed from SCM automatically** | gone from `/config/setup/v1/devices`; tenant record `dissociating` | 13:06 |
| Instance terminated, `terraform destroy` | 0 serials, 0 credits consumed, 0 SCM devices, **0 leftover EBS volumes** | 13:09 |

Boot to SCM-managed took **12 minutes**, and boot to config-pushed about 20, with nobody touching the
hub or SCM. For profile-added firewalls, license deactivation also triggers automatic removal from the
tenant and SCM about 2 minutes later, so `Decommissioned` is only a short-lived parking spot.

## End-to-end lab test: Azure (2026-11-20, eastus2, `terraform-azure/test-harness`)

| Step | Result | Time (UTC) |
|---|---|---|
| VMSS created (PAN-OS 11.2.15, Standard_D4s_v5, custom-data bootstrap) | VM running | 16:46 |
| License, tenant auto-add, SCM | licensed, `source: deployment-profile`, connected in the `dgname` folder | by 16:55 |
| Lifecycle Function (after the Flex/policy fixes above) | VM tagged `paloalto-serial`, serial-map record written | 17:56 |
| `scm-push --wait` | parent CommitAndPush `OK` and per-firewall CommitAll `OK` | 17:58 |
| VMSS 1 → 0 | VM deleted; the Function's next run deactivated the serial and moved the device to `Decommissioned` | 17:59 → 18:00 |
| Leftovers | no disks or NICs remained | 18:00 |

## Status

- **CSP Licensing API: working live.** Token, pools, profiles, create, serials and deactivate (through
  the scale-in Lambda) all tested. Resize and teardown are unit-tested only.
- **SCM: working live** for token, folders, snippets, variables, devices, push and job polling, device
  move, tenant instances and licenses.
- **AWS: working live.** Termination hook, EventBridge, both Lambdas, Secrets Manager and serial
  tagging (see the end-to-end table above).
- **Deployment profile → tenant link:** done manually with SCM Pro on 2026-11-19 (credits allocated went
  from 740.10 to 853.38). `link-status` shows the supporting evidence; there's no API to confirm it outright.
- **Azure: working live.** Flexible VMSS bootstrap, Flex Consumption Function (map, tag, detect-and-deactivate), Key Vault, serial map.
- Python library, CLI, Lambdas and the Azure handler: 45 offline unit tests pass (Python 3.9 and 3.15; Lambda targets 3.12, the Azure Function 3.11).
- Terraform: the module and `test-harness/` were applied to a lab AWS account (Terraform 1.17,
  AWS provider 6.x).
