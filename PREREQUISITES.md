# Prerequisites: automated VM-Series licensing and SCM onboarding in AWS and Azure scale groups

This is everything that has to be in place before scale-out and scale-in run without manual steps,
for an **AWS Auto Scaling Group (ASG)** or an **Azure Virtual Machine Scale Set (VMSS)**. Both were
verified end to end. Sections 1, 2, 4 and 5 apply to both clouds; use **section 3 for AWS** or
**section 3a for Azure**. "Scale group" below means the ASG or the VMSS.

Items marked **(UI, one time)** have no public API and are done once per deployment, not per firewall.
Everything else is automated by this repository (Terraform + Python CLI + two Lambdas on AWS, or one
timer Function on Azure).

---

## 1. Palo Alto Networks Customer Support Portal (CSP)

| # | Prerequisite | Notes |
|---|---|---|
| 1.1 | **CSP account** with a role that can manage Software NGFW credits and device certificates (for example Super User or Standard User) | The same account must own the credit pool, the deployment profile and the registration PIN. |
| 1.2 | **Software NGFW credit pool**, active and not expired | Size it for the scale group maximum: firewalls × vCPU per firewall, plus subscriptions and SCM Pro. Credits are only consumed while a firewall is licensed. |
| 1.3 | **OAuth client for the Licensing API** | CSP → Account Management → OAuth API Management → scope **`fwflex-service`** → Create OAuth Credentials. Store the client ID and secret key in AWS Secrets Manager. |
| 1.4 | **A deployment profile (auth code) dedicated to each scale group** | Created by `cli create`. Set `firewallQuantity` to the scale group maximum and vCPU to match the instance type. Don't share this auth code with firewalls outside the scale group: the reconciler treats unknown serials on it as orphans. |
| 1.5 | **Strata Cloud Manager Pro on the profile** **(UI, one time)** | The Licensing API has no subscription code for SCM Pro. Edit the profile in the CSP and select Strata Cloud Manager Pro. Without it the firewall gets SCM Essentials. |
| 1.6 | **Device-certificate registration PIN** **(UI, renewed before expiry)** | CSP → Assets → Device Certificates → Generate Registration PIN. The PIN ID and value go in the bootstrap. Firewalls can't get a device certificate, and so can't connect to SCM, without it. Choose a long expiry and plan rotation: new firewalls fail to onboard once the PIN expires. |

## 2. Strata Cloud Manager (SCM) tenant

| # | Prerequisite | Notes |
|---|---|---|
| 2.1 | **SCM tenant (TSG)** with Strata Cloud Manager provisioned | One tenant per deployment profile. |
| 2.2 | **Deployment profile associated with the tenant** **(UI, one time; critical)** | CSP → Software NGFW Credits → profile → **Finish SetUp** (or hub → Common Services → Subscriptions & Add-ons → Ready for Activation → **Activate Now**). Select the tenant, **tick the deployment profile**, agree and Activate. Wait for hub → Tenant Management → Deployment Profiles to show the association **Complete** (15–50 minutes; if it stays "Associating" for hours, open a case). Verified in the lab: once Complete, a new firewall was in the tenant the minute it licensed and in SCM 2 minutes later. The CSP profile status may stay `Updated`, so don't rely on it. **This is what makes scale-out hands-free:** only then are new firewalls added to the tenant and associated with SCM automatically. Without it, firewalls license and boot but never appear in SCM until someone adds each one by hand in Device Associations. `cli link-status <auth_code>` checks this. |
| 2.3 | **Consistent SCM tier across the tenant** | A tenant can't mix profiles on SCM Pro with profiles on Strata Logging Service only or on no subscription. Activating a Pro profile on a tenant that already has other profiles can dissociate their firewalls. Check before activating on a shared tenant. |
| 2.4 | **SCM service account** | Hub → Common Services → Identity & Access → Add Identity → Service Account. It needs a role that can edit configuration (folders, snippets, variables, devices, push) and read subscriptions. Provides a client ID, client secret and TSG ID (the TSG ID is inside the client ID: `name@<TSG>.iam.panserviceaccount.com`). Store them in AWS Secrets Manager. |
| 2.5 | **SCM folders: one per scale group, plus `Decommissioned`** | Created by `cli scm-setup`. The scale group's folder name must match `dgname` in the bootstrap. |
| 2.6 | **Firewall configuration in the folder** | Interfaces, zones, routing and security policy for the GWLB design, in the folder or attached snippets. `scm-setup` creates the containers; the content is the customer's design. |
| 2.7 | **Review existing onboarding rules** | A catch-all onboarding rule can place new firewalls in another folder. The bootstrap `dgname` takes precedence, but check the rules on shared tenants. |

