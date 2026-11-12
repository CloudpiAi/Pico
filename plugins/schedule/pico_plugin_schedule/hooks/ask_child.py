"""`python -m pico_plugin_schedule.hooks.ask_child -p "<question>"`: Pico's one-shot mode for a scheduled `ask` task,
with the bash guard installed first (no `$` / backtick expansion in allow-listed commands). kinds._ask runs it with
no secrets in the environment except the LLM keys (`*_API_KEY`) and PICO_FINOPS_AGENT_UNATTENDED=1.
"""
import sys

from . import bash_guard


def prepare():
    bash_guard.install()


def main(argv=None):
    prepare()
    from pico.entrypoints import cli_main

    sys.argv = ["pico", *(sys.argv[1:] if argv is None else argv)]
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
