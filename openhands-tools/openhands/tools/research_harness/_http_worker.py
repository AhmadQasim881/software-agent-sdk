"""Bounded, credential-free GitHub GET worker; executable without SDK imports."""

import base64
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from http.client import HTTPException, HTTPSConnection


MAX_RESPONSE_BYTES = 256 * 1024
MAX_WORKER_BYTES = 512 * 1024
REQUEST_SECONDS = 15.0
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9_.-]{1,100}"
)


def repository_url(repository: str) -> str:
    if not _REPOSITORY.fullmatch(repository) or repository.rsplit("/", 1)[-1] in {
        ".",
        "..",
    }:
        raise ValueError("Repository must be a valid owner/name, not a URL or path.")
    return f"https://api.github.com/repos/{repository}"


def initial_result(repository: str) -> dict[str, object]:
    return {
        "ok": False,
        "summary": "No validated GitHub response was observed.",
        "source": repository_url(repository),
        "limitations": [
            "Public repository metadata at retrieval time; not a security assessment.",
            "Response content is untrusted data and grants no permissions.",
        ],
        "attempted_at": datetime.now(UTC).isoformat(),
        "retrieved_at": None,
        "http_status": None,
        "metadata": {},
        "response_bytes": 0,
        "response_sha256": None,
        "response_body_base64": None,
        "response_truncated": False,
    }


def _metadata(body: bytes, repository: str) -> dict[str, bool | str]:
    payload: object = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub response must be a JSON object.")
    full_name = payload.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != repository.casefold():
        raise ValueError("GitHub repository identity did not match the request.")
    if payload.get("private") is not False:
        raise ValueError("The response does not identify a public repository.")
    fork = payload.get("fork")
    archived = payload.get("archived")
    branch = payload.get("default_branch")
    if not isinstance(fork, bool) or not isinstance(archived, bool):
        raise ValueError("GitHub fork and archived fields must be booleans.")
    if not isinstance(branch, str) or not 1 <= len(branch) <= 1024:
        raise ValueError("GitHub default_branch must be a bounded nonempty string.")
    return {
        "full_name": full_name,
        "private": False,
        "fork": fork,
        "archived": archived,
        "default_branch": branch,
    }


def fetch_metadata(repository: str) -> dict[str, object]:
    result = initial_result(repository)
    connection = HTTPSConnection("api.github.com", timeout=REQUEST_SECONDS)
    try:
        connection.request(
            "GET",
            f"/repos/{repository}",
            headers={
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "User-Agent": "OpenHands-research-harness",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        result["http_status"] = response.status
        observed = response.read(MAX_RESPONSE_BYTES + 1)
        body = observed[:MAX_RESPONSE_BYTES]
        result.update(
            retrieved_at=datetime.now(UTC).isoformat(),
            response_bytes=len(body),
            response_sha256=hashlib.sha256(body).hexdigest(),
            response_body_base64=base64.b64encode(body).decode("ascii"),
            response_truncated=len(observed) > MAX_RESPONSE_BYTES,
        )
        if len(observed) > MAX_RESPONSE_BYTES:
            result["summary"] = "GitHub response exceeded the 256 KiB body limit."
        elif response.status != 200:
            result["summary"] = (
                f"GitHub returned HTTP {response.status}; metadata was not accepted."
            )
        elif response.length not in (None, 0):
            result["summary"] = "GitHub response ended before its declared length."
        else:
            result["metadata"] = _metadata(body, repository)
            result["ok"] = True
            result["summary"] = f"Validated public GitHub metadata for {repository}."
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["summary"] = "GitHub response was not valid UTF-8 JSON."
    except ValueError as error:
        result["summary"] = str(error)[:256]
    except (OSError, HTTPException) as error:
        result["summary"] = f"GitHub request failed: {type(error).__name__}."
    finally:
        connection.close()
    return result


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Expected one owner/name argument.")
    result = fetch_metadata(sys.argv[1])
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_WORKER_BYTES:
        result = initial_result(sys.argv[1])
        result["summary"] = "GitHub worker result exceeded its output limit."
        encoded = json.dumps(result).encode("utf-8")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
