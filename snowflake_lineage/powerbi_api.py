"""Read workspaces, reports and semantic model definitions with a user's access token.

No admin rights are needed: the calls only return what the token's account
can access, and a model's definition needs edit (write) access to that model.
"""

import base64
import json
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

POWER_BI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API = "https://api.fabric.microsoft.com/v1"
REQUEST_TIMEOUT_SECONDS = 120
OPERATION_TIMEOUT_SECONDS = 600
DEFAULT_POLL_SECONDS = 3
DEFINITION_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 15
TRANSIENT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
"""Network failures worth retrying: dropped connections and timeouts, also while reading a response."""


def create_session(access_token: str, pool_size: int) -> requests.Session:
    """Return an HTTP session with the bearer token that retries throttled and failed calls."""
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"
    retry = Retry(
        total=6,
        backoff_factor=5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=pool_size))
    return session


def raise_for_status(response: requests.Response) -> None:
    """Raise on an HTTP error, keeping the API error body in the message."""
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        message = f"{error}\nResponse body: {response.text}"
        if response.status_code == 401:  # noqa: PLR2004
            message += "\nThe token has probably expired (they last about one hour): get a new one."
        raise requests.HTTPError(message, response=response) from error


def _get_json(session: requests.Session, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET a URL and return its JSON body."""
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    raise_for_status(response)
    return response.json()


def list_workspaces(session: requests.Session) -> list[dict[str, Any]]:
    """Return the workspaces the account can access, including its personal one (My workspace)."""
    workspaces: list[dict[str, Any]] = []
    params: dict[str, str] = {}
    while True:
        page = _get_json(session, f"{FABRIC_API}/workspaces", params)
        workspaces.extend(page["value"])
        if not page.get("continuationToken"):
            return workspaces
        params = {"continuationToken": page["continuationToken"]}


def list_reports(session: requests.Session, workspace: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the reports of a workspace.

    The personal workspace can't be read through the group endpoints, so its
    reports come from the account's own report list.
    """
    if workspace["type"] == "Personal":
        return _get_json(session, f"{POWER_BI_API}/reports")["value"]
    return _get_json(session, f"{POWER_BI_API}/groups/{workspace['id']}/reports")["value"]


def list_semantic_models(session: requests.Session, workspace_id: str) -> list[dict[str, Any]]:
    """Return the semantic models of a workspace (id and displayName)."""
    models: list[dict[str, Any]] = []
    params: dict[str, str] = {}
    while True:
        page = _get_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels", params)
        models.extend(page["value"])
        if not page.get("continuationToken"):
            return models
        params = {"continuationToken": page["continuationToken"]}


def _wait_for_operation(session: requests.Session, response: requests.Response) -> dict[str, Any]:
    """Follow a Fabric long-running operation and return its result.

    Raises:
        RuntimeError: If the operation fails.
        TimeoutError: If it doesn't finish in time.
    """
    location = response.headers["Location"]
    delay = int(response.headers.get("Retry-After", DEFAULT_POLL_SECONDS))
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(delay)
        state = _get_json(session, location)
        if state["status"] == "Succeeded":
            return _get_json(session, f"{location}/result")
        if state["status"] == "Failed":
            message = f"Operation failed: {json.dumps(state.get('error'))}"
            raise RuntimeError(message)
    message = f"Operation {location} did not finish within {OPERATION_TIMEOUT_SECONDS} seconds"
    raise TimeoutError(message)


def get_model_definition(session: requests.Session, workspace_id: str, model_id: str) -> dict[str, Any]:
    """Return a semantic model's TMSL definition (the ``model`` object of model.bim).

    The download is retried from the start when the connection drops or
    times out, including in the middle of a response body, which the
    session's own retries don't cover.
    """
    attempt = 1
    while True:
        try:
            return _download_model_definition(session, workspace_id, model_id)
        except TRANSIENT_ERRORS:
            if attempt == DEFINITION_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS * attempt)
            attempt += 1


def _download_model_definition(session: requests.Session, workspace_id: str, model_id: str) -> dict[str, Any]:
    """Start the getDefinition operation, wait for it and decode model.bim."""
    response = session.post(
        f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition",
        params={"format": "TMSL"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    raise_for_status(response)
    result = _wait_for_operation(session, response) if response.status_code == 202 else response.json()  # noqa: PLR2004
    parts = {part["path"]: part["payload"] for part in result["definition"]["parts"]}
    return json.loads(base64.b64decode(parts["model.bim"]))["model"]
