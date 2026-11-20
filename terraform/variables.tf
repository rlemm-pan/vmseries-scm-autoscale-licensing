variable "name_prefix" {
  description = "Prefix for all resources"
  type        = string
  default     = "nvidia-vmseries"
}

variable "asg_name" {
  description = "Existing VM-Series Auto Scaling Group name"
  type        = string
}

variable "auth_code" {
  description = "Software NGFW D- auth code DEDICATED to this ASG. Leave empty to read 'authcodes' from the bootstrap secret."
  type        = string
  default     = ""
}

variable "api_secret_arn" {
  description = "Existing secret with API creds JSON. Empty = create one."
  type        = string
  default     = ""
}

variable "bootstrap_secret_arn" {
  description = "Existing PAN-OS bootstrap secret. Empty = create one."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "Optional CMK for secrets and SNS"
  type        = string
  default     = ""
}

variable "decommission_folder" {
  description = "SCM folder that scaled-in devices are moved to (no public SCM delete API)"
  type        = string
  default     = "Decommissioned"
}

variable "mgmt_device_index" {
  description = "ENI DeviceIndex of the firewall mgmt interface (1 when mgmt-interface-swap=enable)"
  type        = number
  default     = 1
}

variable "lifecycle_heartbeat_timeout" {
  description = "Seconds an instance waits in Terminating:Wait for the Lambda"
  type        = number
  default     = 300
  validation {
    condition     = var.lifecycle_heartbeat_timeout >= 90
    error_message = "Use at least 90 seconds so the Lambda can finish."
  }
}

variable "reconcile_schedule" {
  description = "EventBridge schedule for the reconcile job"
  type        = string
  default     = "rate(10 minutes)"
}

variable "reconcile_dry_run" {
  description = "When true the reconciler only reports orphaned licenses. Set false after validating in the lab."
  type        = bool
  default     = true
}

variable "csp_token_url" {
  description = "CSP OAuth token endpoint (from KB kA14u000000XhzQCAS)"
  type        = string
  default     = "https://identity.paloaltonetworks.com/as/token.oauth2"
}

variable "csp_token_auth_style" {
  description = "How client creds are sent to the CSP token endpoint: post (per KB) | basic"
  type        = string
  default     = "post"
}

variable "lambda_subnet_ids" {
  description = "Optional private subnets (need NAT egress to api.paloaltonetworks.com / api.strata.paloaltonetworks.com)"
  type        = list(string)
  default     = []
}

variable "lambda_security_group_ids" {
  type    = list(string)
  default = []
}

variable "alert_emails" {
  type    = list(string)
  default = []
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret stays recoverable (0 = delete immediately; useful for lab teardown/re-apply)"
  type        = number
  default     = 30
}
