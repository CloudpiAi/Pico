"""Every cost question: the LLM gets the question + the `focus` table's schema, checks which columns can answer it,
writes one SQL query itself (any columns, filters and groupings), runs it on the cache and answers.

Why: scheduled task t57 hit the 15-step limit on 2026-11-12. The agent wasn't given the table, so it spent its steps
on load_skill / list_tables / describe_table and value lookups; and every result over 20 rows reaches it cut to the
first and last 10 rows (pico/utils/compress_utils.py), so it queried again.

The prompt carries the schema, the flow and the rules the schema can't express, with two short example queries
(a pattern, not a menu of question types). These tests pin that and run the SQL the prompt contains in DuckDB on a
small table with all 60 columns.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "focus"
PKG_DIR = PLUGIN_ROOT / "pico_plugin_focus_data"
sys.path.insert(0, str(PLUGIN_ROOT))

# The cache's columns (a test copy of /data/focus.duckdb, 2026-11-12): FOCUS 1.2 + AWS x_ columns.
CACHE_COLUMNS = {
    "availabilityzone": "VARCHAR", "billedcost": "DOUBLE", "billingaccountid": "VARCHAR",
    "billingaccountname": "VARCHAR", "billingaccounttype": "VARCHAR", "billingcurrency": "VARCHAR",
    "billingperiodend": "TIMESTAMP", "billingperiodstart": "TIMESTAMP", "capacityreservationid": "VARCHAR",
    "capacityreservationstatus": "VARCHAR", "chargecategory": "VARCHAR", "chargeclass": "VARCHAR",
    "chargedescription": "VARCHAR", "chargefrequency": "VARCHAR", "chargeperiodend": "TIMESTAMP",
    "chargeperiodstart": "TIMESTAMP", "commitmentdiscountcategory": "VARCHAR", "commitmentdiscountid": "VARCHAR",
    "commitmentdiscountname": "VARCHAR", "commitmentdiscountquantity": "DOUBLE", "commitmentdiscounttype": "VARCHAR",
    "commitmentdiscountstatus": "VARCHAR", "commitmentdiscountunit": "VARCHAR", "consumedquantity": "DOUBLE",
    "consumedunit": "VARCHAR", "contractedcost": "DOUBLE", "contractedunitprice": "DOUBLE", "effectivecost": "DOUBLE",
    "invoiceid": "VARCHAR", "invoiceissuername": "VARCHAR", "listcost": "DOUBLE", "listunitprice": "DOUBLE",
    "pricingcategory": "VARCHAR", "pricingcurrency": "VARCHAR", "pricingcurrencycontractedunitprice": "DOUBLE",
    "pricingcurrencyeffectivecost": "DOUBLE", "pricingcurrencylistunitprice": "DOUBLE", "pricingquantity": "DOUBLE",
    "pricingunit": "VARCHAR", "providername": "VARCHAR", "publishername": "VARCHAR", "regionid": "VARCHAR",
    "regionname": "VARCHAR", "resourceid": "VARCHAR", "resourcename": "VARCHAR", "resourcetype": "VARCHAR",
    "servicecategory": "VARCHAR", "servicename": "VARCHAR", "servicesubcategory": "VARCHAR", "skuid": "VARCHAR",
    "skupricedetails": "MAP(VARCHAR, VARCHAR)", "skupriceid": "VARCHAR", "skumeter": "VARCHAR",
    "subaccountid": "VARCHAR", "subaccountname": "VARCHAR", "subaccounttype": "VARCHAR",
    "tags": "MAP(VARCHAR, VARCHAR)", "x_discounts": "MAP(VARCHAR, DOUBLE)", "x_operation": "VARCHAR",
    "x_servicecode": "VARCHAR",
}
MAP_COLUMNS = ("tags", "skupricedetails", "x_discounts")
SECTION = "## The cost table and how to answer from it"

EC2, S3, LAMBDA = "Amazon Elastic Compute Cloud", "Amazon Simple Storage Service", "AWS Lambda"
OCT, NOV = datetime(2026, 10, 1), datetime(2026, 11, 1)


def prompt_text():
    from pico.plugins.base import read_manifest_file
    from pico.plugins.prompt import render_plugin_prompt

    manifest = read_manifest_file(PKG_DIR, "focus-data")
    return render_plugin_prompt(manifest, {}, config_path=None, config_mutable=False)


def cost_section():
    text = prompt_text()
    assert SECTION in text, "the cost-table section"
    return text[text.index(SECTION):]


def skill_text():
    return (PKG_DIR / "skills" / "focus-data" / "SKILL.md").read_text(encoding="utf-8")


def skill_description():
    found = re.search(r"^description:\s*(.+)$", skill_text(), re.M)
    assert found, "the skill has a description line"
    return found.group(1)


def examples():
    """The prompt's example queries, by their `#### ` heading."""
    return dict(re.findall(r"^#### (.+?)\n.*?```sql\n(.*?)```", cost_section(), re.S | re.M))


