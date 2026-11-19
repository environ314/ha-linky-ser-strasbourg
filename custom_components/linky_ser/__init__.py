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
from .const import (CONF_EMAIL, CONF_INITIAL_HISTORY, CONF_PASSWORD, CONF_PRICE,
                    DEFAULT_HISTORY_DAYS, DEFAULT_PRICE, DOMAIN, LOOKBACK_DAYS,
                    MAX_HISTORY_DAYS, MIN_HISTORY_DAYS, STAT_CONSUMPTION,
                    STAT_COST, STAT_NAME_CONSUMPTION, STAT_NAME_COST,
                    STAT_SOURCE)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON]
TZ_PARIS = zoneinfo.ZoneInfo("Europe/Paris")
# mean_type DOIT être le StatisticMeanType (int), jamais la chaîne "none"
# (sinon HA invalide les métadonnées et masque les statistiques)
from homeassistant.components.recorder.models import StatisticMeanType  # noqa: E402

STATS_CONSUMPTION_META = {
    "statistic_id": STAT_CONSUMPTION,
    "name": STAT_NAME_CONSUMPTION,
    "source": STAT_SOURCE,
    "unit_of_measurement": "kWh",
    "unit_class": "energy",
    "mean_type": StatisticMeanType.NONE,
    "has_mean": False,
    "has_sum": True,
}
STATS_COST_META = {
    "statistic_id": STAT_COST,
    "name": STAT_NAME_COST,
    "source": STAT_SOURCE,
    "unit_of_measurement": "EUR",
    "unit_class": None,
    "mean_type": StatisticMeanType.NONE,
    "has_mean": False,
    "has_sum": True,
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
    # premier lancement : si la base est vide, backfill d'historique
    entry.async_create_task(hass, hub.backfill_if_empty())
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

    async def _import_history(service: ServiceCall) -> None:
        days = int(service.data.get(
            "days", DEFAULT_HISTORY_DAYS))
        for hub in hass.data.get(DOMAIN, {}).values():
            await hub.backfill_history(days)
    if not hass.services.has_service(DOMAIN, "import_history"):
        hass.services.async_register(DOMAIN, "import_history",
                                     _import_history)


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
        """Cycle complet : OAuth -> export 5 j -> UPSERT stats externes.

        Idempotent, verrouillé, et robuste : une indisponibilité du portail
        SER à 08h00 est journalisée sans casser la boucle HA ; le prochain
        tick (ou le bouton) relancera le cycle.
        """
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics)
        async with self._lock:
            try:
                return await self._sync_locked(async_add_external_statistics)
            except LinkySerClientError as err:
                _LOGGER.error("Linky SER: échec de synchronisation "
                              "(portail indisponible ?): %s", err)
            except aiohttp.ClientError as err:
                _LOGGER.error("Linky SER: erreur réseau pendant la "
                              "synchronisation: %s", err)
            return {"points": 0, "error": True}

    async def _sync_locked(self, async_add_external_statistics) -> dict[str, Any]:
        last = await self._last_state()
        last_conso_ts, conso_sum = last.get(
            STAT_CONSUMPTION, (None, 0.0))
        last_cost_ts, cost_sum = last.get(STAT_COST, (None, 0.0))
        now = dt_util.now()
        window_start = now - timedelta(days=LOOKBACK_DAYS)
        if last_conso_ts is not None:
            window_start = min(window_start,
                               last_conso_ts + timedelta(hours=1))
        client = self._get_client()
        csv_text = await client.export_csv(
            dt_util.as_utc(window_start), dt_util.as_utc(now))
        hourly = parse_hourly(csv_text)
        # découplage : chaque statistique ne reçoit que les heures
        # POSTÉRIEURES à SON dernier point — si l'une a pris de l'avance
        # sur l'autre, son cumul n'est pas décalé
        conso_rows = self._rows(hourly, last_conso_ts)
        cost_rows = self._rows(hourly, last_cost_ts)
        price = self.price
        kwh_stats, cum_kwh = [], conso_sum
        for hour in sorted(conso_rows):
            cum_kwh = round(cum_kwh + conso_rows[hour], 5)
            kwh_stats.append({"start": hour.replace(tzinfo=TZ_PARIS),
                              "sum": cum_kwh, "state": cum_kwh})
        eur_stats, cum_eur = [], cost_sum
        for hour in sorted(cost_rows):
            cum_eur = round(cum_eur + cost_rows[hour] * price, 5)
            eur_stats.append({"start": hour.replace(tzinfo=TZ_PARIS),
                              "sum": cum_eur, "state": cum_eur})
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

    @staticmethod
    def _rows(hourly: dict[datetime, float],
              last_ts: datetime | None) -> dict[datetime, float]:
        """Heures postérieures au dernier point de CETTE statistique."""
        if last_ts is None:
            return hourly
        return {h: kwh for h, kwh in hourly.items()
                if h.replace(tzinfo=TZ_PARIS) > last_ts}

    async def backfill_if_empty(self) -> None:
        """Premier lancement : backfill initial si la base est vide."""
        if await self._last_state():
            _LOGGER.debug("Linky SER: stats existantes, pas de backfill")
            return
        days = int(self.entry.options.get(
            CONF_INITIAL_HISTORY,
            self.entry.data.get(CONF_INITIAL_HISTORY,
                                DEFAULT_HISTORY_DAYS)))
        _LOGGER.info("Linky SER: base vide -> backfill initial (%s j)", days)
        await self.backfill_history(days)

    async def backfill_history(self, days: int) -> dict[str, Any]:
        """Import d'historique étendu (tranches de 30 j, UPSERT global).

        Les points sont fusionnés par timestamp croissant, le cumul est
        recalculé depuis le dernier `sum` connu AVANT la fenêtre (ancrage
        en base -> continuité parfaite ; 0.0 si base vierge), puis tout
        est réimporté : les timestamps existants sont écrasés par des
        sommes cohérentes.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics, statistics_during_period)
        days = max(MIN_HISTORY_DAYS, min(MAX_HISTORY_DAYS, int(days)))
        async with self._lock:
            try:
                return await self._backfill_locked(
                    days, get_instance, statistics_during_period,
                    async_add_external_statistics)
            except LinkySerClientError as err:
                _LOGGER.error("Linky SER: backfill échoué (portail ?): %s",
                              err)
            except aiohttp.ClientError as err:
                _LOGGER.error("Linky SER: backfill: erreur réseau: %s", err)
            return {"points": 0, "error": True}

    async def _backfill_locked(self, days, get_instance,
                               statistics_during_period,
                               async_add_external_statistics):
        now = dt_util.now()
        window_start = now - timedelta(days=days)
        client = self._get_client()
        hourly: dict[datetime, float] = {}
        empty_streak = 0
        chunk_no = 0
        # tranches du plus récent au plus ancien, 30 j max, pause 0,5 s
        try:
            async for csv_text in client.export_csv_chunked(
                    dt_util.as_utc(window_start), dt_util.as_utc(now)):
                chunk_no += 1
                chunk = parse_hourly(csv_text)
                if chunk:
                    empty_streak = 0
                    # récent d'abord : le premier vu gagne (setdefault)
                    for hour, kwh in chunk.items():
                        hourly.setdefault(hour, kwh)
                else:
                    empty_streak += 1
                    _LOGGER.warning(
                        "Linky SER backfill: tranche %s vide", chunk_no)
                    if empty_streak >= 2:
                        _LOGGER.warning(
                            "Linky SER: début de l'historique atteint, "
                            "arrêt de la remontée (2 tranches vides)")
                        break
        except LinkySerClientError as err:
            # erreur isolée sur une tranche : on importe ce qu'on a
            _LOGGER.warning("Linky SER backfill: erreur sur tranche "
                            "%s: %s", chunk_no + 1, err)
        if not hourly:
            return {"points": 0, "error": True,
                    "window": f"{window_start:%d/%m} -> {now:%d/%m}"}
        # ancrage : dernier sum en base AVANT la fenêtre (continuité) ;
        # base vierge -> 0.0 (courbe recalculée depuis le 1er point)
        first = min(hourly)
        instance = get_instance(self.hass)
        anchor: dict[str, float] = {}
        for stat_id in (STAT_CONSUMPTION, STAT_COST):
            rows = await instance.async_add_executor_job(
                partial(statistics_during_period, self.hass,
                        dt_util.as_utc(datetime(2020, 1, 1)),
                        dt_util.as_utc(first - timedelta(microseconds=1)),
                        {stat_id}, "hour", None, {"sum"}))
            lst = rows.get(stat_id) or []
            anchor[stat_id] = (float(max(lst, key=lambda r: r["start"])["sum"])
                if lst else 0.0)
        price = self.price
        cum_kwh, cum_eur = anchor[STAT_CONSUMPTION], anchor[STAT_COST]
        kwh_stats, eur_stats = [], []
        for hour in sorted(hourly):
            kwh = hourly[hour]
            cum_kwh = round(cum_kwh + kwh, 5)
            cum_eur = round(cum_eur + kwh * price, 5)
            iso = hour.replace(tzinfo=TZ_PARIS)
            kwh_stats.append({"start": iso, "sum": cum_kwh,
                              "state": cum_kwh})
            eur_stats.append({"start": iso, "sum": cum_eur,
                              "state": cum_eur})
        result = {"points": len(kwh_stats),
                  "kwh_total": round(cum_kwh, 3),
                  "cost_total": round(cum_eur, 3),
                  "window": f"{window_start:%d/%m} -> {now:%d/%m}",
                  "chunks": chunk_no}
        async_add_external_statistics(
            self.hass, dict(STATS_CONSUMPTION_META), kwh_stats)
        async_add_external_statistics(
            self.hass, dict(STATS_COST_META), eur_stats)
        self.hass.bus.async_fire(f"{DOMAIN}_history_imported", result)
        _LOGGER.info("Linky SER backfill: %s", result)
        return result


async def async_unload_entry(hass: HomeAssistant,
                             entry: ConfigEntry) -> bool:
    hub: LinkySerHub | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None)
    if hub is not None:
        await hub.async_close()
    return await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS)