## 3. AWS

| # | Prerequisite | Notes |
|---|---|---|
| 3.1 | **AWS account and region**, with permission to deploy VPC/EC2, Auto Scaling, Lambda, IAM roles, EventBridge, Secrets Manager, SNS and CloudWatch Logs | |
| 3.2 | **AWS Marketplace subscription to "VM-Series Next-Generation Firewall (BYOL)"** | One click in the console, per account. Instances fail to launch without it. |
| 3.3 | **PAN-OS version** | Tested with **11.2.16-h1**. 11.2 or later is recommended. |
| 3.4 | **Instance type and EC2 vCPU quota** | vCPU should match the profile's vCPU per firewall. The quota must cover the ASG maximum. |
| 3.5 | **Auto Scaling Group and launch template** (customer-owned) | The launch template carries the PAN-OS bootstrap: `authcodes`, `panorama-server=cloud`, `dgname=<SCM folder>`, `vm-series-auto-registration-pin-id` / `-value`, and for GWLB `mgmt-interface-swap=enable` and `plugin-op-commands=aws-gwlb-inspect:enable,advance-routing:enable`. See `terraform/examples/`. **Set the root volume to `delete_on_termination = true`:** the VM-Series AMI defaults it to false, so every scaled-in firewall would otherwise leave a 60 GB EBS volume behind and keep billing. |
| 3.6 | **Outbound internet from the firewall management interface** (NAT gateway or public IP) | Required destinations: `api.paloaltonetworks.com` (TCP 443, licensing); `certificate.paloaltonetworks.com`, `certificatetrusted.paloaltonetworks.com`, `apitrusted.paloaltonetworks.com` (device certificate); `ocsp.paloaltonetworks.com`, `crl.paloaltonetworks.com`, `ocsp.godaddy.com` (TCP 80); `*.gpcloudservice.com` (TCP 443 and 444); the SCM service (TCP 443 on 11.2 and later, otherwise 3978); content update servers. No inbound access is needed. |
| 3.7 | **Outbound internet for the two Lambdas** | `identity.paloaltonetworks.com` and `api.paloaltonetworks.com` (licensing), `auth.apps.paloaltonetworks.com`, `api.strata.paloaltonetworks.com` and `api.sase.paloaltonetworks.com` (SCM). If the Lambdas run in a VPC, they need a NAT route. |
| 3.8 | **Two Secrets Manager secrets** | API credentials (CSP OAuth client and SCM service account) and PAN-OS bootstrap values. Terraform creates the containers; the values are loaded separately so they stay out of Terraform state. |
| 3.9 | **Tooling for deployment** | Terraform 1.5 or later with AWS provider 6.x, and Python 3.9 or later for the CLI. |

## 3a. Azure (instead of section 3)

