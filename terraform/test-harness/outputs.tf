output "asg_name" { value = aws_autoscaling_group.fw.name }
output "ami_id" { value = local.ami_id }
output "nat_public_ip" { value = aws_eip.nat.public_ip }
output "bootstrap_secret_arn" { value = module.lifecycle.bootstrap_secret_arn }
output "api_secret_arn" { value = module.lifecycle.api_secret_arn }
output "alerts_topic_arn" { value = module.lifecycle.alerts_topic_arn }
