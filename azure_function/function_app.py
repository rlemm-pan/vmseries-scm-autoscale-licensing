"""Azure Functions (Python v2 model) entry point for the VM-Series lifecycle.

One timer trigger: map VMs to serials, deactivate serials of deleted VMs, report orphans.
See vmseries_lifecycle/handlers/azure_lifecycle.py.
"""
import azure.functions as func

from vmseries_lifecycle.handlers.azure_lifecycle import run

app = func.FunctionApp()


@app.timer_trigger(schedule="%LIFECYCLE_SCHEDULE%", arg_name="timer", run_on_startup=False, use_monitor=False)
def lifecycle(timer: func.TimerRequest) -> None:
    run()
