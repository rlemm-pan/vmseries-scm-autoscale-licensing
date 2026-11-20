output "function_app_name" { value = azurerm_function_app_flex_consumption.this.name }
output "key_vault_name" { value = azurerm_key_vault.this.name }
output "key_vault_uri" { value = azurerm_key_vault.this.vault_uri }
output "storage_account_name" { value = azurerm_storage_account.this.name }
output "application_insights_app_id" { value = azurerm_application_insights.this.app_id }
output "function_principal_id" { value = local.fn_principal }
output "function_package_path" { value = data.archive_file.function.output_path }
