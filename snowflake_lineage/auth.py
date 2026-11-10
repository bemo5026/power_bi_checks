"""Get the Power BI access token, either pasted in the environment or from an Entra app."""

import os

import msal

POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def get_app_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Return an app-only access token for the Power BI and Fabric REST APIs.

    Raises:
        RuntimeError: If Entra ID refuses the token request.
    """
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=[POWER_BI_SCOPE])
    if "access_token" not in result:
        message = f"Token request failed: {result.get('error')}: {result.get('error_description')}"
        raise RuntimeError(message)
    return result["access_token"]


def get_access_token() -> str:
    """Return the token to call Power BI with.

    The Entra app is used when ``POWERBI_CLIENT_ID`` is set (with
    ``POWERBI_TENANT_ID`` and ``POWERBI_CLIENT_SECRET``); otherwise the token
    pasted in ``POWERBI_ACCESS_TOKEN`` is used.
    """
    if os.environ.get("POWERBI_CLIENT_ID"):
        return get_app_token(
            os.environ["POWERBI_TENANT_ID"],
            os.environ["POWERBI_CLIENT_ID"],
            os.environ["POWERBI_CLIENT_SECRET"],
        )
    return os.environ["POWERBI_ACCESS_TOKEN"].strip().removeprefix("Bearer ").strip()
