# Bank Retenue Sync

Automatisation bancaire et fiscale pour ERPNext (société Aquaworld & Servicing) :

- **Banque Zitouna** — registre des mouvements, identification, solde par capture, frais mensuels ;
- **Retenue à la source (TEJ)** — certificats reçus (ingestion, rapprochement, imputation) et
  **émis** (déclaration d'un certificat fournisseur depuis la facture d'achat, suivi du dépôt) ;
- **Achats locaux** — contrôle des factures sur scan (OpenAI), retenue 1 %, matricule fournisseur ;
- **Dépenses automatiques** — Total, Aramex, honoraires, CNSS, carte technologique, en brouillon ;
- **Partenaire Economiq** — échéancier, consolidé, règlements, rapport mensuel sur la fiche client ;
- **Facturation mensuelle** — dossier de clôture pour le comptable.

Tout passe par le microservice **tej-bank-service** (FastAPI + Playwright, dépôt
`wkoubaa1986/gestion-bank-retenue`), joint via `service_url` / `service_token` des réglages
(défaut `http://tej-bank-service:8080`). Le coupe-circuit `Bank Retenue Sync Settings.enabled`
(défaut **0**) rend tout le scheduler inerte tant qu'il n'est pas coché.

## Installation

```bash
bench get-app https://github.com/wkoubaa1986/bank_retenue_sync.git --branch main
bench --site <site> install-app bank_retenue_sync
bench --site <site> migrate
```

Nécessite ERPNext (`required_apps`). Après une première installation sur un site existant, jouer
les patches de politique (non exécutés par `install-app`) :

```bash
bench --site <site> execute bank_retenue_sync.patches.politique_regularisation_ras.execute
bench --site <site> execute bank_retenue_sync.patches.politique_achat_local.execute
```

## Exploitation

- `AUTOMATION_HANDOFF.md` — les flux automatiques, le mapping comptable, les clés de config.
- `HANDOFF.md` — le microservice et les pièges des portails (partiellement daté, voir en-tête).

## Tests

```bash
# Fonctions pures, sans site :
python -m unittest bank_retenue_sync.tests.test_achat bank_retenue_sync.tests.test_emis \
  bank_retenue_sync.tests.test_facturation bank_retenue_sync.tests.test_especes \
  bank_retenue_sync.tests.test_rapport
```

⚠️ Dette connue : 11 des 16 modules historiques (`test_cartes`, `test_ecarts`, `test_virements`…)
échouent depuis avant 08/2026 sur du code inchangé — la CI ne porte que sur les modules sains
ci-dessus, jusqu'à résorption.

## License

mit
