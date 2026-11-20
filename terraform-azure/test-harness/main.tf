# Lab-only Azure end-to-end harness: throwaway VNet + VM-Series Flexible scale set + lifecycle module.
#   firewall NICs (mgmt eth0 + data eth1) -> NAT gateway -> internet (outbound only: licensing,
#   device certificate, SCM). No public IPs on the firewalls, no inbound access.
# Bootstrap values go into custom data, so they are stored in Terraform state: lab use only.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 4.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
    random  = { source = "hashicorp/random", version = ">= 3.5" }
    time    = { source = "hashicorp/time", version = ">= 0.11" }
    tls     = { source = "hashicorp/tls", version = ">= 4.0" }
  }
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
    key_vault {
      purge_soft_delete_on_destroy = true
    }
    # The lifecycle storage account denies public network access (tenant policy), so don't let the
    # provider make data-plane calls to it from the operator's machine.
    storage {
      data_plane_available = false
    }
  }
  subscription_id = var.subscription_id
  # Flex Consumption VNet integration runs through Microsoft.App; without it the Function App create
  # fails (retried until timeout) with "Please check if 'Microsoft.App' is registered".
  resource_providers_to_register = ["Microsoft.App"]
}

locals {
  name = var.name_prefix
  tags = merge({ project = "nvidia-vmseries-autoscale-e2e" }, var.tags)
  bootstrap = {
    "type"                                  = "dhcp-client"
    "panorama-server"                       = "cloud"
    "dgname"                                = var.scm_folder
    "authcodes"                             = var.auth_code
    "vm-series-auto-registration-pin-id"    = var.pin_id
    "vm-series-auto-registration-pin-value" = var.pin_value
    "plugin-op-commands"                    = "advance-routing:enable"
  }
  custom_data = join(";", [for k, v in local.bootstrap : "${k}=${v}"])
}

resource "azurerm_resource_group" "this" {
  name     = "${local.name}-rg"
  location = var.location
  tags     = local.tags
}

# ------------------------------------------------------------------ network
resource "azurerm_virtual_network" "this" {
  name                = "${local.name}-vnet"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "mgmt" {
  name                 = "mgmt"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 0)]
}

resource "azurerm_subnet" "data" {
  name                 = "data"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 1)]
}

# Function App VNet integration (Flex Consumption): delegated subnet with service endpoints so the
# Function reaches its network-restricted storage account and Key Vault.
resource "azurerm_subnet" "func" {
  name                 = "func-integration"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 2)]
  service_endpoint {
    service = "Microsoft.Storage"
  }
  service_endpoint {
    service = "Microsoft.KeyVault"
  }
  delegation {
    name = "flex"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_public_ip" "nat" {
  name                = "${local.name}-nat-ip"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = [var.zone]
  tags                = local.tags
}

resource "azurerm_nat_gateway" "this" {
  name                = "${local.name}-nat"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  sku_name            = "Standard"
  zones               = [var.zone]
  tags                = local.tags
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  nat_gateway_id       = azurerm_nat_gateway.this.id
  public_ip_address_id = azurerm_public_ip.nat.id
}

resource "azurerm_subnet_nat_gateway_association" "mgmt" {
  subnet_id      = azurerm_subnet.mgmt.id
  nat_gateway_id = azurerm_nat_gateway.this.id
}

resource "azurerm_subnet_nat_gateway_association" "data" {
  subnet_id      = azurerm_subnet.data.id
  nat_gateway_id = azurerm_nat_gateway.this.id
}

resource "azurerm_subnet_nat_gateway_association" "func" {
  subnet_id      = azurerm_subnet.func.id
  nat_gateway_id = azurerm_nat_gateway.this.id
}

resource "azurerm_network_security_group" "fw" {
  name                = "${local.name}-fw-nsg"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = local.tags
  # Default rules: all outbound allowed, inbound only from inside the VNet.
}

resource "azurerm_subnet_network_security_group_association" "mgmt" {
  subnet_id                 = azurerm_subnet.mgmt.id
  network_security_group_id = azurerm_network_security_group.fw.id
}

resource "azurerm_subnet_network_security_group_association" "data" {
  subnet_id                 = azurerm_subnet.data.id
  network_security_group_id = azurerm_network_security_group.fw.id
}

# ------------------------------------------------------------------ VM-Series scale set
resource "tls_private_key" "admin" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "azurerm_orchestrated_virtual_machine_scale_set" "fw" {
  name                        = "${local.name}-vmss"
  resource_group_name         = azurerm_resource_group.this.name
  location                    = var.location
  sku_name                    = var.vm_size
  instances                   = var.instances
  platform_fault_domain_count = 1
  zones                       = [var.zone]
  tags                        = local.tags

  plan {
    publisher = "paloaltonetworks"
    product   = "vmseries-flex"
    name      = "byol"
  }

  source_image_reference {
    publisher = "paloaltonetworks"
    offer     = "vmseries-flex"
    sku       = "byol"
    version   = var.panos_image_version
  }

  os_profile {
    custom_data = base64encode(local.custom_data)
    linux_configuration {
      admin_username                  = "panadmin"
      disable_password_authentication = true
      admin_ssh_key {
        username   = "panadmin"
        public_key = tls_private_key.admin.public_key_openssh
      }
    }
  }

  os_disk {
    storage_account_type = "Premium_LRS"
    caching              = "ReadWrite"
  }

  # eth0 = management (no mgmt swap), eth1 = data
  network_interface {
    name    = "mgmt"
    primary = true
    ip_configuration {
      name      = "mgmt"
      primary   = true
      subnet_id = azurerm_subnet.mgmt.id
    }
  }
  network_interface {
    name                           = "data"
    accelerated_networking_enabled = true
    ip_forwarding_enabled          = true
    ip_configuration {
      name      = "data"
      primary   = true
      subnet_id = azurerm_subnet.data.id
    }
  }

  boot_diagnostics {}

  depends_on = [azurerm_subnet_nat_gateway_association.mgmt, azurerm_subnet_nat_gateway_association.data]
}

# ------------------------------------------------------------------ lifecycle module
module "lifecycle" {
  source                   = "../"
  resource_group_name      = azurerm_resource_group.this.name
  vmss_resource_group_name = azurerm_resource_group.this.name
  vmss_name                = azurerm_orchestrated_virtual_machine_scale_set.fw.name
  integration_subnet_id    = azurerm_subnet_nat_gateway_association.func.subnet_id
  location                 = var.location
  name_prefix              = var.short_prefix
  auth_code                = var.auth_code
  decommission_folder      = var.decommission_folder
  dry_run                  = var.dry_run
  tags                     = local.tags
}
