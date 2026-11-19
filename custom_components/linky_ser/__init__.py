"""Linky SER : synchronisation courbe de charge SER -> statistiques HA.

Fonctionnement (indestructible) :
- Tick quotidien 08h00 (Europe/Paris) + service linky_ser.sync_now +
  bouton linky_ser_sync.
- Fenêtre glissante de 5 jours : le CSV SER est ré-exporté puis les heures
  POSTÉRIEURES au dernier point en base sont injectées/écrasées (UPSERT
  sur `start` identique) -> rattrape les retards de publication SER.
- Continuité de la somme cumulée : reprise exacte du dernier `sum`
  présent dans le recorder (get_last_statistics), jamais de trou ni de
  remise à zéro.
- Deux statistiques externes : ser:linky_consumption (kWh) et
  ser:linky_cost (EUR à price €/kWh).
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: F401
from homeassistant.util import dt as dt_util
import zoneinfo

from .api import LinkySerClient, LinkySerClientError
from .const import (CONF_EMAIL, CONF_PASSWORD, CONF_PRICE, DEFAULT_PRICE,
                    DOMAIN, LOOKBACK_DAYS, STAT_CONSUMPTION, STAT_COST,
                    STAT_NAME_CONSUMPTION, STAT_NAME_COST, STAT_SOURCE)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON]
TZ_PARIS = zoneinfo.ZoneInfo("Europe/Paris")

STATS_CONSUMPTION_META = {
    "statistic_id": STAT_CONSUMPTION,
    "name": STAT_NAME_CONSUMPTION,
    "source": STAT_SOURCE,
    "unit_of_measurement": "kWh",
    "unit_class": "energy",
    "mean_type": "none",
}
STATS_COST_META = {
    "statistic_id": STAT_COST,
    "name": STAT_NAME_COST,
    "source": STAT_SOURCE,
    "unit_of_measurement": "EUR",
    "unit_class": None,
    "mean_type": "none",
}


async def async_setup(hass: HomeAssistant, config: Any) -> bool:
    """Setup du domaine (service manuel)."""
    _register_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant,
                            entry: ConfigEntry) -> bool:
    """Setup d'une entrée : planning quotidien + service + bouton."""
    hass.data.setdefault(DOMAIN, {})
    hub = LinkySerHub(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # tick quotidien 08h00 heure locale (timezone HA)
    entry.async_on_unload(async_track_daily(hub))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.async_close))
    return True


def _register_service(hass: HomeAssistant) -> None:
    async def _sync(service: ServiceCall) -> None:
        for hub in hass.data.get(DOMAIN, {}).values():
            await hub.sync_now()
    if not hass.services.has_service(DOMAIN, "sync_now"):
        hass.services.async_register(DOMAIN, "sync_now", _sync)


def async_track_daily(hub: LinkySerHub) -> Any:
    """Planifie la synchro tous les jours à 08h00 heure locale."""
    from homeassistant.helpers.event import async_track_time_change
    return async_track_time_change(
        hub.hass, hub._scheduled_sync, hour=8, minute=0, second=0)


