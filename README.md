# power_bi_checks

Lists the Snowflake tables and views used by Power BI semantic models and the reports built on them.

The tool runs with an Entra app (service principal) or with your own Power BI access token. No admin rights are needed. It downloads the definition of the semantic models listed in `config.toml` (or of every model in the selected workspaces), reads the Snowflake objects from the Power Query (M) code, and links them back to the reports.

## How it works

1. List the workspaces the app or your account can access, including My workspace for a personal token (Fabric `GET /workspaces`).
2. List the semantic models and reports of every accessible workspace, and the model each report uses (Fabric `GET /semanticModels`, Power BI `GET /groups/{id}/reports`, and `GET /reports` for My workspace). Reports are always listed everywhere, because a report can use a model from another workspace. This takes a few seconds.
3. Download the definition of each selected model (Fabric `POST /semanticModels/{id}/getDefinition`, TMSL format), several in parallel. This is the slow step, so list only the models you need in `config.toml`.
4. Parse the Power Query (M) code of each model table. The code is split into tokens (strings, names, brackets, with comments dropped) and walked following the bracket nesting, so function arguments and records are read as structures:
   - **Snowflake connector navigation** (`Snowflake.Databases(...)` then `{[Name="...", Kind="Database|Schema|Table|View"]}`): this gives the database, schema, object and whether it's a table or a view. The `Implementation="2.0"` connector can leave `Kind` out: the level then comes from the position (database, schema, then object), and the object's kind is `Table or view`.
   - **Native SQL** (`Value.NativeQuery(<database>, "SELECT ...")`), also when the connector call is written inside it: the SQL is parsed with [sqlglot](https://github.com/tobymao/sqlglot) (Snowflake dialect), and every table or view it reads is listed with the kind `SQL reference`: joins, unions, subqueries and several statements included; common table expressions, table functions (`TABLE(...)`, `FLATTEN`), comments and text in strings left out. Names the query doesn't qualify get the database and schema navigated to. Snowflake doesn't say there whether they're tables or views.
   - Power Query parameters, text steps (`Query = "select ..."`), `&` concatenations of those, and shared queries (for example a shared "Snowflake connection" query) are resolved. A name that can't be resolved shows as `{ParameterName}`. SQL that can't be resolved or parsed gives one row of kind `SQL not parsed`, with the code in `native_sql`.
   - Commented-out M code (`//`, `/* */`) is ignored.
5. Write one row per report and Snowflake object.

## Access needed

Reading a model's definition needs **edit access** to that model: a Contributor, Member or Admin role in its workspace, or Build plus Write permission on the model. This applies to the app as well as to your account. Models that can only be viewed are listed in the *Skipped models* sheet with the reason.

## Setup

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## Authentication

Put one of the two options in `.env`. `.env` is ignored by git: never commit a secret or a token.

### Option 1: Entra app (service principal)

```
POWERBI_TENANT_ID=<tenant ID>
POWERBI_CLIENT_ID=<application (client) ID>
POWERBI_CLIENT_SECRET=<client secret>
```

The app is used whenever `POWERBI_CLIENT_ID` is set, and it gets its own token on each run. It needs:

1. **The tenant setting** *Developer settings → Service principals can use Fabric APIs*, turned on by a Fabric admin for a security group that contains the app. No admin API setting is needed.
2. **Contributor (or higher) access to each workspace** to read: in the workspace, *Manage access → Add people or groups*, then search for the app's name. The app only sees the workspaces it was added to.

No Power BI API permissions need to be granted to the app in Entra ID: workspace roles control what it can read. An app has no personal workspace, so models and reports in your My workspace aren't covered: use option 2 for those.

### Option 2: your own access token

Remove or empty `POWERBI_CLIENT_ID`, then set `POWERBI_ACCESS_TOKEN=<token>` (the `Bearer ` prefix is optional). The tool then sees what your account sees, My workspace included. Tokens expire after about one hour, so get a new one just before each run:

- **Browser:** Open [app.powerbi.com](https://app.powerbi.com), open the developer tools (F12) and type `copy(powerBIAccessToken)` in the console. The token is copied to the clipboard.
- **PowerShell** (with the `MicrosoftPowerBIMgmt` module, installed once with `Install-Module MicrosoftPowerBIMgmt -Scope CurrentUser`):
  ```powershell
  Connect-PowerBIServiceAccount
  (Get-PowerBIAccessToken)["Authorization"]
  ```

## Configuration

Choose what to read in [config.toml](config.toml):

```toml
# Semantic models to read, by name or ID. Empty: every model of the workspaces below,
# including models no report uses (slow when no workspace is set).
semantic_models = ["6740ce81-3520-493b-862e-b67235f38704", "Economics_China_Domestic Cash"]

# Workspaces whose models are read, by name or ID ("My workspace" is the personal one). Empty: all.
workspaces = []

output_dir = "output"   # folder for the result files
parallel = 8            # model definitions downloaded at the same time; lower it if Power BI throttles
batch_size = 25         # the Excel workbook is rewritten every time this many models are downloaded
reuse_downloaded = true # reuse the model definitions saved by earlier runs (resume after a crash)
```

The model ID is in the model's URL: `app.powerbi.com/groups/<workspace ID>/datasets/<model ID>/...`. Names and IDs are matched without regard to case. A name shared by models in several workspaces selects all of them, so add the workspace to `workspaces` to narrow it down. A name or ID that matches nothing stops the run with an error.

`workspaces` only limits which models are read. Reports are looked up in every workspace you can access, so a model's report count includes reports from other workspaces, and a count of 0 means the model really isn't used by any report you can see.

## Usage

```powershell
.venv\Scripts\python -m snowflake_lineage                          # reads config.toml
.venv\Scripts\python -m snowflake_lineage --config other.toml      # another configuration
```

Outputs, written to `output/` by default:

**`snowflake_lineage.xlsx`**, one sheet per view of the results, each with filters and a frozen header row:

| Sheet | Content |
|---|---|
| Model objects | One row per semantic model, model table and Snowflake object, with the number, IDs and names of the reports built on the model. A `report_count` of 0 means no report uses the model |
| Report lineage | One row per report, model table and Snowflake object: workspace, report, model, model table, server, warehouse, database, schema, object, kind (`Table`, `View`, `SQL reference`) and the native SQL when there is some |
| Snowflake objects | Each distinct Snowflake object, with the number, IDs and names of the reports that use it |
| Skipped models | Selected models whose definition couldn't be read, with the number of reports affected and the reason |

Text longer than an Excel cell (32,767 characters, only very long native SQL) is cut, with a mark at the end.

**`model_definitions/`**: one JSON file per downloaded model definition, for checking a result and resuming a run.

## Long runs: batches, retries and resuming

- **Saved every batch:** the workbook is rewritten every `batch_size` models (25 by default) and at the end, so an interrupted run keeps what it has read. If the workbook is open in Excel during a batch save, that save is skipped and the next one catches up. If it's still open at the end, the results go to a timestamped copy such as `snowflake_lineage_20261110_153000.xlsx`.
- **Retries:** throttled or failed calls (429, 5xx) are retried automatically. A model whose download drops or times out, even in the middle of the response, is downloaded again up to 4 times. If it still fails, it goes to the *Skipped models* sheet and the run continues.
- **Resuming:** each model definition is saved in `model_definitions/` as soon as it's downloaded. When a run stops (network loss, expired token), run the same command again: models already downloaded are reused and only the missing or failed ones are downloaded. Set `reuse_downloaded = false` in `config.toml`, or delete `model_definitions/`, to download everything again after the models have changed.

## Limitations

- Only the workspaces and models your account can access are covered.
- Paginated reports and models without Snowflake sources produce no rows.
- Reports on a live connection to Analysis Services, and push or streaming models, have no Power Query code and end up in the *Skipped models* sheet or produce no rows.
- SQL built dynamically in M (for example with `Text.Combine` or a function call) can't be resolved: it shows as `SQL not parsed`, with the M code in `native_sql`.
- Objects that a Snowflake view reads in turn aren't listed: only what Power BI reads directly.

## Troubleshooting

| Error | Cause |
|---|---|
| `401` / `TokenExpired` | The pasted token has expired or is incomplete: get a new one and run again |
| `Token request failed: invalid_client` | Wrong client ID or secret, or the secret has expired |
| `401` or `403` with the app, or no workspaces found | The *Service principals can use Fabric APIs* setting is off for the app, or the app wasn't added to the workspaces |
| `KeyError: 'POWERBI_...'` | `.env` is missing, or defines neither the app (`POWERBI_CLIENT_ID`, `POWERBI_TENANT_ID`, `POWERBI_CLIENT_SECRET`) nor `POWERBI_ACCESS_TOKEN` |
| `No accessible semantic model matches ...` | Typo in `config.toml`, the model is in a workspace excluded by `workspaces`, or you have no access to it |
| `Unknown keys in config.toml` | A misspelled key in `config.toml` |
| Many `403` rows in the *Skipped models* sheet | You only have view access to those models |
| `Read timed out` or connection errors in the *Skipped models* sheet | The network or Power BI dropped the download 4 times: run the same command again, only those models are downloaded |
| `429` errors | Power BI throttling: run again with a lower `parallel` in `config.toml` |

## Development

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```
