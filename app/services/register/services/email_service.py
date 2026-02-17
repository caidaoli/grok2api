"""Email service for temporary inbox creation."""
from __future__ import annotations

import os
import random
import string
import json
from typing import Tuple, Optional

import requests
import urllib3

from app.core.config import get_config


class EmailService:
    """Email service wrapper."""

    _DOH_ENDPOINTS = (
        "https://1.1.1.1/dns-query",
        "https://1.0.0.1/dns-query",
    )
    _DNS_ERROR_MARKERS = (
        "nameresolutionerror",
        "failed to resolve",
        "temporary failure in name resolution",
        "no address associated with hostname",
    )

    def __init__(
        self,
        worker_domain: Optional[str] = None,
        email_domain: Optional[str] = None,
        admin_password: Optional[str] = None,
    ) -> None:
        self.worker_domain = (
            (worker_domain or get_config("register.worker_domain", "") or os.getenv("WORKER_DOMAIN", "")).strip()
        )
        self.email_domain = (
            (email_domain or get_config("register.email_domain", "") or os.getenv("EMAIL_DOMAIN", "")).strip()
        )
        self.admin_password = (
            (admin_password or get_config("register.admin_password", "") or os.getenv("ADMIN_PASSWORD", "")).strip()
        )

        if not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError(
                "Missing required email settings: register.worker_domain, register.email_domain, "
                "register.admin_password"
            )

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def _is_dns_resolution_error(self, exc: requests.exceptions.RequestException) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in self._DNS_ERROR_MARKERS)

    def _resolve_ipv4_via_doh(self, host: str) -> list[str]:
        for endpoint in self._DOH_ENDPOINTS:
            try:
                res = requests.get(
                    endpoint,
                    params={"name": host, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                    timeout=5,
                )
                if res.status_code != 200:
                    continue
                data = res.json()
                answers = data.get("Answer") if isinstance(data, dict) else None
                if not isinstance(answers, list):
                    continue
                ips: list[str] = []
                for item in answers:
                    if not isinstance(item, dict):
                        continue
                    if int(item.get("type", 0) or 0) != 1:
                        continue
                    ip = str(item.get("data", "") or "").strip()
                    if ip and ip not in ips:
                        ips.append(ip)
                if ips:
                    return ips
            except Exception:
                continue
        return []

    def _create_email_via_doh(self, path: str, payload: dict, headers: dict) -> Tuple[Optional[str], Optional[str]]:
        ips = self._resolve_ipv4_via_doh(self.worker_domain)
        if not ips:
            print(f"[-] Email create DNS fallback failed: no A record from DoH for {self.worker_domain}")
            return None, None

        body = json.dumps(payload).encode("utf-8")
        req_headers = dict(headers)
        req_headers["Host"] = self.worker_domain

        for ip in ips:
            pool: urllib3.HTTPSConnectionPool | None = None
            try:
                pool = urllib3.HTTPSConnectionPool(
                    host=ip,
                    port=443,
                    assert_hostname=self.worker_domain,
                    server_hostname=self.worker_domain,
                    cert_reqs="CERT_REQUIRED",
                    retries=False,
                    timeout=urllib3.util.Timeout(connect=10, read=10),
                )
                res = pool.request(
                    "POST",
                    path,
                    body=body,
                    headers=req_headers,
                    preload_content=True,
                )
                text = res.data.decode("utf-8", errors="replace")
                if res.status == 200:
                    data = json.loads(text)
                    return data.get("jwt"), data.get("address")
                print(f"[-] Email create failed via DoH({ip}): {res.status} - {text}")
                return None, None
            except Exception as exc:
                print(f"[-] Email create DoH fallback error ({ip}): {exc}")
            finally:
                if pool is not None:
                    pool.close()
        return None, None

    def create_email(self) -> Tuple[Optional[str], Optional[str]]:
        """Create a temporary mailbox. Returns (jwt, address)."""
        url = f"https://{self.worker_domain}/admin/new_address"
        random_name = self._generate_random_name()
        payload = {
            "enablePrefix": True,
            "name": random_name,
            "domain": self.email_domain,
        }
        headers = {
            "x-admin-auth": self.admin_password,
            "Content-Type": "application/json",
        }
        try:
            res = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] Email create failed: {res.status_code} - {res.text}")
            return None, None
        except requests.exceptions.RequestException as exc:
            if self._is_dns_resolution_error(exc):
                return self._create_email_via_doh("/admin/new_address", payload, headers)
            print(f"[-] Email create error ({url}): {exc}")
            return None, None
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"[-] Email create error ({url}): {exc}")
            return None, None
        return None, None

    def fetch_first_email(self, jwt: str) -> Optional[str]:
        """Fetch the first email content for the mailbox."""
        try:
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"Email fetch failed: {exc}")
            return None
