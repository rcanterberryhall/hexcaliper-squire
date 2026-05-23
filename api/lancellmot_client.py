"""HTTP client for lancellmot's workspace + documents API (parsival#43).

Used by parsival to resolve project tags to lancellmot projects and fetch
related documents for the situation-card chip. Failures (network error,
timeout, non-2xx) raise ``LancellmotUnavailable`` so callers can render the
amber "unreachable" chip state instead of silently hiding the link.
"""
import requests

import config


DEFAULT_TIMEOUT = 2.0


class LancellmotUnavailable(Exception):
    """Raised on network error, timeout, or non-2xx response from lancellmot."""


def list_projects() -> list[dict]:
    """Return all lancellmot projects. Raises LancellmotUnavailable on failure."""
    url = f"{config.LANCELLMOT_URL}/workspace/projects"
    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise LancellmotUnavailable(str(exc)) from exc


def list_documents(project_id: str, limit: int = 5) -> list[dict]:
    """Return the first ``limit`` documents for a lancellmot project.

    Raises LancellmotUnavailable on network error, timeout, or non-2xx response.
    """
    url = f"{config.LANCELLMOT_URL}/documents"
    params = {"project_id": project_id}
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        docs = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise LancellmotUnavailable(str(exc)) from exc
    return docs[:limit]
