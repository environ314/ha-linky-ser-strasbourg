"""Client aiohttp MaConsoLinky SER (OAuth PKCE + export courbe de charge).

Flux validé par capture réseau (19/11/2026) puis replay Python pur :
1. POST /auth/externe/authentification (username, password, client_id)
2. GET  /auth/authorize-internet (PKCE S256) -> 303 ?code=
3. POST /auth/tokenUtilisateurInternet -> access_token (Bearer)
4. REST : PASC puis POST historiqueDeMesure/exporter (CSV latin-1).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime
from typing import Any

import aiohttp

from .const import API_URL, CLIENT_ID, REDIRECT_URI, USER_AGENT

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=60)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class LinkySerClientError(Exception):
    """Erreur du client MaConsoLinky."""


class LinkySerClient:
    """Client asynchrone MaConsoLinky SER."""

    def __init__(self, session: aiohttp.ClientSession,
                 email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self.pasc_id: str | None = None

    async def _request(self, method: str, url: str,
                       **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        """Retourne (status, headers, body) — body lu DANS le contexte."""
        kwargs.setdefault("timeout", TIMEOUT)
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", USER_AGENT)
        if self._token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._token}"
        async with self._session.request(method, url, headers=headers,
                                         **kwargs) as resp:
            body = await resp.read()
            return resp.status, dict(resp.headers), body

    @staticmethod
    def _json(body: bytes) -> Any:
        return json.loads(body.decode("utf-8"))

    async def login(self) -> str:
        """OAuth complet : session -> code -> access_token -> PASC."""
        # 1. authentification (cookie de session)
        status, _, body = await self._request(
            "POST", API_URL + "/auth/externe/authentification",
            data={"username": self._email, "password": self._password,
                  "client_id": CLIENT_ID},
            allow_redirects=False)
        if status != 200:
            raise LinkySerClientError(f"authentification HTTP {status}")
        data: dict = self._json(body)
        if data.get("code") != "0":
            raise LinkySerClientError(
                f"login refusé: {data.get('libelle', data)}")
        # 2. authorize avec PKCE généré ici
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        status, headers, _ = await self._request(
            "GET", API_URL + "/auth/authorize-internet",
            params={"redirect_uri": REDIRECT_URI, "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "client_id": CLIENT_ID},
            allow_redirects=False)
        code = None
        for _ in range(3):
            if status in (301, 302, 303, 307):
                loc = headers.get("Location", "")
                match = re.search(r"[?&]code=([^&]+)", loc)
                if match:
                    code = match.group(1)
                    break
                status, headers, _ = await self._request(
                    "GET", loc, allow_redirects=False)
                continue
            break
        if code is None:
            raise LinkySerClientError(f"pas de code OAuth (HTTP {status})")
        # 3. échange du code
        status, _, body = await self._request(
            "POST", API_URL + "/auth/tokenUtilisateurInternet",
            data={"client_id": CLIENT_ID, "code": code,
                  "redirect_uri": REDIRECT_URI,
                  "grant_type": "authorization_code",
                  "code_verifier": verifier})
        if status != 200:
            raise LinkySerClientError(f"token HTTP {status}")
        self._token = self._json(body)["access_token"]
        # 4. point d'accès (PASC)
        status, _, body = await self._request(
            "GET", API_URL + "/rest/produits/pointsAccesServicesClient",
            params={"expand": "pointDeService(espaceDeLivraison)"})
        if status != 200:
            raise LinkySerClientError(f"PASC HTTP {status}")
        pascs = self._json(body)
        if not pascs:
            raise LinkySerClientError("aucun point d'accès sur le compte")
        self.pasc_id = pascs[0]["id"]
        assert self.pasc_id is not None
        return self.pasc_id

    async def export_csv(self, date_debut: datetime,
                         date_fin: datetime) -> str:
        """CSV courbe de charge sur la plage (datetimes UTC).

        Retour : texte latin-1 (colonnes Date;Heure;Calendrier;Poste;
        Puissance (W), pas 30 min).
        """
        if self._token is None or self.pasc_id is None:
            await self.login()
        payload = {
            "typeObjet": "DonneesHistoriqueMesureRepresentation",
            "dateDebut": date_debut.isoformat().replace("+00:00", "Z"),
            "dateFin": date_fin.isoformat().replace("+00:00", "Z"),
            "pointAccesServicesClient": {
                "typeObjet": "produit.PointAccesServicesClient",
                "id": self.pasc_id,
            },
            "groupesDeGrandeurs": [
                {"typeObjet": "produit.GroupeGrandeur",
                 "codeGroupeGrandeur": {"code": "0"}}],
        }
        status, _, body = await self._request(
            "POST", API_URL + "/rest/interfaces/aelgrd/historiqueDeMesure/exporter",
            json=payload)
        if status in (401, 403):
            # token expiré (le portail le révoque au bout de ~24 h) :
            # invalide tout et re-authentifie une fois avant de réessayer
            _LOGGER.warning("Linky SER: token expiré (HTTP %s), re-login", status)
            self._token = None
            self.pasc_id = None
            if self._session.cookie_jar is not None:
                self._session.cookie_jar.clear()
            await self.login()
            return await self.export_csv(date_debut, date_fin)
        if status != 200:
            raise LinkySerClientError(f"export HTTP {status}")
        return body.decode("latin-1")
