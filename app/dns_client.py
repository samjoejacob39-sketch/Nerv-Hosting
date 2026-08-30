"""Cloudflare API v4 DNS integration client."""

from __future__ import annotations

import logging
from typing import Any

import requests
from flask import current_app

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareDNSError(Exception):
    """Raised when the Cloudflare DNS API returns an error or fails."""

    def __init__(self, message: str, status_code: int = 502, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class CloudflareDNSClient:
    """Client for creating and managing Cloudflare DNS records."""

    def __init__(
        self,
        api_token: str | None = None,
        zone_id: str | None = None,
        domain: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_token = api_token or current_app.config.get("CLOUDFLARE_API_TOKEN", "")
        self.zone_id = zone_id or current_app.config.get("CLOUDFLARE_ZONE_ID", "")
        self.domain = (domain or current_app.config.get("CLOUDFLARE_DOMAIN", "yourdomain.com")).lstrip(".")
        self.session = session or requests.Session()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{CLOUDFLARE_API_BASE}/{path.lstrip('/')}"

    def create_subdomain_record(
        self,
        subdomain: str,
        target_ip: str,
        port: int | None = None,
        record_type: str = "A",
        proxied: bool = False,
    ) -> dict[str, Any]:
        """Create a DNS A or SRV record for the claimed subdomain."""
        subdomain_clean = subdomain.strip().lower()
        full_name = f"{subdomain_clean}.{self.domain}" if self.domain else subdomain_clean

        if record_type == "SRV" and port is not None:
            payload = {
                "type": "SRV",
                "data": {
                    "service": "_minecraft",
                    "proto": "_tcp",
                    "name": subdomain_clean,
                    "priority": 1,
                    "weight": 1,
                    "port": int(port),
                    "target": full_name,
                },
                "ttl": 120,
            }
        else:
            payload = {
                "type": "A",
                "name": full_name,
                "content": target_ip,
                "ttl": 120,
                "proxied": proxied,
            }

        url = self._url(f"zones/{self.zone_id}/dns_records")
        try:
            res = self.session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.error("Cloudflare DNS network failure: %s", exc)
            raise CloudflareDNSError(f"Network error communicating with Cloudflare DNS: {exc}") from exc

        if not res.ok:
            data = res.json() if res.content else {}
            errors = data.get("errors", [])
            msg = errors[0].get("message") if errors else f"HTTP {res.status_code}"
            logger.warning("Cloudflare DNS record creation failed (%s): %s", res.status_code, msg)
            raise CloudflareDNSError(f"Cloudflare DNS Error: {msg}", status_code=res.status_code, errors=errors)

        body = res.json()
        result = body.get("result", {})
        return {
            "id": result.get("id"),
            "name": result.get("name", full_name),
            "content": result.get("content", target_ip),
            "type": result.get("type", record_type),
            "full_domain": full_name,
        }

    def delete_subdomain_record(self, record_id: str) -> bool:
        """Delete an existing DNS record from Cloudflare."""
        if not record_id:
            return False

        url = self._url(f"zones/{self.zone_id}/dns_records/{record_id}")
        try:
            res = self.session.delete(
                url,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.error("Cloudflare DNS deletion network failure: %s", exc)
            raise CloudflareDNSError(f"Network error deleting Cloudflare DNS record: {exc}") from exc

        if res.status_code == 404:
            return True  # Already deleted

        if not res.ok:
            data = res.json() if res.content else {}
            errors = data.get("errors", [])
            msg = errors[0].get("message") if errors else f"HTTP {res.status_code}"
            logger.warning("Cloudflare DNS record deletion failed (%s): %s", res.status_code, msg)
            raise CloudflareDNSError(f"Cloudflare DNS Error: {msg}", status_code=res.status_code, errors=errors)

        return True


def get_dns_client(**kwargs: Any) -> CloudflareDNSClient:
    """Factory to obtain a Cloudflare DNS client instance."""
    return CloudflareDNSClient(**kwargs)