def inline_sql(label):
    """A query or expression the prompt gives inline as  <label>: `...`."""
    found = re.search(rf"{re.escape(label)}: `([^`]+)`", cost_section())
    assert found, label
    return found.group(1)


# ---------- the fixture table: all 60 columns, a small account ----------

def _row(**kw):
    row = dict.fromkeys(CACHE_COLUMNS)
    row.update(chargecategory="Usage", billingcurrency="USD", providername="AWS", subaccountid="111122223333",
               subaccountname="prod", regionid="us-east-1", tags={}, skupricedetails={}, x_discounts={})
    row.update(kw)
    for column in ("effectivecost", "listcost"):  # no discounts in the fixture account
        if row[column] is None:
            row[column] = row["billedcost"]
    return row


def fixture_rows():
    rows = []
    # 25 EC2 instances, Oct (each in its own region: 25 service+region groups) and the first 7 days of Nov
    for month in (OCT, NOV):
        for i in range(25):
            day = month + timedelta(days=i % 7)
            rows.append(_row(billingperiodstart=month, servicename=EC2, resourceid=f"i-{i:02d}", resourcetype="instance",
                             billedcost=float(50 - i) * (2 if month == NOV else 1), pricingunit="Hours",
                             pricingquantity=24.0, chargeperiodstart=day, chargeperiodend=day + timedelta(hours=1),
                             regionid=f"r-{i:02d}" if month == OCT else "us-east-1",
                             tags={"team": "web" if i % 2 else "data"}))
    # S3 buckets in every resource-id form, Nov only
    s3 = [("bare-bucket", "GB-Months", 2.0, 4.0), ("bare-bucket", "Requests", 1000.0, 1.0),
          ("arn:aws:s3:::arn-bucket", "GB", 3.0, 3.0),
          ("arn:aws:s3tables:us-east-1:111122223333:bucket/s3t-bucket/table/1234", "GB-Months", 1.0, 2.0),
          ("arn:aws:s3tables:us-east-1:111122223333:bucket/s3t-bucket", "Requests", 10.0, 0.5),
          ("", None, None, 0.25)]
    for rid, unit, qty, cost in s3:
        rows.append(_row(billingperiodstart=NOV, servicename=S3, resourceid=rid, pricingunit=unit,
                         pricingquantity=qty, billedcost=cost, chargeperiodstart=NOV, chargeperiodend=NOV + timedelta(hours=1),
                         subaccountid="444455556666", subaccountname="data", regionid="eu-west-1"))
    rows.append(_row(billingperiodstart=NOV, servicename=LAMBDA, billedcost=0.4, pricingunit="Requests",
                     pricingquantity=100.0, chargeperiodstart=NOV, chargeperiodend=NOV + timedelta(hours=1)))
    # account-level EC2 charges (no resource): NULL and '' ids; a blank tag value, as an export may write '' for empty
    rows.append(_row(billingperiodstart=NOV, servicename=EC2, resourceid=None, billedcost=0.7, chargeperiodstart=NOV,
                     chargeperiodend=NOV + timedelta(hours=1), tags={"team": ""}))
    rows.append(_row(billingperiodstart=NOV, servicename=EC2, resourceid="", billedcost=0.3, chargeperiodstart=NOV,
                     chargeperiodend=NOV + timedelta(hours=1)))
    # tax is billed per service and spans the whole month: in the month's service total, never in "data through"
    rows.append(_row(billingperiodstart=NOV, servicename=EC2, chargecategory="Tax", billedcost=5.0,
                     chargeperiodstart=NOV, chargeperiodend=datetime(2026, 12, 1)))
    rows.append(_row(billingperiodstart=OCT, servicename=EC2, chargecategory="Tax", billedcost=3.0,
                     chargeperiodstart=OCT, chargeperiodend=NOV))
    return rows


@pytest.fixture(scope="module")
def db():
    c = duckdb.connect(":memory:")
    c.sql("CREATE TABLE focus (" + ", ".join(f"{n} {t}" for n, t in CACHE_COLUMNS.items()) + ")")
    names = list(CACHE_COLUMNS)
    c.executemany(f"INSERT INTO focus VALUES ({', '.join('?' * len(names))})",
                  [[r[n] for n in names] for r in fixture_rows()])
    return c


def rows_of(db, sql):
    rel = db.sql(sql)
    cols = [d[0] for d in rel.description]
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def total(month, service=None, category="Usage"):
    return sum(r["billedcost"] for r in fixture_rows() if r["billingperiodstart"] == month
               and (service is None or r["servicename"] == service) and (category is None or r["chargecategory"] == category))


# ---------- the flow: question + schema -> the LLM writes one query -> run on the cache -> answer ----------

def test_prompt_states_the_flow():
    low = cost_section().lower()
    assert "every cost question is answered from the `focus` table" in low
    assert "check, from the schema below, which columns can answer it" in low
    assert "if no column holds what is asked" in low  # say so; never guess
    assert "write one sql query yourself" in low
    assert "any combination of columns" in low  # not a fixed list of question types
    assert "1 to 3 tool calls" in low


