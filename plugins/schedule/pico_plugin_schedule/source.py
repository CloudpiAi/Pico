"""What the host gives the schedule plugin for `report` and `query` tasks and the "Data through" line.

A Source has:
- `templates`: the report template names it knows; `supports_query`: whether saved queries can run on it;
- `check_report(payload) -> payload` and `check_query(sql) -> sql`: raise tasks.TaskError when the input doesn't fit;
- `read(kind, payload) -> (newest full day or None, body lines or None)`: None = nothing to send (a conditional task
  whose condition isn't met); [] for kinds without a body (`ask`); raises kinds.RenderError when it can't run.

The standalone Pico FinOps Agent uses pico_plugin_focus_data.schedule_source:FocusSource (its local FOCUS cache); a host
without cost data uses NoData (reminders and `ask` tasks only). The plugin config's `source` names the class.
"""
import importlib

from .tasks import TaskError


class NoData:
    """No cost data here: reminders and `ask` tasks only."""

    templates = ()
    supports_query = False

    def __init__(self, config=None):
        pass

    def check_report(self, payload):
        raise TaskError("bad_kind", "Cost reports aren't available here. Ask me as a question (an ask task) instead.")

    def check_query(self, sql):
        raise TaskError("bad_kind", "Saved queries aren't available here. Ask me as a question (an ask task) instead.")

    def read(self, kind, payload):
        return None, []


def load(ref, config=None):
    """The Source named by `ref` ("module:Class"), built with the plugin config; NoData when ref is empty."""
    if not ref:
        return NoData(config)
    module, _, name = ref.partition(":")
    return getattr(importlib.import_module(module), name)(config or {})
