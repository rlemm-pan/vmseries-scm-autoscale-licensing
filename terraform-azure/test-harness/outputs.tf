output "resource_group" { value = azurerm_resource_group.this.name }
output "vmss_name" { value = azurerm_orchestrated_virtual_machine_scale_set.fw.name }
output "nat_public_ip" { value = azurerm_public_ip.nat.ip_address }
output "function_app_name" { value = module.lifecycle.function_app_name }
output "key_vault_name" { value = module.lifecycle.key_vault_name }
output "application_insights_app_id" { value = module.lifecycle.application_insights_app_id }
