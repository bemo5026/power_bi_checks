"""Tests for the extraction of Snowflake objects from Power Query expressions."""

from snowflake_lineage.m_parser import SnowflakeObject, extract_snowflake_objects, extract_sql_tables

SERVER = "acme.snowflakecomputing.com"


def test_connector_navigation_to_table_and_view() -> None:
    """Tables and views reached through connector navigation are listed with their path."""
    expression = """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "REPORTING_WH"),
    Db = Source{[Name="ANALYTICS", Kind="Database"]}[Data],
    Sch = Db{[Name="SALES", Kind="Schema"]}[Data],
    Orders = Sch{[Name="ORDERS", Kind="Table"]}[Data],
    Active = Sch{[Name="V_ACTIVE_CUSTOMERS", Kind="View"]}[Data]
in
    Active"""

    assert extract_snowflake_objects(expression, {}) == [
        SnowflakeObject(SERVER, "REPORTING_WH", "ANALYTICS", "SALES", "ORDERS", "Table"),
        SnowflakeObject(SERVER, "REPORTING_WH", "ANALYTICS", "SALES", "V_ACTIVE_CUSTOMERS", "View"),
    ]


def test_parameters_are_resolved() -> None:
    """Server, warehouse and names given by Power Query parameters are replaced by their values."""
    shared = {
        "Server": '"acme.snowflakecomputing.com" meta [IsParameterQuery=true, Type="Text"]',
        "Warehouse": '"WH" meta [IsParameterQuery=true, Type="Text"]',
        "Database Name": '"PROD" meta [IsParameterQuery=true, Type="Text"]',
    }
    expression = """let
    Source = Snowflake.Databases(Server, Warehouse),
    Db = Source{[Name=#"Database Name", Kind="Database"]}[Data],
    Sch = Db{[Kind="Schema", Name="CORE"]}[Data],
    T = Sch{[Name=MissingParam, Kind="Table"]}[Data]
in
    T"""

    assert extract_snowflake_objects(expression, shared) == [
        SnowflakeObject(SERVER, "WH", "PROD", "CORE", "{MissingParam}", "Table"),
    ]


def test_connection_in_shared_query() -> None:
    """A table that starts from a shared query inherits its connection and navigation."""
    shared = {
        "Snowflake Core": """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),
    Db = Source{[Name="PROD", Kind="Database"]}[Data],
    Sch = Db{[Name="CORE", Kind="Schema"]}[Data]
in
    Sch""",
    }
    expression = 'let Source = #"Snowflake Core", T = Source{[Name="DIM_DATE", Kind="Table"]}[Data] in T'

    assert extract_snowflake_objects(expression, shared) == [
        SnowflakeObject(SERVER, "WH", "PROD", "CORE", "DIM_DATE", "Table"),
    ]


def test_native_query() -> None:
    """Objects in a native SQL query are qualified with the navigated database and schema."""
    expression = """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),
    Db = Source{[Name="PROD", Kind="Database"]}[Data],
    Result = Value.NativeQuery(Db, "select o.* from SALES.ORDERS o#(lf)join ""Mixed"".""Case"" c on 1=1", null)
in
    Result"""

    sql = 'select o.* from SALES.ORDERS o\njoin "Mixed"."Case" c on 1=1'
    assert extract_snowflake_objects(expression, {}) == [
        SnowflakeObject(SERVER, "WH", "PROD", "SALES", "ORDERS", "SQL reference", sql),
        SnowflakeObject(SERVER, "WH", "PROD", "Mixed", "Case", "SQL reference", sql),
    ]


def test_sql_tables_skip_ctes_comments_strings_and_table_functions() -> None:
    """Only real tables are reported: not CTE names, commented code, text in strings or table functions."""
    sql = """with recent as (select * from db1.s1.orders), totals as (select 1)
select * from recent -- from commented_table
join (select * from s2.items) i on true
join table(flatten(input => x)) f
, lateral flatten(input => y) l
where note = 'from fake_table'"""

    assert set(extract_sql_tables(sql)) == {("DB1", "S1", "ORDERS"), (None, "S2", "ITEMS")}


