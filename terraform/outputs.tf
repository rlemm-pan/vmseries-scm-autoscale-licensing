output "api_secret_arn" {
  value = local.api_secret_arn
}

output "bootstrap_secret_arn" {
  value = local.bootstrap_secret_arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "scale_in_function" {
  value = aws_lambda_function.scale_in.function_name
}

output "reconcile_function" {
  value = aws_lambda_function.reconcile.function_name
}

output "lifecycle_hook_name" {
  value = aws_autoscaling_lifecycle_hook.terminate.name
}
