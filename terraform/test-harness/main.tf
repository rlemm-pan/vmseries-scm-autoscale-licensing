# Lab-only end-to-end harness: a throwaway VPC + VM-Series ASG, with the lifecycle module attached.
# NOT for NVIDIA: they bring their own ASG/launch template and use ../ directly.
#
#   firewall ENIs (mgmt eth0 + data eth1) in a private subnet -> NAT gateway -> internet
#   (outbound only: licensing api.paloaltonetworks.com, device-cert service, SCM). No inbound access.
#
# Bootstrap values go into user data (option A), so they are stored in Terraform state: lab use only.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 6.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = merge({ project = "nvidia-vmseries-autoscale-e2e" }, var.tags) }
}

data "aws_availability_zones" "az" { state = "available" }

locals {
  name = var.name_prefix
  az   = data.aws_availability_zones.az.names[0]
}

# ------------------------------------------------------------------ network
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone = local.az
  tags              = { Name = "${local.name}-public" }
}

resource "aws_subnet" "fw" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone = local.az
  tags              = { Name = "${local.name}-fw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = local.name }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  tags          = { Name = local.name }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "fw" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
  tags = { Name = "${local.name}-fw" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "fw" {
  subnet_id      = aws_subnet.fw.id
  route_table_id = aws_route_table.fw.id
}

resource "aws_security_group" "fw" {
  name        = "${local.name}-fw"
  description = "VM-Series lab: outbound only"
  vpc_id      = aws_vpc.this.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "intra-VPC"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
}

# ------------------------------------------------------------------ VM-Series
data "aws_ami" "vmseries" {
  count       = var.ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["aws-marketplace"]
  filter {
    name   = "product-code"
    values = [var.vmseries_product_code]
  }
  filter {
    name   = "name"
    values = ["PA-VM-AWS-${var.panos_version}*"]
  }
}

# Root device name of the chosen AMI, so the launch template can override its block device settings.
data "aws_ami" "chosen" {
  owners = ["aws-marketplace", "self", "amazon"]
  filter {
    name   = "image-id"
    values = [local.ami_id]
  }
}

locals {
  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ami.vmseries[0].id
  bootstrap = {
    "type"                                  = "dhcp-client"
    "panorama-server"                       = "cloud"
    "dgname"                                = var.scm_folder
    "authcodes"                             = var.auth_code
    "vm-series-auto-registration-pin-id"    = var.pin_id
    "vm-series-auto-registration-pin-value" = var.pin_value
    "plugin-op-commands"                    = "advance-routing:enable"
  }
  user_data = join("\n", [for k, v in local.bootstrap : "${k}=${v}"])
}

resource "aws_launch_template" "fw" {
  name_prefix   = "${local.name}-"
  image_id      = local.ami_id
  instance_type = var.instance_type
  user_data     = base64encode(local.user_data)

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  # The VM-Series AMI sets DeleteOnTermination=false on its root volume, so every scaled-in firewall
  # would leave a 60 GB volume behind (seen in the lab). Override it.
  block_device_mappings {
    device_name = data.aws_ami.chosen.root_device_name
    ebs {
      delete_on_termination = true
      volume_type           = "gp3"
      encrypted             = true
    }
  }

  # eth0 = management (no mgmt-interface-swap), eth1 = data
  network_interfaces {
    device_index          = 0
    subnet_id             = aws_subnet.fw.id
    security_groups       = [aws_security_group.fw.id]
    delete_on_termination = true
  }
  network_interfaces {
    device_index          = 1
    subnet_id             = aws_subnet.fw.id
    security_groups       = [aws_security_group.fw.id]
    delete_on_termination = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-fw" }
  }
}

resource "aws_autoscaling_group" "fw" {
  name                = "${local.name}-asg"
  min_size            = 0
  max_size            = var.asg_max
  desired_capacity    = var.desired_capacity
  vpc_zone_identifier = [aws_subnet.fw.id]
  health_check_type   = "EC2"

  launch_template {
    id      = aws_launch_template.fw.id
    version = aws_launch_template.fw.latest_version
  }

  # The lifecycle hook comes from the module; scale with `aws autoscaling set-desired-capacity`
  # or by changing var.desired_capacity.
  depends_on = [aws_route_table_association.fw, aws_nat_gateway.this]
}

# ------------------------------------------------------------------ lifecycle module + bootstrap values
# The module creates the bootstrap secret container; the harness fills it (lab only: value in state).
resource "aws_secretsmanager_secret_version" "bootstrap" {
  secret_id     = module.lifecycle.bootstrap_secret_arn
  secret_string = jsonencode(local.bootstrap)
}

module "lifecycle" {
  source              = "../"
  name_prefix         = local.name
  asg_name            = aws_autoscaling_group.fw.name
  auth_code           = var.auth_code
  decommission_folder = var.decommission_folder
  mgmt_device_index   = 0
  reconcile_dry_run   = var.reconcile_dry_run
  reconcile_schedule  = var.reconcile_schedule
  alert_emails        = var.alert_emails
}
