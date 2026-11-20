variable "resource_group_name" {
  description = "Resource group for the lifecycle resources (Function App, Key Vault, storage)"
  type        = string
}
variable "vmss_resource_group_name" {
  description = "Resource group of the VM-Series scale set (Function gets Reader + Tag Contributor on it)"
  type        = string
}
variable "vmss_name" {
  description = "Existing VM-Series Flexible-orchestration scale set"
  type        = string
}
variable "location" {
  type = string
}
variable "name_prefix" {
  description = "Short lowercase prefix (letters/digits/hyphens); also used for globally unique names"
  type        = string
  default     = "vmsfw"
}
variable "auth_code" {
  description = "Software NGFW D- auth code DEDICATED to this scale set"
  type        = string
}
variable "decommission_folder" {
  type    = string
  default = "Decommissioned"
}
variable "mgmt_nic_index" {
  description = "Index of the firewall management NIC in the scale set's NIC list (0 = first NIC)"
  type        = number
  default     = 0
}
variable "schedule" {
  description = "NCRONTAB schedule of the lifecycle pass (also the worst-case scale-in detection delay)"
  type        = string
  default     = "0 */2 * * * *"
}
variable "dry_run" {
  description = "When true, orphaned licenses are only reported. Deactivation of deleted VMs' serials always runs."
  type        = bool
  default     = true
}
variable "integration_subnet_id" {
  description = "Subnet for Function VNet integration: delegated to Microsoft.App/environments, with service endpoints Microsoft.Storage and Microsoft.KeyVault, and outbound internet (NAT) for the licensing/SCM/ARM APIs"
  type        = string
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "tags" {
  type    = map(string)
  default = {}
}

variable "deploy_code" {
  description = "Deploy the Function package with scripts/azure-deploy-function.sh (needs az CLI signed in, curl, python3 on the machine running Terraform). Set false to deploy it another way (CI/CD)."
  type        = bool
  default     = true
}
