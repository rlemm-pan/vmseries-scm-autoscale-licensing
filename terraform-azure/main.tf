# Azure lifecycle automation for an EXISTING VM-Series Flexible-orchestration scale set.
# Azure has no termination hook that waits for automation, so a timer-triggered Function detects
# deleted VMs and deactivates their licenses (see src/vmseries_lifecycle/handlers/azure_lifecycle.py):
#   - Function App on the Flex Consumption plan (Python, system-assigned identity, VNet-integrated)
#   - Key Vault (RBAC) for the API credentials secret (value loaded separately, kept out of state)
#   - Storage account (network-restricted to the integration subnet): host + deployment storage and
#     the `vm name -> serial` map container
#   - Log Analytics + Application Insights for the run reports
#   - Least-privilege roles for the Function identity

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 4.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
    random  = { source = "hashicorp/random", version = ">= 3.5" }
    time    = { source = "hashicorp/time", version = ">= 0.11" }
  }
}

data "azurerm_client_config" "current" {}


resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

locals {
  # Built from the name (no data lookup) so the module can be applied together with a new resource group.
  vmss_rg_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.vmss_resource_group_name}"
  rg         = var.resource_group_name
  sfx        = random_string.suffix.result
  sa_name    = substr(replace("${var.name_prefix}${local.sfx}", "-", ""), 0, 24)
  kv_name    = substr("${var.name_prefix}-${local.sfx}", 0, 24)
  pkg_root   = "${path.module}/../src/vmseries_lifecycle"
  fn_root    = "${path.module}/../azure_function"
}

# ---------------------------------------------------------------- storage (host + serial map)
resource "azurerm_storage_account" "this" {
  name                            = local.sa_name
  resource_group_name             = local.rg
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = var.tags

  # Locked to the Function's integration subnet (service endpoint); many tenants enforce this by policy.
  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    virtual_network_subnet_ids = [var.integration_subnet_id]
  }
}

resource "azurerm_storage_container" "deploy" {
  name                  = "function-deploy"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "serial_map" {
  name                  = "serial-map"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

# ---------------------------------------------------------------- key vault
resource "azurerm_key_vault" "this" {
  name                       = local.kv_name
  resource_group_name        = local.rg
  location                   = var.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
  tags                       = var.tags
}

# Lets whoever runs Terraform load the API-credentials secret afterwards (az keyvault secret set).
resource "azurerm_role_assignment" "deployer_kv" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ---------------------------------------------------------------- logs
resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name_prefix}-logs"
  resource_group_name = local.rg
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_application_insights" "this" {
  name                = "${var.name_prefix}-ai"
  resource_group_name = local.rg
  location            = var.location
  workspace_id        = azurerm_log_analytics_workspace.this.id
  application_type    = "other"
  tags                = var.tags
}

# ---------------------------------------------------------------- function app
data "archive_file" "function" {
  type        = "zip"
  output_path = "${path.module}/build/vmseries_lifecycle_function.zip"

  dynamic "source" {
    for_each = fileset(local.pkg_root, "**/*.py")
    content {
      content  = file("${local.pkg_root}/${source.value}")
      filename = "vmseries_lifecycle/${source.value}"
    }
  }
  dynamic "source" {
    for_each = fileset(local.fn_root, "**")
    content {
      content  = file("${local.fn_root}/${source.value}")
      filename = source.value
    }
  }
}

resource "azurerm_service_plan" "this" {
  name                = "${var.name_prefix}-plan"
  resource_group_name = local.rg
  location            = var.location
  os_type             = "Linux"
  sku_name            = "FC1" # Flex Consumption: serverless, VNet integration, no dedicated-instance quota
  tags                = var.tags
}

resource "azurerm_function_app_flex_consumption" "this" {
  name                        = "${var.name_prefix}-fn-${local.sfx}"
  resource_group_name         = local.rg
  location                    = var.location
  service_plan_id             = azurerm_service_plan.this.id
  runtime_name                = "python"
  runtime_version             = "3.11"
  storage_container_type      = "blobContainer"
  storage_container_endpoint  = "${azurerm_storage_account.this.primary_blob_endpoint}${azurerm_storage_container.deploy.name}"
  storage_authentication_type = "StorageAccountConnectionString"
  storage_access_key          = azurerm_storage_account.this.primary_access_key
  virtual_network_subnet_id   = var.integration_subnet_id
  maximum_instance_count      = 40
  instance_memory_in_mb       = 2048
  https_only                  = true
  tags                        = var.tags

  identity {
    type = "SystemAssigned"
  }

  site_config {
    application_insights_connection_string = azurerm_application_insights.this.connection_string
  }

  app_settings = {
    LIFECYCLE_SCHEDULE  = var.schedule
    SUBSCRIPTION_ID     = data.azurerm_client_config.current.subscription_id
    VMSS_RESOURCE_GROUP = var.vmss_resource_group_name
    VMSS_NAME           = var.vmss_name
    KEY_VAULT_URI       = azurerm_key_vault.this.vault_uri
    API_SECRET_NAME     = "api-credentials"
    MAP_STORAGE_ACCOUNT = azurerm_storage_account.this.name
    MAP_CONTAINER       = azurerm_storage_container.serial_map.name
    AUTH_CODE           = var.auth_code
    DECOMMISSION_FOLDER = var.decommission_folder
    MGMT_NIC_INDEX      = tostring(var.mgmt_nic_index)
    DRY_RUN             = tostring(var.dry_run)
  }
}

# Code deployment runs as a separate step (one-deploy API with remote build): the provider's
# zip_deploy_file can fail on Flex Consumption with "waiting for deployment service to be ready: 404".
resource "terraform_data" "deploy_code" {
  count            = var.deploy_code ? 1 : 0
  triggers_replace = [data.archive_file.function.output_sha256, azurerm_function_app_flex_consumption.this.id]
  provisioner "local-exec" {
    command = "${path.module}/../scripts/azure-deploy-function.sh ${azurerm_function_app_flex_consumption.this.name} ${data.archive_file.function.output_path}"
  }
}

# ---------------------------------------------------------------- roles for the function identity
locals {
  fn_principal = azurerm_function_app_flex_consumption.this.identity[0].principal_id
}

resource "azurerm_role_assignment" "fn_reader" {
  scope                = local.vmss_rg_id
  role_definition_name = "Reader"
  principal_id         = local.fn_principal
}

resource "azurerm_role_assignment" "fn_tags" {
  scope                = local.vmss_rg_id
  role_definition_name = "Tag Contributor"
  principal_id         = local.fn_principal
}

resource "azurerm_role_assignment" "fn_kv" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = local.fn_principal
}

resource "azurerm_role_assignment" "fn_blob" {
  scope                = azurerm_storage_account.this.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.fn_principal
}