def parse_hourly(csv_text: str) -> dict[datetime, float]:
    """Agrège le CSV (W, pas 30 min) en {heure locale Paris: kWh}.

    Heures COMPLÈTES uniquement (2 tranches de 30 min).
    """
    tranches: dict[datetime, float] = {}
    reader = csv.reader(io.StringIO(csv_text), delimiter=";")
    next(reader, None)  # en-tête
    for row in reader:
        if len(row) < 5 or not row[0] or row[0] == "Date":
            continue
        try:
            watts = float(row[4].replace(" ", "").replace(",", "."))
            dt = datetime.strptime(row[0] + " " + row[1],
                                   "%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
        tranches[dt] = watts
    by_hour: dict[datetime, list[float]] = defaultdict(list)
    for dt, watts in tranches.items():
        by_hour[dt.replace(minute=0, second=0)].append(watts)
    hourly: dict[datetime, float] = {}
    for hour, values in by_hour.items():
        if len(values) == 2:  # heure complète uniquement
            hourly[hour] = round(sum(values) * 0.5 / 1000, 5)
    return hourly


class LinkySerHub:
    """Hub : client + cycle de synchronisation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._session: aiohttp.ClientSession | None = None
        self._client: LinkySerClient | None = None
        self._lock = asyncio.Lock()

    @property
    def price(self) -> float:
        return float(self.entry.options.get(
            CONF_PRICE, self.entry.data.get(CONF_PRICE, DEFAULT_PRICE)))

    def _get_client(self) -> LinkySerClient:
        if self._client is None:
            self._session = aiohttp.ClientSession()
            self._client = LinkySerClient(
                self._session, self.entry.data[CONF_EMAIL],
                self.entry.data[CONF_PASSWORD])
        return self._client

    async def async_close(self, *_: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._client = None

    @callback
    def _scheduled_sync(self, now: datetime) -> None:
        """Tick 08h00 : planifie la synchro sans bloquer."""
        self.hass.async_create_task(self.sync_now())

    async def _last_state(self) -> dict[str, tuple[datetime, float]]:
        """Dernier point en base par statistique : {id: (start_utc, sum)}."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics)
        out: dict[str, tuple[datetime, float]] = {}
        instance = get_instance(self.hass)
        for stat_id in (STAT_CONSUMPTION, STAT_COST):
            rows = await instance.async_add_executor_job(
                partial(get_last_statistics, self.hass, 1, stat_id,
                        True, {"sum"}))
            for row in rows.get(stat_id, []):
                out[stat_id] = (
                    dt_util.utc_from_timestamp(row["start"]),
                    float(row["sum"] or 0.0))
        return out

    async def sync_now(self) -> dict[str, Any]:
        """Cycle complet : OAuth -> export 5 j -> UPSERT stats externes."""
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics)
        async with self._lock:
            last = await self._last_state()
            start_ref = min((s for s, _ in last.values()), default=None)
            now = dt_util.now()
            window_start = now - timedelta(days=LOOKBACK_DAYS)
            if start_ref is not None:
                window_start = min(window_start,
                                   start_ref + timedelta(hours=1))
            client = self._get_client()
            csv_text = await client.export_csv(
                dt_util.as_utc(window_start), dt_util.as_utc(now))
            hourly = parse_hourly(csv_text)
            # uniquement les heures postérieures au dernier point connu
            if start_ref is not None:
                ref = start_ref
                hourly = {h: kwh for h, kwh in hourly.items()
                          if h.replace(tzinfo=TZ_PARIS) > ref}
            # continuité des cumuls : reprise des derniers sum en base
            cum_kwh = last.get(STAT_CONSUMPTION, (None, 0.0))[1]
            cum_eur = last.get(STAT_COST, (None, 0.0))[1]
            kwh_stats, eur_stats = [], []
            for hour in sorted(hourly):
                kwh = hourly[hour]
                cum_kwh = round(cum_kwh + kwh, 5)
                cum_eur = round(cum_eur + kwh * self.price, 5)
                iso = hour.replace(tzinfo=TZ_PARIS)
                kwh_stats.append({"start": iso, "sum": cum_kwh,
                                  "state": cum_kwh})
                eur_stats.append({"start": iso, "sum": cum_eur,
                                  "state": cum_eur})
            result = {"points": len(kwh_stats),
                      "kwh_total": round(cum_kwh, 3),
                      "cost_total": round(cum_eur, 3),
                      "window": f"{window_start:%d/%m} -> {now:%d/%m}"}
            if kwh_stats:
                async_add_external_statistics(
                    self.hass, dict(STATS_CONSUMPTION_META), kwh_stats)
                async_add_external_statistics(
                    self.hass, dict(STATS_COST_META), eur_stats)
            self.hass.bus.async_fire(f"{DOMAIN}_synced", result)
            _LOGGER.info("Linky SER sync: %s", result)
            return result


async def async_unload_entry(hass: HomeAssistant,
                             entry: ConfigEntry) -> bool:
    hub: LinkySerHub | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None)
    if hub is not None:
        await hub.async_close()
    return await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS)
