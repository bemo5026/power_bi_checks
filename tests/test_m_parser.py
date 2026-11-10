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


def test_sql_tables_skip_ctes_and_table_functions() -> None:
    """CTE names, subqueries and table functions are not reported as objects."""
    sql = """with recent as (select * from db1.s1.orders), totals as (select 1)
select * from recent
join (select * from s2.items) i on true
join table(flatten(input => x)) f
, lateral flatten(input => y)"""

    assert extract_sql_tables(sql) == [("DB1", "S1", "ORDERS"), (None, "S2", "ITEMS")]


def test_non_snowflake_expression() -> None:
    """Expressions using another connector produce nothing."""
    expression = 'let Source = Sql.Database("server", "db"), T = Source{[Schema="dbo",Item="Orders"]}[Data] in T'

    assert extract_snowflake_objects(expression, {}) == []
