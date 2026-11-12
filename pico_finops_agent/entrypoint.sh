#!/bin/sh
# Start-time check of the Pico FinOps Agent's .env values, then exec the command.
#
# Pico fills ${VAR} in the profile at config load but turns a missing one into the text
# <MISSING:VAR> without an error, so a wrong .env would only show up at the first load hours
# later. This script stops the start instead, naming the variable. It prints variable NAMES only,
# never values, and writes no files. Then `exec "$@"` so signals reach the app directly.
#
# Format rules (the loader Lambda checks its payload with the same ones):
#   bucket names     ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$
#   export prefix    not starting with /, ends with /, no "..", no quote or ;
#   table bucket ARN ^arn:aws[a-z-]*:s3tables:<region>:[0-9]{12}:bucket/[a-z0-9-]{3,63}$, region = FOCUS_REGION
#   role ARN         ^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$
#   namespace/table  ^[a-z0-9_]{1,255}$
#   state key        ends with .json, no ".."
#   schedule         5 cron fields
#   region           ^[a-z]{2}(-[a-z]+)+-[0-9]$
# FOCUS_DATA_DIR (default /data) is for tests only; it is not a customer setting.
set -eu

PROG=pico-finops-agent

fail() {
    printf '%s: %s %s\n' "$PROG" "$1" "$2" >&2
    exit 1
}

# Value of the variable named $1, or empty when unset (never printed).
val() {
    eval "printf '%s' \"\${$1-}\""
}

is_set() {
    eval "[ \"\${$1+x}\" = x ]"
}

need() {
    is_set "$1" || fail "$1" "is not set (see pico_finops_agent/.env.example)"
    [ -n "$(val "$1")" ] || fail "$1" "is empty"
}

# An optional variable that is set must not be empty: Pico applies ${VAR:-default} only when unset.
opt_set() {
    if is_set "$1"; then
        [ -n "$(val "$1")" ] || fail "$1" "is set but empty; remove the line to use the default"
        return 0
    fi
    return 1
}

match() {
    val "$1" | grep -Eq "$2" || fail "$1" "$3"
}

no_match() {
    if val "$1" | grep -Eq "$2"; then
        fail "$1" "$3"
    fi
}

BUCKET_RE='^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
NAME_RE='^[a-z0-9_]{1,255}$'

# 1. Required values set and non-empty.
for name in FOCUS_REGION FOCUS_EXPORT_BUCKET FOCUS_EXPORT_PREFIX FOCUS_TABLE_BUCKET_ARN \
            FOCUS_STATE_BUCKET FOCUS_LAMBDA_ROLE_ARN; do
    need "$name"
done

# 2. Formats.
match FOCUS_REGION '^[a-z]{2}(-[a-z]+)+-[0-9]$' "is not an AWS Region (e.g. us-east-1)"
match FOCUS_EXPORT_BUCKET "$BUCKET_RE" "is not a valid S3 bucket name"
match FOCUS_STATE_BUCKET "$BUCKET_RE" "is not a valid S3 bucket name"
no_match FOCUS_EXPORT_PREFIX '^[sS]3://' "must be a key path inside the bucket (e.g. focus2/focus2/data/), not an s3:// URL"
case "$(val FOCUS_EXPORT_PREFIX)" in
    "$(val FOCUS_EXPORT_BUCKET)"/*) printf '%s: %s %s\n' "$PROG" FOCUS_EXPORT_PREFIX "starts with the bucket name: it is a key path inside the bucket (e.g. focus2/focus2/data/), so leave the bucket name out unless a folder really has that name" >&2 ;;
esac
match FOCUS_EXPORT_PREFIX '/$' "must end with /"
no_match FOCUS_EXPORT_PREFIX '^/' "must not start with /"
no_match FOCUS_EXPORT_PREFIX "\.\.|'|\"|;" "must not contain .., quotes or ;"
case "$(val FOCUS_EXPORT_PREFIX)" in
    data/ | */data/) ;;
    *) printf '%s: %s %s
