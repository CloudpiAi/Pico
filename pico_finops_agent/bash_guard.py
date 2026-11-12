"""No variable or command expansion in the agent's allow-listed bash commands (secrets would be expanded by
`bash -c`). The guard is the schedule plugin's
(pico_plugin_schedule.hooks.bash_guard: `$` / backtick outside single quotes refused). Without that plugin there is
nothing to single-quote for, so any `$` or backtick refuses an allow-listed command.
"""
try:
    from pico_plugin_schedule.hooks.bash_guard import expands_outside_single_quotes, install
except ImportError:
    _MARK = "pico_finops_agent_bash_guard"

    def expands_outside_single_quotes(command):
        return "$" in command or "`" in command

    def install():
        from pico.tools.func_tool.bash_tool import BashTool

        original = BashTool._is_command_allowed
        if getattr(original, _MARK, False):
            return

        def guarded(self, command):
            restricted = bool(self.allowed_patterns) and "*" not in self.allowed_patterns
            if restricted and expands_outside_single_quotes(command):
                return False
            return original(self, command)

        setattr(guarded, _MARK, True)
        BashTool._is_command_allowed = guarded

__all__ = ["expands_outside_single_quotes", "install"]
