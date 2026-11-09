---
name: focus-data
description: Use for AWS cost questions on the FOCUS table, for "when was the cost data last loaded / is it up to date", for "reload / refresh the cost data now", and for "set up the cost agent / start collecting the cost data".
---

# FOCUS cost data

The `focus` table in the `focus` DuckDB datasource holds the AWS FOCUS 1.2 billing export, one row per charge.
It is a local copy of the S3 Table. `pico focus-data load` refreshes it: the `pico-finops-agent-loader` Lambda loads new export
deliveries into the S3 Table, then the local copy is rebuilt. The built-in schedule runs it twice a day (06:00 and
18:00 UTC by default), and once at start when there is no copy yet.

## If the data is not loaded yet
If querying `focus` fails because the table or the database file doesn't exist, say: "The cost data is not loaded yet;
the first load runs at start and can take a few minutes." Then run `pico focus-data status` and report `last_load.last_error`
if it is set. Never guess numbers.

## Answering cost questions
- All column names are lowercase (`billedcost`, `effectivecost`, `servicename`, `subaccountid`, `regionid`, `resourceid`, `chargecategory`, `chargedescription`, ...).
- All timestamp columns are plain `TIMESTAMP` in **UTC**. A month is `billingperiodstart = TIMESTAMP '2026-10-01 00:00:00'`. Never add a time-zone offset to the literal.
- `billingperiodstart` is the month of the bill; `chargeperiodstart` / `chargeperiodend` is the hour or period the charge covers.
- Use `billedcost` for "cost" unless the user asks for effective (after discounts) cost.
- The current month is month-to-date. Say so, and give "data through" as `max(chargeperiodend) WHERE chargecategory = 'Usage'`. Tax rows span the whole month (end = next month's first day), so never include them in "data through".

## "When was the data last loaded?"
Run:

```
pico focus-data status
```

Report `last_load.last_success` (UTC), `last_load.last_error` if it is set, and from `s3_table_state` which export delivery each
month came from (`loaded`). If `last_load.schema_diff` is set, say that AWS added a column (`added`) or sent a value the table's column type
can't hold (`cannot_convert`), and the load is stopped
until someone recreates the table (see the setup guide). If `aws_identity` starts with "no AWS credentials", say so.

## "Reload the data now" and "Set up the cost agent"
Both mean the same command. It creates whatever is missing (the loader Lambda, the cost table) and loads the newest
data, so it is also the setup step on a new install. Run it at once, no approval needed, and **call the bash tool with
timeout 600**: a first setup loads every month and takes a few minutes, and the default limit (60 seconds) would stop it.

```
pico focus-data load
```

It prints one JSON line per step. Read them, then answer in plain words; never show the JSON, step names, month codes
like 2026-10, counts of records, or file and function names:
- `loader_function` with `"action": "created"`: you set up the data loader.
- `loader_result` with `"table_created": true`: you created the cost table.
- the months in `loader_result`: name them as "October and November 2026". No months means the data was already
  current; say so and give the date it runs through if you know it.
- `skipped`: a load is already running (on a new install the first one starts by itself). Say the setup or refresh is
  already in progress and that a note will come in the channel when the new data is in.
- `failed`: say in one sentence what is wrong and what to do, from `pico focus-data status` (`last_load.problem`).
Add `--force` only when the user explicitly asks to reload even though nothing new was delivered.

Example after a first setup: "All set. I set up the data loader and the cost table and loaded October and November
2026. Ask me anything about your AWS costs."