' "$PROG" FOCUS_EXPORT_PREFIX "doesn't end with data/ (an AWS FOCUS export keeps its files under .../data/): the export's manifests can't be found, so an overwrite-mode month is read from every file in its folder" >&2 ;;
esac
region=$(val FOCUS_REGION)
match FOCUS_TABLE_BUCKET_ARN "^arn:aws[a-z-]*:s3tables:${region}:[0-9]{12}:bucket/[a-z0-9-]{3,63}\$" \
    "must be an S3 Tables table bucket ARN in FOCUS_REGION"
match FOCUS_LAMBDA_ROLE_ARN '^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$' "must be an IAM role ARN"

if opt_set FOCUS_NAMESPACE; then match FOCUS_NAMESPACE "$NAME_RE" "may contain only a-z, 0-9 and _"; fi
if opt_set FOCUS_TABLE; then match FOCUS_TABLE "$NAME_RE" "may contain only a-z, 0-9 and _"; fi
if opt_set FOCUS_STATE_KEY; then
    match FOCUS_STATE_KEY '\.json$' "must end with .json"
    no_match FOCUS_STATE_KEY '\.\.' "must not contain .."
fi
if opt_set FOCUS_MONTHS; then
    match FOCUS_MONTHS '^([1-9]|[1-9][0-9]|1[01][0-9]|120)$' "must be a whole number of months from 1 to 120"
fi
if opt_set FOCUS_SCHEDULE; then
    match FOCUS_SCHEDULE '^[^ ]+( [^ ]+){4}$' "must be 5 cron fields, e.g. \"0 6,18 * * *\""
fi

# Optional thresholds for the post-load notice's "sudden increase" (defaults 50 / 1 / 0.5 when unset).
for name in FOCUS_ALERT_INCREASE_PCT FOCUS_ALERT_MIN_SERVICE_USD FOCUS_ALERT_MIN_RESOURCE_USD FOCUS_ALERT_STALE_DAYS; do
    if opt_set "$name"; then match "$name" '^[0-9]+([.][0-9]+)?$' "must be a number (e.g. 50 or 0.5)"; fi
done

# Scheduled Slack tasks (all optional; unset = the defaults: 5 history changes per task, 1000 asks a day).
for name in FOCUS_REPORT_HISTORY_MAX FOCUS_ASK_MAX_PER_DAY; do
    if opt_set "$name"; then match "$name" '^[1-9][0-9]*$' "must be a whole number of 1 or more"; fi
done
# The lists may be empty: no channel list = any channel the bot is in; no creators = nobody may post tasks to
# a channel (DM tasks still work); no admins = only each task's own creator changes it.
if [ -n "$(val FOCUS_REPORT_CHANNELS)" ]; then
    match FOCUS_REPORT_CHANNELS '^[CG][A-Z0-9]{8,}( ?, ?[CG][A-Z0-9]{8,})*$' \
        "must be Slack channel IDs (C...), comma-separated"
fi
if [ -n "$(val FOCUS_REPORT_CREATORS)" ]; then
    match FOCUS_REPORT_CREATORS '^[UW][A-Z0-9]{8,}( ?, ?[UW][A-Z0-9]{8,})*$' \
        "must be Slack user IDs (U...), comma-separated"
fi
if [ -n "$(val FOCUS_REPORT_ADMINS)" ]; then
    match FOCUS_REPORT_ADMINS '^[UW][A-Z0-9]{8,}( ?, ?[UW][A-Z0-9]{8,})*$' \
        "must be Slack user IDs (U...), comma-separated"
fi
if opt_set FOCUS_REPORT_ALLOW_MENTIONS; then
    match FOCUS_REPORT_ALLOW_MENTIONS '^(true|false)$' "must be true or false"
fi

