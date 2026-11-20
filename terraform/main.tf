# Lifecycle automation that attaches to an EXISTING VM-Series ASG.
# NVIDIA owns the ASG, launch template and scaling triggers; this module adds:
#   - terminate lifecycle hook -> EventBridge -> scale-in Lambda (deactivate + SCM park)
#   - scheduled reconcile Lambda (serial tagging + orphan license cleanup)
#   - Secrets Manager containers for API creds and the PAN-OS bootstrap values
#   - SNS alert topic

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 6.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  # Built from the name (no data lookup) so the module can be applied together with a new ASG.
  asg_arn = "arn:${data.aws_partition.current.partition}:autoscaling:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/${var.asg_name}"

  api_secret_arn       = var.api_secret_arn != "" ? var.api_secret_arn : aws_secretsmanager_secret.api[0].arn
  bootstrap_secret_arn = var.bootstrap_secret_arn != "" ? var.bootstrap_secret_arn : aws_secretsmanager_secret.bootstrap[0].arn

  lambda_env = {
    API_SECRET_ID        = local.api_secret_arn
    BOOTSTRAP_SECRET_ID  = local.bootstrap_secret_arn
    AUTH_CODE            = var.auth_code
    ASG_NAME             = var.asg_name
    DECOMMISSION_FOLDER  = var.decommission_folder
    MGMT_DEVICE_INDEX    = tostring(var.mgmt_device_index)
    SNS_TOPIC_ARN        = aws_sns_topic.alerts.arn
    CSP_TOKEN_URL        = var.csp_token_url
    CSP_TOKEN_AUTH_STYLE = var.csp_token_auth_style
  }
}

# ---------------------------------------------------------------- secrets
# Values are NOT managed here (keeps them out of state). Populate with:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string file://api-creds.json
#   python -m vmseries_lifecycle.cli create --spec ... --bootstrap-secret <arn>
resource "aws_secretsmanager_secret" "api" {
  count       = var.api_secret_arn == "" ? 1 : 0
  name        = "${var.name_prefix}/api-credentials"
  description = "CSP OAuth client (fwflex-service) + SCM service account for VM-Series lifecycle automation"
  kms_key_id  = var.kms_key_arn != "" ? var.kms_key_arn : null
  tags        = var.tags

  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret" "bootstrap" {
  count       = var.bootstrap_secret_arn == "" ? 1 : 0
  name        = "${var.name_prefix}/bootstrap"
  description = "PAN-OS bootstrap key/values (authcodes, registration PIN, panorama-server=cloud, dgname)"
  kms_key_id  = var.kms_key_arn != "" ? var.kms_key_arn : null
  tags        = var.tags

  recovery_window_in_days = var.secret_recovery_window_days
}

# ---------------------------------------------------------------- alerts
resource "aws_sns_topic" "alerts" {
  name              = "${var.name_prefix}-alerts"
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---------------------------------------------------------------- lifecycle hook
# If the ASG is managed elsewhere with inline initial_lifecycle_hook blocks,
# move this hook there instead to avoid drift.
resource "aws_autoscaling_lifecycle_hook" "terminate" {
  name                   = "${var.name_prefix}-deactivate-license"
  autoscaling_group_name = var.asg_name
  lifecycle_transition   = "autoscaling:EC2_INSTANCE_TERMINATING"
  default_result         = "CONTINUE"
  heartbeat_timeout      = var.lifecycle_heartbeat_timeout
}

resource "aws_cloudwatch_event_rule" "terminate" {
  name        = "${var.name_prefix}-scale-in"
  description = "VM-Series ASG terminate lifecycle action -> license deactivation"
  event_pattern = jsonencode({
    source        = ["aws.autoscaling"]
    "detail-type" = ["EC2 Instance-terminate Lifecycle Action"]
    detail = {
      AutoScalingGroupName = [var.asg_name]
      LifecycleHookName    = [aws_autoscaling_lifecycle_hook.terminate.name]
    }
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "terminate" {
  rule = aws_cloudwatch_event_rule.terminate.name
  arn  = aws_lambda_function.scale_in.arn
}

resource "aws_lambda_permission" "terminate" {
  statement_id  = "AllowEventBridgeScaleIn"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scale_in.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.terminate.arn
}

# ---------------------------------------------------------------- reconcile schedule
resource "aws_cloudwatch_event_rule" "reconcile" {
  name                = "${var.name_prefix}-reconcile"
  description         = "VM-Series serial mapping + orphan license reconciliation"
  schedule_expression = var.reconcile_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "reconcile" {
  rule = aws_cloudwatch_event_rule.reconcile.name
  arn  = aws_lambda_function.reconcile.arn
}

resource "aws_lambda_permission" "reconcile" {
  statement_id  = "AllowEventBridgeReconcile"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconcile.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconcile.arn
}
