variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name_prefix" {
  type    = string
  default = "nvidia-vmseries-e2e"
}
variable "vpc_cidr" {
  type    = string
  default = "10.100.0.0/16"
}
variable "instance_type" {
  description = "Small flex instance to keep EC2 and credit cost down (4 vCPU)"
  type        = string
  default     = "m5.xlarge"
}
variable "panos_version" {
  description = "AMI name prefix filter, e.g. 11.2 or 11.2.5"
  type        = string
  default     = "11.2"
}
variable "ami_id" {
  description = "Override the AMI lookup"
  type        = string
  default     = ""
}
variable "vmseries_product_code" {
  description = "AWS Marketplace product code of the VM-Series BYOL listing (subscribe in the console first)"
  type        = string
  default     = "6njl1pau431dv1qxipg63mvah"
}
variable "asg_max" {
  type    = number
  default = 2
}
variable "desired_capacity" {
  type    = number
  default = 0
}
variable "auth_code" {
  description = "D- auth code dedicated to this ASG"
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
variable "reconcile_dry_run" {
  type    = bool
  default = true
}
variable "reconcile_schedule" {
  type    = string
  default = "rate(5 minutes)"
}
variable "alert_emails" {
  type    = list(string)
  default = []
}
variable "tags" {
  type    = map(string)
  default = {}
}
