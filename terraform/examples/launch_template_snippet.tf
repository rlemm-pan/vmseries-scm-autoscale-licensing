# Reference only - NVIDIA owns its launch template. Shows how the bootstrap
# secret produced by `cli create --bootstrap-secret` feeds PAN-OS user data.
#
# Option A: Terraform renders key=value user data from the secret.
# Option B: user data only names the secret and PAN-OS reads it directly
#           (instance profile needs secretsmanager:GetSecretValue on it).

data "aws_secretsmanager_secret_version" "bootstrap" {
  secret_id = module.vmseries_lifecycle.bootstrap_secret_arn
}

locals {
  boot = jsondecode(data.aws_secretsmanager_secret_version.bootstrap.secret_string)
}

resource "aws_launch_template" "vmseries" {
  name_prefix = "nvidia-vmseries-"
  # image_id, instance_type, network_interfaces, iam_instance_profile ... (NVIDIA-owned)

  # The VM-Series AMI defaults its root volume to DeleteOnTermination=false; without this, every
  # scale-in leaves an orphaned 60 GB EBS volume that keeps billing.
  block_device_mappings {
    device_name = "/dev/xvda" # the AMI's root_device_name
    ebs {
      delete_on_termination = true
      volume_type           = "gp3"
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/userdata.tftpl", {
    use_secret_name       = false # true = option B
    bootstrap_secret_name = module.vmseries_lifecycle.bootstrap_secret_arn
    scm_folder            = local.boot["dgname"]
    authcodes             = local.boot["authcodes"]
    pin_id                = local.boot["vm-series-auto-registration-pin-id"]
    pin_value             = local.boot["vm-series-auto-registration-pin-value"]
  }))
}