def test_sql_with_several_datasets_and_statements() -> None:
    """Joins, unions and several statements report every table once."""
    sql = """select * from DATASET."dnexr-a" a join DATASET."dnexr-b" b on a.id = b.id
union all select * from DATASET."dnexr-a";
select * from LAB.futures"""

    assert set(extract_sql_tables(sql)) == {
        (None, "DATASET", "dnexr-a"),
        (None, "DATASET", "dnexr-b"),
        (None, "LAB", "FUTURES"),
    }


def test_native_query_around_the_connection() -> None:
    """A native query wrapped around the connector call, with a navigation step without Kind."""
    expression = (
        "let\n"
        '    Source = Value.NativeQuery(Snowflake.Databases("acme.snowflakecomputing.com","DNEXR",'
        '[Implementation="2.0"]){[Name="DNEXR"]}[Data], '
        '"select * from DNEXR.DATASET.""dnexr-dnexr-biof-uns-yearly""", null, [EnableFolding=true])\n'
        "in\n"
        "    Source"
    )

    sql = 'select * from DNEXR.DATASET."dnexr-dnexr-biof-uns-yearly"'
    assert extract_snowflake_objects(expression, {}) == [
        SnowflakeObject(SERVER, "DNEXR", "DNEXR", "DATASET", "dnexr-dnexr-biof-uns-yearly", "SQL reference", sql),
    ]


def test_navigation_without_kind() -> None:
    """Without Kind, the level comes from the position: database, schema, then object."""
    expression = """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH", [Implementation="2.0"]),
    Db = Source{[Name="PROD"]}[Data],
    Sch = Db{[Name="SALES"]}[Data],
    T = Sch{[Name="ORDERS"]}[Data]
in
    T"""

    assert extract_snowflake_objects(expression, {}) == [
        SnowflakeObject(SERVER, "WH", "PROD", "SALES", "ORDERS", "Table or view"),
    ]


def test_sql_from_a_step_and_a_parameter() -> None:
    """SQL kept in a step and built with & from a parameter is resolved."""
    shared = {"Dataset": '"dnexr-x" meta [IsParameterQuery=true, Type="Text"]'}
    expression = (
        "let\n"
        '    Query = "select * from DATASET.""" & Dataset & """",\n'
        '    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),\n'
        '    Db = Source{[Name="PROD", Kind="Database"]}[Data],\n'
        "    Result = Value.NativeQuery(Db, Query)\n"
        "in\n"
        "    Result"
    )

    [item] = extract_snowflake_objects(expression, shared)

    assert (item.database, item.schema, item.name, item.kind) == ("PROD", "DATASET", "dnexr-x", "SQL reference")


def test_unparseable_sql_is_reported() -> None:
    """SQL that can't be parsed gives one row holding the code instead of disappearing."""
    expression = """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),
    Db = Source{[Name="PROD", Kind="Database"]}[Data],
    Result = Value.NativeQuery(Db, "select from where (")
in
    Result"""

    [item] = extract_snowflake_objects(expression, {})

    assert (item.database, item.name, item.kind) == ("PROD", None, "SQL not parsed")
    assert item.native_sql == "select from where ("


def test_m_comments_are_ignored() -> None:
    """Navigation in a commented-out step is not reported."""
    expression = """let
    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),
    Db = Source{[Name="PROD", Kind="Database"]}[Data],
    Sch = Db{[Name="SALES", Kind="Schema"]}[Data],
    // Old = Sch{[Name="OLD_ORDERS", Kind="Table"]}[Data],
    T = Sch{[Name="ORDERS", Kind="Table"]}[Data]
in
    T"""

    assert [item.name for item in extract_snowflake_objects(expression, {})] == ["ORDERS"]


def test_non_snowflake_expression() -> None:
    """Expressions using another connector produce nothing."""
    expression = 'let Source = Sql.Database("server", "db"), T = Source{[Schema="dbo",Item="Orders"]}[Data] in T'

    assert extract_snowflake_objects(expression, {}) == []