def test_prompt_rules_for_the_query():
    low = cost_section().lower()
    assert "do not call list_tables, describe_table or load_skill for a cost question" in low
    assert "never select raw rows" in low
    assert "first 10 and last 10 rows" in low and "at most 20 rows" in low
    assert 'top 19 plus one "other n" row' in low
    assert "one lookup query" in low  # only for a value whose exact spelling isn't known
    assert "the cost data is not loaded yet" in low


def test_prompt_is_a_pattern_not_a_menu():
    """Two short examples; no catalogue of question types the model might think it is limited to."""
    assert len(examples()) == 2
    assert "question types" not in cost_section().lower()


def test_scope_rules_still_hold():
    low = prompt_text().lower()
    assert "reply with exactly" not in low  # no fixed canned reply (pinned in test_focus_plugin)
    assert "do not mention tools, sql or table names" in low


def test_skill_is_not_the_place_for_cost_questions():
    """A skill costs a step to load; cost questions are answered from the prompt."""
    desc = skill_description().lower()
    assert "aws cost questions" not in desc
    assert "last loaded" in desc and "reload" in desc and "set up" in desc
    assert "## Answering cost questions" not in skill_text()


# ---------- the schema: every column, its meaning and its values ----------

def test_cache_column_list_is_complete():
    assert len(CACHE_COLUMNS) == 60


def test_prompt_names_every_cache_column():
    text = cost_section()
    missing = [c for c in CACHE_COLUMNS if f"`{c}`" not in text]
    assert not missing, missing


def test_prompt_says_which_columns_are_maps_and_how_to_read_them():
    low = cost_section().lower()
    for column in MAP_COLUMNS:
        assert re.search(rf"`{column}`[^\n]*\bmap\b", low), column
    assert "tags['" in low and "skupricedetails['instancetype']" in low


def test_prompt_lists_the_fixed_focus_values():
    text = cost_section()
    for value in ("Usage", "Purchase", "Tax", "Credit", "Adjustment",  # ChargeCategory
                  "Usage-Based", "Recurring", "One-Time",  # ChargeFrequency
                  "Standard", "Committed", "Dynamic",  # PricingCategory
                  "Compute", "Storage", "Databases", "Networking"):  # ServiceCategory
        assert f"`{value}`" in text, value


def test_prompt_gives_the_account_values_a_query_needs():
    text = cost_section()
    assert f"'{S3}'" in text and f"'{EC2}'" in text
    for unit in ("GB-Months", "Requests", "`GB`", "Hours"):
        assert unit in text, unit


# ---------- the rules the schema can't express ----------

def test_prompt_cost_and_time_rules():
    low = cost_section().lower()
    assert "use `billedcost` for \"cost\"" in low and "`effectivecost`" in low
    assert "never add a time-zone offset" in low
    assert "max(chargeperiodend)" in low and "tax rows span the whole month" in low
    assert "never add different units" in low


def test_prompt_tax_and_empty_value_rules():
    low = cost_section().lower()
    assert "month-level breakdowns by service, account or region use every charge" in low
    assert "say a breakdown shows usage only" in low
    assert "coalesce(<column>, '') = ''" in low
    assert "(account-level)" in low


def test_s3_bucket_expression_reads_every_resource_id_form(db):
    expr = inline_sql("S3 bucket name")
    names = {r[0] for r in db.sql(f"SELECT DISTINCT {expr} FROM focus WHERE servicename = '{S3}'").fetchall()}
    assert names == {"bare-bucket", "arn-bucket", "s3t-bucket", "(account-level)"}


def test_tag_key_lookup_runs(db):
    assert "team" in [r[0] for r in db.sql(inline_sql("Tag keys in use")).fetchall()]


def test_data_through_query_ignores_tax(db):
    (row,) = db.sql(inline_sql("Data through")).fetchall()
    assert row[0] == datetime(2026, 11, 7, 1, 0)


# ---------- the two example queries run, stay within 20 rows and give the right figures ----------

def test_several_columns_example_keeps_the_month_total_in_20_rows(db):
    """Service x region for October: 26 groups -> the top 19 + 'other 7'; tax included, so it adds up to the bill."""
    rows = rows_of(db, examples()["Several columns at once"])
    assert len(rows) == 20 and rows[-1]["servicename"] == "other 7 rows"
    assert rows[0]["servicename"] == EC2 and rows[0]["regionid"] == "r-00"
    assert round(sum(r["cost"] for r in rows), 2) == round(total(OCT, category=None), 2)


def test_tag_example_reads_the_map_and_treats_blank_as_no_tag(db):
    rows = {r["team"]: r["cost"] for r in rows_of(db, examples()["A tag, usage only"])}
    assert set(rows) == {"web", "data", "(no team tag)"}
    assert round(sum(rows.values()), 2) == round(total(NOV, EC2), 2)  # Usage rows: no tax
