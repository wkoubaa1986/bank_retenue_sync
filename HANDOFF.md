# Handoff — bank_retenue_sync


> ⚠️ **OBSOLÈTE DEPUIS 08/2026 SUR UN POINT MAJEUR — le protocole.**
> Ce document décrit un flux *push* : le microservice pousserait un JSON signé HMAC vers un
> endpoint `api/receive.py`. Cet endpoint n'a jamais été écrit, et l'openapi réel du service ne
> propose plus que des **exports à lire** (`GET /tej/certificats-recus/export/latest`,
> `POST /jobs/tej/certificats-recus/export`, `GET /tej/certificats/{reference}/pdf`).
> La récupération des certificats est donc implémentée en **pull**, comme les quatre flux
> bancaires : voir `bank_retenue_sync/tej/certificats.py` (ingestion), `tej/rapprochement.py`
> (identification client/pièce/facture), `tej/paiements.py` et `tej/pdf.py`.
> Le reste du document — description du microservice, format des données, pièges du portail —
> reste juste et utile.

État au 2026-07-20 — **dépassé** : depuis 08/2026 l'app est complète, installable
(`bench install-app bank_retenue_sync`) et couverte de tests. Le microservice est terminé et testé.
Pour l'exploitation courante, voir `AUTOMATION_HANDOFF.md` (flux, mapping comptable, clés de config).

---

## Vue d'ensemble

Deux dépôts, une seule chaîne :

```
ERPNext  --POST /jobs/tej/sync (bearer)-->  tej-bank-service  --Playwright-->  TEJ
   ^                                              |
   +---- POST /api/method/…receive (HMAC signé) --+
```

- **`/home/wassim/Documents/Local_Services/tej-bank-service`** — ✅ terminé,
  35 tests verts, commit `29dd83c`.
- **`…/frappe-bench/apps/bank_retenue_sync`** — 🚧 en cours, ~30 % fait.

Conception complète : `docs/PLAN.md` dans le dépôt du service (copie du plan
validé).

Périmètre v1 décidé : **ingestion seule**. Le rapprochement certificat →
Payment Entry est explicitement remis à l'itération suivante.

---

## Ce qui est fait — `tej-bank-service` ✅

Ne rien y reprendre sans raison ; c'est vérifié.

| Fichier | Rôle |
|---|---|
| `app/scrapers/tej_bot.py` | bot d'origine, **logique Playwright inchangée**. Seuls les chemins (config) et le point d'entrée (`scrape()` au lieu de `main()` argparse) changent |
| `app/scrapers/normalize.py` | Excel TEJ → contrat JSON. Résolution **stricte** des colonnes |
| `app/scrapers/mock.py` | `MOCK_SCRAPERS=1` rejoue une fixture, sans navigateur |
| `app/jobs.py` | file asyncio + registre SQLite, **un seul worker** |
| `app/push.py` | POST signé vers ERPNext, retries 5/30/120 s |
| `app/security.py` | bearer entrant, signature HMAC sortante |
| `app/handlers.py` | scrape → normalise → push |
| `app/routers/` | `/health`, `/jobs/*`, `/certificats/{ref}/pdf` |
| `Dockerfile` | base Playwright officielle + xvfb + tini |

Vérifié : `35 passed`, le service démarre, `/health` répond.

```bash
cd /home/wassim/Documents/Local_Services/tej-bank-service
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app.main:app --port 8080
```

### Décisions à ne pas défaire

- **Un seul worker** (`--workers 1`). Deux processus Playwright sur le même
  profil persistant le corrompent, et ce profil porte la session anti-captcha.
- **Navigateur visible sous Xvfb**, jamais headless — le headless est détecté
  et déclenche le reCAPTCHA. D'où `xvfb-run` devant uvicorn.
- **Normalisation stricte.** Colonne absente ou ambiguë ⇒ le job échoue en
  listant les en-têtes reçus. Un mapping faux en silence coûte un mois de
  données ; un job en échec coûte cinq minutes.
- **Signature liée à l'horodatage** : `HMAC(secret, "{ts}." + corps)`. Sans ce
  liage, un rejeu resterait valide indéfiniment.