# Slack answers (pico_finops_agent/slack_guard.py; all optional, defaults 8 / 180 / 30 when unset).
if opt_set AGENT_HISTORY_TURNS; then
    match AGENT_HISTORY_TURNS '^([1-9]|[1-4][0-9]|50)$' "must be a whole number of turns from 1 to 50"
fi
if opt_set AGENT_ANSWER_DEADLINE_S; then
    match AGENT_ANSWER_DEADLINE_S '^([3-9][0-9]|[1-8][0-9][0-9]|900)$' "must be a whole number of seconds from 30 to 900"
fi
if opt_set AGENT_QUESTIONS_PER_HOUR; then
    match AGENT_QUESTIONS_PER_HOUR '^(0|[1-9][0-9]{0,4})$' "must be a whole number (0 = no limit)"
fi

# 3. The LLM (pico_finops_agent/llm.py, same names): AGENT_LLM_PROVIDER picks it (default gemini); its key must be set.
provider=gemini
if opt_set AGENT_LLM_PROVIDER; then
    match AGENT_LLM_PROVIDER '^(gemini|openai|anthropic|claude|deepseek)$' \
        "must be one of gemini, openai, anthropic (or claude), deepseek"
    provider=$(val AGENT_LLM_PROVIDER)
fi
if opt_set AGENT_LLM_MODEL; then
    match AGENT_LLM_MODEL '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' "is not a model name (e.g. gpt-5.5)"
fi
case "$provider" in
    openai) llm_key=OPENAI_API_KEY ;;
    anthropic|claude) llm_key=ANTHROPIC_API_KEY ;;
    deepseek) llm_key=DEEPSEEK_API_KEY ;;
    *) llm_key=GEMINI_API_KEY ;;
esac
[ -n "$(val "$llm_key")" ] || fail "$llm_key" "is not set: the agent's LLM is $provider (AGENT_LLM_PROVIDER)"

# 4. Slack: both tokens or neither.
app=$(val SLACK_APP_TOKEN)
bot=$(val SLACK_BOT_TOKEN)
if [ -n "$app" ] || [ -n "$bot" ]; then
    [ -n "$app" ] || fail SLACK_APP_TOKEN "is not set; Slack needs both SLACK_APP_TOKEN and SLACK_BOT_TOKEN"
    [ -n "$bot" ] || fail SLACK_BOT_TOKEN "is not set; Slack needs both SLACK_APP_TOKEN and SLACK_BOT_TOKEN"
    match SLACK_APP_TOKEN '^xapp-' "must start with xapp- (the app-level token)"
    match SLACK_BOT_TOKEN '^xoxb-' "must start with xoxb- (the bot token)"
else
    # The agent (python -m pico_finops_agent.agent) runs without Slack; only a bare `pico-gateway` needs the tokens.
    if [ "$(basename "${1:-}")" = "pico-gateway" ]; then
        fail SLACK_APP_TOKEN "Slack tokens not set; pico-gateway needs both SLACK_APP_TOKEN and SLACK_BOT_TOKEN"
    fi
fi
# Optional: where the scheduler posts "fresh data is in" (the bot posts it, so it needs the bot token).
if opt_set SLACK_NOTIFY_CHANNEL; then
    [ -n "$bot" ] || fail SLACK_NOTIFY_CHANNEL "needs SLACK_BOT_TOKEN (the bot posts the notice)"
    match SLACK_NOTIFY_CHANNEL '^(C[A-Z0-9]{8,}|#?[a-z0-9._-]{1,80})$' "must be a channel name (pico-finops-agent or #pico-finops-agent) or ID (C...)"
fi

# 5. The data directory is writable by this user (checked without writing a file).
data_dir=${FOCUS_DATA_DIR:-/data}
{ [ -d "$data_dir" ] && [ -w "$data_dir" ]; } || fail FOCUS_DATA_DIR "directory is missing or not writable by uid $(id -u)"

# 6. Hand over the process.
exec "$@"