| # | Prerequisite | Notes |
|---|---|---|
| 3a.1 | **Subscription with Owner** (or Contributor + User Access Administrator) | Terraform assigns roles to the Function's managed identity. |
| 3a.2 | **Resource providers registered** | `Microsoft.Compute`, `Microsoft.Network`, `Microsoft.Web`, `Microsoft.App` (Flex VNet integration; easy to miss), `Microsoft.KeyVault`, `Microsoft.Storage`, `Microsoft.Insights`, `Microsoft.ManagedIdentity`. |
| 3a.3 | **Marketplace terms for `paloaltonetworks:vmseries-flex:byol`** | Accept once per subscription (`az vm image terms accept`). |
| 3a.4 | **vCPU quota** for the VM size family in the region | Tested with Standard_D4s_v5. Check per-zone restrictions (`az vm list-skus`). |
| 3a.5 | **VM-Series Flexible-orchestration scale set** (customer-owned) | Management NIC first, data NIC(s) after. Bootstrap keys in **custom data**, `;`-separated: `type=dhcp-client;panorama-server=cloud;dgname=<SCM folder>;authcodes=<D-code>;vm-series-auto-registration-pin-id=…;vm-series-auto-registration-pin-value=…`. |
| 3a.6 | **Outbound internet** from the management subnet (NAT gateway) | Same destinations as 3.6. |
| 3a.7 | **Function VNet-integration subnet** | Delegated to `Microsoft.App/environments`, service endpoints `Microsoft.Storage` and `Microsoft.KeyVault`, with NAT for outbound (licensing, SCM and ARM APIs). Needed because tenant policies often require storage accounts to deny public network access. |
| 3a.8 | **No dedicated App Service quota needed** | The Function runs on the Flex Consumption plan. |
| 3a.9 | **Tooling** | Terraform 1.5 or later with azurerm 4.x/5.x, Azure CLI signed in (browser login; device-code login is often blocked by Conditional Access), curl, Python 3.9 or later. |

Azure scale-in is detected, not intercepted: the lifecycle Function runs every 2 minutes (configurable),
so licenses are released up to that long after a VM is deleted.

## 4. Operational expectations

| Topic | What to expect |
|---|---|
| Timing | Measured in the labs: boot to license about 10 minutes, in SCM about 2 minutes later, threat content about 7 minutes after that, first successful config push about 20 minutes after launch (AWS and Azure were similar). Size scale-out alarms with that in mind. |
| First config push | A freshly booted firewall has no threat content yet, and config that references content objects (for example URL categories) fails to commit. `scm-push` waits for content and checks the per-firewall push result. |
| Scale-in (AWS) | The termination hook holds the instance, the Lambda deactivates the license and moves the SCM device to `Decommissioned`, then the instance is released. Scale-in never blocks termination; failures raise an SNS alert, and the reconciler retries. |
| Scale-in (Azure) | Azure has no termination hook. The VM is deleted right away; the lifecycle Function notices on its next run (every 2 minutes by default), deactivates the license and moves the SCM device. Verified: deactivated within about 30 seconds of the delete in the lab. |
| Leftover disks | AWS: set the launch template's root volume to `delete_on_termination = true` (see 3.5). Azure: Flexible scale sets delete the OS disk and NICs with the VM (verified). |
| Credits | Deactivation returns credits to the deployment profile, not the pool (confirmed). To free pool credits, lower the profile's firewall count (`cli resize`). |
| Reconciler / orphans | Starts in dry-run (AWS reconciler and Azure Function). Enable deactivation of orphaned licenses after validating in your environment. |
| Device cleanup in SCM | Automatic for firewalls added through the linked deployment profile: after scale-in deactivates the license, the device is removed from the tenant and SCM within a couple of minutes (verified). Only firewalls added by hand would need cleanup in hub → Device Associations. |
| PIN rotation | Regenerate the registration PIN before it expires and update the bootstrap (AWS launch template user data and bootstrap secret; Azure scale set custom data). |

## 5. Summary of one-time UI steps (no public API)

1. Select Strata Cloud Manager Pro on the deployment profile (CSP).
2. Associate the deployment profile with the SCM tenant, ticking the profile, and wait for **Complete** (CSP Finish SetUp / hub).
3. Generate the registration PIN (CSP), and renew it before it expires.
4. Subscribe to the VM-Series BYOL image (AWS Marketplace, or accept the Azure Marketplace terms).
5. Only if a firewall was ever added by hand: remove it from the tenant in hub Device Associations. Profile-added firewalls are cleaned up automatically.

Everything else (deployment profile creation, SCM folders and snippets, bootstrap secrets, lifecycle
hook, license deactivation on scale-in, serial tagging, orphan reconciliation, config push) is automated.