- **Séparation des secrets.** `TEJ_MATRICULE`/`TEJ_PASSWORD` ne vivent que dans
  l'environnement du conteneur ; ERPNext ne détient que le jeton de service et
  la clé HMAC. Un ERPNext compromis ne donne pas accès à TEJ.

### Défaut corrigé dans le bot d'origine

`_find_column(df, "clarant")` matchait **aussi bien** `Déclarant` que
`Matricule fiscal déclarant`, et renvoyait la première colonne rencontrée. Un
simple changement d'ordre des colonnes côté TEJ aurait suffi à lire le
matricule comme raison sociale, sans aucun signal. Les `ColumnSpec` portent
désormais des `excludes` et l'ambiguïté est une erreur. Régression couverte par
`test_declarant_not_confused_with_matricule`.

---

## Le contrat JSON (gelé — les deux côtés doivent bouger ensemble)

`POST {erp}/api/method/bank_retenue_sync.api.receive.retenue_certificates`

En-têtes : `X-BRS-Timestamp`, `X-BRS-Signature: sha256=<hex>`, `X-BRS-Job-Id`,
`X-BRS-Source`, `Host: <site>`.

```json
{"source": "tej",
 "job_id": "jb_…",
 "scraped_at": "2026-07-20T12:00:00Z",
 "certificates": [
   {"reference": "1234567890123",
    "declarant": "STE ALPHA SARL",
    "declarant_matricule": "1234567A/M/000",
    "date_paiement": "2026-05-31",
    "etat_depot": "Déposé",
    "total_brut": 5000.0,
    "montant_retenue": 50.0,
    "taux": 1.0,
    "type_retenue": "Marché public 1%",
    "pdf_available": true,
    "pdf_sha256": "…",
    "raw": {"…ligne Excel verbatim…"}}]}
```

Source de vérité : `app/models.py` (`Certificate`, `CertificatePayload`).
`tests/test_pipeline.py::test_push_payload_is_signed_and_verifiable` rejoue
côté receveur exactement la vérification attendue d'ERPNext — **s'en servir
comme spécification** en écrivant `api/receive.py`.

---

## Ce qui reste — `bank_retenue_sync` 🚧

