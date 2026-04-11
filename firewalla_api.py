"""
Firewalla MSP API Client
Detailed logging on every request: what was sent, what came back, and why it failed.
URL pattern: https://{msp_domain}/v2/...
"""

import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
RATE_LIMIT_DELAY = 1.0

# ── Shared persistent HTTP client ─────────────────────────────────────────────
# One client for the lifetime of the process: keeps TCP connections alive across
# API calls and avoids per-request TLS handshake overhead.
_http_client: Optional[httpx.AsyncClient] = None

def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client

async def close_http_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


class FirewallaAPI:
    def __init__(self, api_key: str, msp_domain: str):
        self.api_key = api_key
        self.msp_domain = msp_domain.strip().rstrip("/")
        self._last_request_time = 0.0

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @property
    def _base(self) -> str:
        return f"https://{self.msp_domain}/v2"

    @property
    def _lists_url(self) -> str:
        return f"{self._base}/target-lists"  # correct Firewalla path

    async def _rate_limit(self):
        now = asyncio.get_running_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = asyncio.get_running_loop().time()

    async def _request(self, method: str, url: str, context: str = "", **kwargs) -> Optional[dict]:
        """
        Make an HTTP request with full diagnostic logging on every failure.
        context: human-readable description of what this call is trying to do.
        """
        await self._rate_limit()
        tag = f"[{context}] " if context else ""

        if not self.api_key:
            logger.error(f"{tag}Aborting request — FIREWALLA_API_KEY is not set in .env")
            return None
        if not self.msp_domain:
            logger.error(f"{tag}Aborting request — FIREWALLA_MSP_DOMAIN is not set in .env")
            return None

        logger.debug(f"{tag}{method} {url}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                client = _get_client()
                response = await client.request(method, url, headers=self._headers, **kwargs)

                logger.debug(f"{tag}Response: HTTP {response.status_code}")

                # ── Rate limited ──────────────────────────────────────
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", RETRY_BACKOFF_BASE ** attempt))
                    logger.warning(
                        f"{tag}Rate limited by Firewalla API (HTTP 429). "
                        f"Waiting {retry_after}s before retry {attempt}/{MAX_RETRIES}."
                    )
                    await asyncio.sleep(retry_after)
                    continue

                # ── Success / No content ──────────────────────────────
                if response.status_code == 204:
                    return {}

                # ── Client errors (4xx) ───────────────────────────────
                if 400 <= response.status_code < 500:
                    body = response.text[:500]
                    if response.status_code == 401:
                        logger.error(
                            f"{tag}HTTP 401 Unauthorized — your API key is wrong or expired. "
                            f"Check FIREWALLA_API_KEY in .env. Response: {body}"
                        )
                    elif response.status_code == 403:
                        logger.error(
                            f"{tag}HTTP 403 Forbidden — the API key doesn't have permission for this action. "
                            f"URL: {url}. Response: {body}"
                        )
                    elif response.status_code == 404:
                        logger.error(
                            f"{tag}HTTP 404 Not Found — the resource doesn't exist on Firewalla. "
                            f"URL: {url}. Response: {body}"
                        )
                    elif response.status_code == 409:
                        logger.warning(
                            f"{tag}HTTP 409 Conflict — resource already exists. URL: {url}. Response: {body}"
                        )
                    else:
                        logger.error(
                            f"{tag}HTTP {response.status_code} client error. "
                            f"URL: {url}. Response: {body}"
                        )
                    return None  # Don't retry client errors

                # ── Server errors (5xx) ───────────────────────────────
                if response.status_code >= 500:
                    body = response.text[:300]
                    logger.error(
                        f"{tag}HTTP {response.status_code} server error from Firewalla "
                        f"(attempt {attempt}/{MAX_RETRIES}). Response: {body}"
                    )
                    if attempt < MAX_RETRIES:
                        wait = RETRY_BACKOFF_BASE ** attempt
                        logger.info(f"{tag}Retrying in {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"{tag}All {MAX_RETRIES} attempts failed.")
                    return None

                return response.json()

            except httpx.ConnectError as e:
                logger.error(
                    f"{tag}Connection failed to {self.msp_domain} (attempt {attempt}/{MAX_RETRIES}). "
                    f"Check that FIREWALLA_MSP_DOMAIN is correct and the host is reachable. Error: {e}"
                )
            except httpx.TimeoutException:
                logger.warning(
                    f"{tag}Request timed out after 30s (attempt {attempt}/{MAX_RETRIES}). "
                    f"URL: {url}"
                )
            except httpx.TooManyRedirects as e:
                logger.error(f"{tag}Too many redirects — possible misconfigured domain. Error: {e}")
                return None
            except httpx.RequestError as e:
                logger.error(
                    f"{tag}Network error (attempt {attempt}/{MAX_RETRIES}): {type(e).__name__}: {e}"
                )

            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.info(f"{tag}Retrying in {wait}s...")
                await asyncio.sleep(wait)

        logger.error(f"{tag}Giving up after {MAX_RETRIES} failed attempts. URL: {url}")
        return None

    # ── Public API methods ─────────────────────────────────────────────────────

    async def check_health(self) -> dict:
        """Detailed health check — returns status + a diagnostic message."""
        if not self.api_key:
            return {"status": "unconfigured", "message": "FIREWALLA_API_KEY is not set"}
        if not self.msp_domain:
            return {"status": "unconfigured", "message": "FIREWALLA_MSP_DOMAIN is not set"}

        url = self._lists_url
        logger.info(f"Health check: GET {url}")
        try:
            response = await _get_client().get(url, headers=self._headers)
            if response.status_code == 200:
                data = response.json()
                count = len(data) if isinstance(data, list) else len(data.get("results", []))
                return {"status": "ok", "message": f"Connected — {count} target list(s) found"}
            elif response.status_code == 401:
                return {"status": "error", "message": "HTTP 401 — API key is invalid or expired"}
            elif response.status_code == 403:
                return {"status": "error", "message": "HTTP 403 — API key lacks permission"}
            elif response.status_code == 404:
                return {"status": "error", "message": f"HTTP 404 — domain '{self.msp_domain}' not found. Check MSP domain."}
            else:
                return {"status": "error", "message": f"HTTP {response.status_code} — {response.text[:200]}"}
        except httpx.ConnectError:
            return {"status": "error", "message": f"Cannot connect to '{self.msp_domain}' — is the domain correct?"}
        except httpx.TimeoutException:
            return {"status": "error", "message": f"Connection timed out to '{self.msp_domain}'"}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    async def get_all_lists(self) -> list:
        result = await self._request("GET", self._lists_url, context="get_all_lists")
        if result is None:
            return []
        return result if isinstance(result, list) else result.get("results", [])

    async def get_list(self, list_id: str) -> Optional[dict]:
        return await self._request("GET", f"{self._lists_url}/{list_id}", context=f"get_list:{list_id}")

    async def create_list(self, name: str, entries: list[str], list_type: str = "domain", notes: str = "") -> Optional[str]:
        logger.info(f"Creating Firewalla list '{name}' with {len(entries)} entries (type={list_type})")
        payload: dict = {"name": name, "type": list_type, "targets": entries}
        if notes:
            payload["notes"] = notes
        result = await self._request("POST", self._lists_url, context=f"create_list:{name}", json=payload)
        if result:
            list_id = result.get("id") or result.get("target_list_id") or result.get("_id")
            if list_id:
                logger.info(f"Created list '{name}' → ID: {list_id}")
            else:
                logger.warning(f"List created but no ID returned. Full response: {result}")
            return list_id
        logger.error(f"Failed to create list '{name}' — see errors above for details")
        return None

    async def update_list(self, list_id: str, name: str, entries: list[str], list_type: str = "domain", notes: str = "") -> bool:
        """
        Firewalla update API: PATCH /v2/target-lists/{id}
        Body contains name + targets. The 'type' field is immutable after creation.
        Reference: https://docs.firewalla.net/api-reference/target-lists/
        """
        logger.info(f"Updating Firewalla list '{name}' (ID: {list_id}) with {len(entries)} entries")
        payload: dict = {"name": name, "targets": entries}
        if notes:
            payload["notes"] = notes
        result = await self._request("PATCH", f"{self._lists_url}/{list_id}", context=f"update_list:{name}", json=payload)
        if result is not None:
            logger.info(f"Successfully patched list '{name}' (ID: {list_id})")
            return True
        logger.error(f"Failed to patch list '{name}' (ID: {list_id}) — see errors above")
        return False

    async def delete_list(self, list_id: str) -> bool:
        logger.info(f"Deleting Firewalla list ID: {list_id}")
        result = await self._request("DELETE", f"{self._lists_url}/{list_id}", context=f"delete_list:{list_id}")
        if result is not None:
            logger.info(f"Deleted list ID: {list_id}")
            return True
        logger.error(f"Failed to delete list ID: {list_id}")
        return False
