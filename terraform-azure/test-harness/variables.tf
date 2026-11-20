variable "subscription_id" {
  type = string
}
variable "location" {
  type    = string
  default = "eastus2"
}
variable "zone" {
  description = "Availability zone for the scale set, NAT gateway and its IP (check size restrictions)"
  type        = string
  default     = "1"
}
variable "name_prefix" {
  type    = string
  default = "nvidia-vmseries-e2e"
}
variable "short_prefix" {
  description = "Short prefix for globally unique names (storage account, Key Vault)"
  type        = string
  default     = "vmsfwe2e"
}
variable "vnet_cidr" {
  type    = string
  default = "10.110.0.0/16"
}
variable "vm_size" {
  description = "4 vCPU flex size to keep cost and credits down"
  type        = string
  default     = "Standard_D4s_v5"
}
variable "panos_image_version" {
  description = "paloaltonetworks:vmseries-flex:byol image version (az vm image list ... --all)"
  type        = string
  default     = "11.2.15"
}
variable "instances" {
  type    = number
  default = 0
}
variable "auth_code" {
  description = "D- auth code dedicated to this scale set"
  type        = string
}
variable "pin_id" {
  type      = string
  sensitive = true
}
variable "pin_value" {
  type      = string
  sensitive = true
}
variable "scm_folder" {
  type    = string
  default = "AWS-NVIDIA-GWLB"
}
variable "decommission_folder" {
  type    = string
  default = "Decommissioned"
}
variable "dry_run" {
  type    = bool
  default = true
}
variable "tags" {
  type    = map(string)
  default = {}
}