Chemin : `…/frappe-bench/apps/bank_retenue_sync/bank_retenue_sync/`
Module : `Bank Retenue Sync` → dossier `bank_retenue_sync/` (imbrication
normale quand le nom du module égale celui de l'app).

### Fait
- `bench new-app` passé, app compilée et liée.
- Arborescence : `api/ client/ ingest/ tasks/ bank_retenue_sync/doctype/`.
- DocType **Bank Retenue Sync Settings** (Single) — json + py.
- DocType **Retenue Certificate** — json (les champs de rapprochement sont
  déjà présents mais inutilisés : ajouter le matching plus tard sera du code,
  pas une migration de schéma).

### À faire
1. **DocTypes manquants** (dossiers créés, JSON à écrire) :
   - `BRS Sync Run` — `kind`, `remote_job_id` (indexé), `status`
     (Queued/Running/Success/Partial/Failed), horodatages,
     `rows_received/created/duplicate/failed`, **`payload_hash` unique**,
     `payload`, `error_log`, table `log`
   - `BRS Sync Run Item` (enfant) — `row_key`, `status`, Dynamic Link
     `document`, `message`
   - `BRS Request Log` — forme de `WooCommerce Request Log`
     (`woocommerce_fusion/tasks/utils.py:63-88`)
   - `retenue_certificate.py` (contrôleur, encore absent)
2. **`api/receive.py`** — modèle : `woocommerce_fusion/woocommerce_endpoint.py`,
   **avec la vérification HMAC activée**. Vérifié : dans ce fichier la
   comparaison est commentée aux lignes 31-35, donc l'endpoint n'authentifie
   rien. Ne pas reproduire.
   - `frappe.request.get_data()` (pas `.data`) : signer sur un corps
     re-sérialisé est le bug HMAC classique
   - vérifier la signature **avant** `frappe.set_user` — tout bug en amont est
     une élévation de privilèges sur un endpoint `allow_guest`
   - accepter `push_secret` **ou** `push_secret_previous` (rotation)
   - tolérance d'horodatage 300 s ; `payload_hash` déjà vu ⇒ 409
   - répondre `202` + `frappe.enqueue(queue="long")` : 200 certificats ne
     doivent pas être traités dans la requête
3. **`ingest/certificates.py`** — upsert sur `reference`, chaque ligne dans un
   `frappe.db.savepoint()`. Un doublon incrémente `rows_duplicate` et continue.
   **Ré-ingérer une période chevauchante doit être un no-op** — c'est le test
   le plus important. Le PDF s'attache au `Retenue Certificate` lui-même en v1.
4. **`client/service_client.py`** — modèle `APIWithRequestLogging`, avec deux
   écarts délibérés : journaliser dans un `finally` plutôt que dupliquer
   l'`enqueue`, et passer `res.text`/`status_code`/`elapsed` plutôt qu'un objet
   `Response` (`frappe.enqueue` pickle ses arguments).
5. **`hooks.py`** — cron `30 8 * * *` → `tasks.scheduled.daily_tej_sync`,
   `15 * * * *` → `poll_stale_runs`, verrou `frappe.cache().setex` façon
   `sync_job.py:20-34`. `fixtures = []`.
6. **Tests** — HMAC valide/invalide/périmé/rejeu/désactivé ; ingestion
   idempotente ; isolation par savepoint.
7. **Compose** — bloc `tej-bank-service` par **image** (`ghcr.io/wkoubaa1986/
   tej-bank-service:0.1.0`), jamais par chemin relatif, + override dev
   `.devcontainer/docker-compose.tej-dev.yml` avec `build:` et `--reload`.

### Règles locales à respecter
- **DocTypes en fichiers de module, pas en fixtures** — règle explicite à
  `customization_app/customization_app/hooks.py:338`.
- **Ne rien modifier dans `customization_app`.** Vérifié : `_attachments()`
  (`retenue_source.py:58-81`) lit `tabFile` uniquement sur
  `attached_to_doctype IN ('Sales Invoice','Payment Entry')` + `attached_to_name`.
  À l'itération suivante, faire pointer le `File` vers la Payment Entry
  rapprochée suffit à alimenter la colonne Justificatifs et à faire baisser
  `missing_proof`. Importer `RAS_MODE` depuis ce module plutôt que redéclarer
  la chaîne.
- Le conteneur de dev est **`frappe_docker_devcontainer-frappe-1`**, et non
  `frappe_docker-frappe-1` comme l'indique `CLAUDE.md`.

---

## Prérequis côté utilisateur

1. **Un export TEJ réel** (`.xlsx`, anonymisé si besoin) à déposer dans
   `tej-bank-service/tests/fixtures/certificats_recus_sample.xlsx`.
   La fixture livrée est une **hypothèse** sur les en-têtes, déduite des
   mots-clés du script d'origine — elle n'a jamais été confrontée à un vrai
   export. Si `resolve_columns` échoue sur le fichier réel, c'est le garde-fou
   qui fonctionne : ajuster `COLUMN_SPECS`.
2. **Amorcer le profil navigateur** avant toute exécution réelle —
   `docs/bootstrap-profile.md`. Inutile en mode mock.
3. **Générer les secrets** : `openssl rand -base64 32` pour `SERVICE_TOKEN` et
   `PUSH_SECRET`, à reporter à l'identique dans les Settings ERPNext.

---

## À traiter, hors périmètre de ce chantier

`outil-facturation-erpnext` contient des identifiants d'API vivants commités
comme valeurs par défaut de `os.getenv`, à `get_economiq_situation.py:60-61`.
Ils sont dans l'historique git. **À révoquer**, indépendamment de ce projet.

Le dépôt du service a `origin` renommé en `upstream` pour qu'un `git push`
distrait ne réécrive pas `gestion-bank-retenue`. Créer le nouveau dépôt et
ajouter `origin` avant de pousser.
