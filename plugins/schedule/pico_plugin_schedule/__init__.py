"""Scheduled Slack tasks as a Pico plugin: `pico schedule <action>` (reminder, report, query, ask).

Independent of any data source or host: the host (the standalone Pico FinOps Agent, or another host) gives it a data source
for report figures and, if it has one, its data-load schedule. Nothing here imports the FOCUS plugin or the agent.
"""
