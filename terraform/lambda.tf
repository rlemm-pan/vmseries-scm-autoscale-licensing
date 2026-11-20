data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/vmseries_lifecycle.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "aws_cloudwatch_log_group" "scale_in" {
  name              = "/aws/lambda/${var.name_prefix}-scale-in"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "reconcile" {
  name              = "/aws/lambda/${var.name_prefix}-reconcile"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "scale_in" {
  function_name    = "${var.name_prefix}-scale-in"
  description      = "Deactivate VM-Series license + park SCM device on ASG scale-in"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "vmseries_lifecycle.handlers.scale_in.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = min(var.lifecycle_heartbeat_timeout - 30, 300)
  memory_size      = 256

  environment {
    variables = local.lambda_env
  }

  dynamic "vpc_config" {
    for_each = length(var.lambda_subnet_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = var.lambda_security_group_ids
    }
  }

  depends_on = [aws_cloudwatch_log_group.scale_in]
  tags       = var.tags
}

resource "aws_lambda_function_event_invoke_config" "scale_in" {
  function_name          = aws_lambda_function.scale_in.function_name
  maximum_retry_attempts = 2
}

resource "aws_lambda_function" "reconcile" {
  function_name    = "${var.name_prefix}-reconcile"
  description      = "Tag VM-Series instances with serials; deactivate orphaned licenses"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "vmseries_lifecycle.handlers.reconcile.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = merge(local.lambda_env, { DRY_RUN = tostring(var.reconcile_dry_run) })
  }

  dynamic "vpc_config" {
    for_each = length(var.lambda_subnet_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = var.lambda_security_group_ids
    }
  }

  depends_on = [aws_cloudwatch_log_group.reconcile]
  tags       = var.tags
}

resource "aws_lambda_function_event_invoke_config" "reconcile" {
  function_name          = aws_lambda_function.reconcile.function_name
  maximum_retry_attempts = 0
}
