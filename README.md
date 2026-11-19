# Linky SER Strasbourg — courbe de charge Strasbourg Électricité Réseaux pour Home Assistant

Intégration [Home Assistant](https://www.home-assistant.io) pour les compteurs **Linky du réseau de Strasbourg Électricité Réseaux (SER)** (Strasbourg Eurométropole — **pas** Enedis : le portail est [MaConsoLinky](https://maconsolinky.strasbourg-electricite-reseaux.fr)).

Elle récupère la **courbe de charge** (puissance moyenne par pas de 30 min) et l'injecte dans les **Long-Term Statistics** de Home Assistant, exactement comme le font les intégrations officielles type `ha-linky` / `gazpar` : pas de capteur temps réel faux, pas d'écriture directe en base — uniquement l'API `recorder` (`async_add_external_statistics`).

## Fonctionnalités

- **OAuth PKCE** complet en Python pur (`aiohttp`) contre le portail MaConsoLinky — aucun navigateur ni scraper.
- **Deux statistiques externes** :
  - `ser:linky_consumption` — consommation horaire en **kWh** (somme cumulée) ;
  - `ser:linky_cost` — coût cumulé en **€** au tarif que tu configures.
- **Synchro quotidienne à 08h00** (heure locale de ton HA).
- **Fenêtre glissante de 5 jours** : chaque cycle ré-exporte les 5 derniers jours et écrase/complète les heures manquantes — les retards de publication de 24-48 h (fréquents le week-end) sont rattrapés automatiquement. L'écriture est un *UPSERT* sur l'horodatage : relancer ne crée jamais de doublon.
- **Continuité garantie de la somme cumulée** : avant chaque injection, le dernier `sum` présent dans le recorder est relu (`get_last_statistics`) et la courbe continue sans trou ni remise à zéro, même si tu viens d'une autre solution (CSV, script…).
- **Synchronisation manuelle** : bouton `button.linky_ser_sync` **et** service `linky_ser.sync_now`.
- Léger : un cycle par jour, aucune entité température/puissance superflue.

## Installation via HACS

1. Ouvre **HACS** (bouton `...` en haut à droite) → **Dépôts personnalisés** (Custom repositories).
2. Ajoute ce dépôt : catégorie **Integration**.
3. **HACS → Intégrations → Linky SER** → **Télécharger**, puis **redémarre Home Assistant**.
4. **Paramètres → Appareils et services → Ajouter une intégration → Linky SER** :
   - adresse mail et mot de passe du compte MaConsoLinky (le login est testé immédiatement) ;
   - tarif en **€/kWh** (modifiable ensuite via *Configurer*).
5. Le point de comptage (PASC) est **découvert automatiquement** après connexion — rien à saisir.

Installation manuelle : copie `custom_components/linky_ser/` dans `<config>/custom_components/` et redémarre HA.

## Affichage dans le dashboard Énergie

1. **Paramètres → Tableaux de bord → Énergie**.
2. Ajoute une source **Électricité → Réseau** :
   - *Consommation* : statistique **`ser:linky_consumption`** (kWh) ;
   - *Coût* : statistique **`ser:linky_cost`** (€).
3. Comme le coût est injecté en € réels (somme cumulée à ton tarif), laisse le champ prix vide et utilise la stat de coût — HA appliquera exactement tes 0,1348 €/kWh (ou autre) sans approximation.

## Utilisation

- Déclencher un cycle à la demande : appuie sur **Linky SER sync**, ou `Service : linky_ser.sync_now`.
- Changer de tarif : **Paramètres → Appareils et services → Linky SER → Configurer**. Les heures déjà injectées gardent leur coût historique ; seules les nouvelles heures prennent le nouveau tarif.
- Vérification rapide : **Outils de développement → Statistiques** → `ser:linky_consumption` doit montrer la courbe cumulée et la consommation horaire (max/mean).

## Limites

- Les données SER sont publiées avec ~24 h de retard : la journée J n'est visible que le lendemain matin (le tick 08h00 la ramène).
- Une seule courbe de charge par compte (point de comptage principal) est importée.
- Les identifiants restent dans la configuration Home Assistant (entrée de config chiffrée par ta sauvegarde locale) ; **aucune donnée n'est envoyée ailleurs que sur le portail SER**.

## Licence

MIT — voir [LICENSE](LICENSE).