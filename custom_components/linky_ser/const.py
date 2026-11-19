"""Constantes Linky SER."""
from __future__ import annotations

DOMAIN = "linky_ser"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_PRICE = "price"  # €/kWh
CONF_INITIAL_HISTORY = "initial_history_days"  # backfill au 1er lancement

# Statistiques externes (aucune entité -> aucune pollution du recorder)
STAT_CONSUMPTION = "ser:linky_consumption"
STAT_COST = "ser:linky_cost"
STAT_SOURCE = "ser"
STAT_NAME_CONSUMPTION = "Linky SER Consommation"
STAT_NAME_COST = "Linky SER Coût"

# Portail MaConsoLinky (Strasbourg Électricité Réseaux)
BASE_URL = "https://maconsolinky.strasbourg-electricite-reseaux.fr"
API_URL = BASE_URL + "/efluidES_PORTAIL"
CLIENT_ID = "aelGRD"
REDIRECT_URI = BASE_URL + "/autorisation-callback.html"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

# Fenêtre de rattrapage quotidien (jours) : couvre les retards SER
LOOKBACK_DAYS = 7

# Historique étendu (backfill linky_ser.import_history)
MIN_HISTORY_DAYS = 7
MAX_HISTORY_DAYS = 1095
DEFAULT_HISTORY_DAYS = 365
CHUNK_DAYS = 30  # découpe des exports longs pour ménager le portail SER

DEFAULT_PRICE = 0.1348
