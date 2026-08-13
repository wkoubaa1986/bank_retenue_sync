# bank_retenue_sync — Automatisation des dépenses mensuelles (HANDOFF)

> État au moment de la pause. But : **une fois par jour, créer automatiquement en brouillon les
> écritures de journal des dépenses mensuelles** à partir de la boîte Gmail comptable et des
> mouvements bancaires (`tej-bank-service`). Company = **Aquaworld & Servicing** (comptes `- A&S`).

## ✅ Ce qui est fait (testé sur données réelles)

Cinq flux de dépense → écriture de journal (brouillon), + idempotence + orchestrateur + scheduler.

| Flux | Déclencheur | Source montants | Écriture |
|------|-------------|-----------------|----------|
| **Total** | 📧 email `*@totalenergies.com` sujet « facture » | ZIP → **XLSX** (déterministe) | Cr Carte Total / Dr TVA 19% / Dr Frais de Déplacement |
| **Aramex** | 📧 email `*@e.aramex.com` sujet « E-INV » | PDF → **OpenAI** | Cr Créditeurs+ARAMEX / Dr TVA 7% / Dr Frais de Fret (timbre inclus) |
| **Note d'honoraire** | 📧 email `belghithayman@gmail.com` PJ « HONORAIRE » | PDF → **OpenAI** | Cr Découvert bancaire (NET) + Cr RS achat (3%) / Dr TVA 19% / Dr Charges Diverses (HT+timbre) |
| **Déclaration** mensuelle | 🏦 banque « PRELEVEMENT MIN DES FINANCES » | PDF DECL → **OpenAI** + banque | Cr Zitouna (date banque) / Dr Taxe Loyer, RS achat (1% l.17), Timbre, T.C.L, [TVA collectee si à payer], Impot+CNSS (=reste) **+ Balancer TVA auto** |
| **CNSS** trimestriel | 🏦 banque « PRELEVEMENT C.N.S.S » | banque (montant) | Cr Zitouna / Dr Impot+CNSS. Email comptable = vérif + justificatifs |

**Balancer TVA** (`create_tva_balancer_entry`) : dernier jour du mois, solde les sous-comptes TVA
secondaires (TVA 7%) → 0, aligne TVA 19% sur le **crédit TVA de la déclaration** (`tva_credit`),
contrepartie « Compte temporaire - compte d'overture ». Se crée **automatiquement** avec la déclaration.

## 🗂️ Modules

```
bank_retenue_sync/
├── orchestrator.py              # ★ run_email_ingestion() / run_bank_ingestion() / run_all()  (whitelisted, idempotent)
├── mail/
│   ├── sources.py, classifier.py, finder.py   # détection/classification emails
│   ├── mailbox.py               # accès IMAP réutilisable (lecture seule)
│   ├── aramex_advice.py         # parser TSV du Payment Advice Aramex (virements) — pour rapprochement
│   └── total_invoice.py         # extraction ZIP Total + parse XLSX (totaux facture)
├── ai/
│   ├── invoice_extract.py       # OpenAI : facture Aramex + note d'honoraire (+ pdf_to_text via pypdf)
│   └── declaration_extract.py   # OpenAI : déclaration fiscale (composantes + tva_credit)
├── bank/
│   └── movements.py             # export mouvements tej-bank-service + find_declaration_payment / find_cnss_payment / find_debits_by_keywords / find_credit_by_amount
└── expenses/
    ├── dates.py                 # period_end_date
    └── journal.py               # ★ tous les constructeurs d'écriture + mapping comptable + Balancer TVA
```

## ▶️ Comment lancer

```bash
# manuel (marche déjà)
bench --site mysite.localhost execute bank_retenue_sync.orchestrator.run_all
# ou run_email_ingestion / run_bank_ingestion séparément
```
Job quotidien enregistré : **Scheduled Job Type** `bank_retenue_sync.orchestrator.run_all` (Daily).
⚠️ Le **scheduler du site est DÉSACTIVÉ** → l'activer quand prêt : `bench --site mysite.localhost enable-scheduler`.
Passer en soumission auto (au lieu de brouillon) : `bench --site mysite.localhost set-config brs_auto_submit 1`.

## 🔑 Config (site_config, repli tant que l'app n'est pas installée)

`brs_service_url`, `brs_service_token`, `brs_push_secret` (⚠️ **à régénérer**, exposés en dev),
`brs_auto_submit` (0=brouillon). Clé OpenAI lue depuis le Single **AI Settings** (woocommerce_fusion) :
`openai_api_key`, `open_ai_model`, `open_ai_temperature`.

## 💳 Carte technologique — mécanisme de génération des écritures

Trésorerie **parallèle** : rechargée par virement Zitouna, dépensée en pub Facebook. Ces dépenses ne
paraissent **jamais** au relevé bancaire → le rapprochement bancaire ne peut rien en dire. Source de
vérité = le **relevé de CARTE** (`GET /banque/cartes/export/latest`, XLSX).

Chaîne (`bank/cartes.py`, orchestrée par `orchestrator.run_cartes()`, tâche quotidienne 9h40) :

1. `fetch_latest_cartes()` → lignes `Date | Operation | Detail | Statut | Reference | Montant`.
2. `est_approuve()` → **statut « Transaction approuvée » ET référence non vide**. Les
   « Solde insuffisant » sont des **refus** : aucun argent n'a bougé (9 lignes sur 17 au dernier
   export ; les comptabiliser aurait inventé ~3 400 DT de charges).
3. `a_comptabiliser()` → écarte ce qui est antérieur à la dernière écriture du compte **et** ce qui
   porte déjà une écriture. **L'idempotence tient au `cheque_no` = `Facture Facebook <référence>`**,
   format identique aux saisies manuelles historiques → la date n'est qu'un raccourci, on peut la
   relâcher (`depuis=None`) pour un rattrapage sans risque de doublon.
4. `build_journal_entry()` → **Dr Frais de Marketing / Cr Carte technologique**, à la date
   d'opération. Deux lignes, ni TVA ni timbre (pub en ligne facturée HT par l'Irlande).
   Soumise si `auto_submit_journal_entries` est coché.
5. `sync_frais_carte()` → **seulement si plus rien n'est à comptabiliser**, aligne le solde
   comptable sur le solde réel par `Dr Frais bancaire / Cr Carte`. Tant qu'un paiement manque,
   l'écart **n'est pas un frais, c'est ce paiement**.
6. `alerte_recharge()` → sous le seuil, une notification/jour aux Accounts Managers.
   **Jamais en essai à blanc** (`insert=0`) : ça consommerait l'unique alerte du jour.

**Suis-je à jour ?** `cartes.etat_controle()` / whitelist `controle_releve` confronte relevé et
livres. `a_jour` exige **les deux preuves** : chaque paiement approuvé porte son écriture, **et**
solde comptable = solde réel. Exposé dans le rapport **Carte technologique** : indicateur de page,
tuiles (« Paiements carte non comptabilisés », « Écart livres / carte »), bouton **Contrôle du
relevé** (lecture seule, ligne par ligne avec l'écriture liée) et bouton **Générer les écritures**
(essai à blanc → création).

**Recharge** : déclencheur = solde réel < `seuil_recharge_carte` (700 DT) ; montant =
`montant_recharge_carte` (**1500 DT**, recharge type et non appoint — virer le seul manque ferait
replonger la carte dès le premier débit ~600 DT), **plafonné par le solde disponible en banque**
(dernier `BRS Solde Bancaire`). Proposé dans le tableau **Paiements à faire** — seule ligne sans
écriture de journal, la recharge n'étant pas encore décidée.

## ⚠️ Idempotence / gotchas

- Idempotence = par **`cheque_no`** du Journal Entry. En **PROD**, ajouter une dédup par **référence
  bancaire (FT…)** : d'anciennes saisies manuelles peuvent porter un libellé de période différent
  (ex. coquille « Declaration comptable 05-2025 » pour un paiement de mai-2026).
- Déclaration : la « Retenue a la source achat » = **RS 1% acquisitions ≥1000 DT (ligne 17)**, PAS le
  3% honoraires (qui tombe dans le reste « Impot+CNSS »). Vérifié sur historique.
- `TVA à payer` (>0) → ligne `Dr TVA collectee` ; `crédit TVA` → pas de TVA collectee mais Balancer TVA.
- Vision OpenAI indisponible (ni PyMuPDF ni pdf2image) — tous les PDF traités ont du texte extractible.

## ⏭️ Ce qui reste

1. **Rapprochement bancaire journalier** (côté user) : clôturer les créditeurs Aramex/honoraire quand
   le paiement sort en banque, et identifier les **virements Aramex reçus** (NET du Payment Advice =
   crédit « VIR TN AUTRE BQ ARAM » ; briques prêtes : `find_credit_by_amount`, `parse_advice`).
2. **Dédup par référence bancaire** avant mise en prod.
3. **Install propre de l'app** (optionnel) : débloquer en créant les DocTypes TEJ vides
   (`BRS Sync Run/Item`, `BRS Request Log`) + contrôleur `Retenue Certificate` → scheduler via hooks.py
   + param `auto_submit` dans un vrai Settings chiffré (au lieu de site_config).
4. **Nettoyer les brouillons de test** en dev (ACC-JV-2026-00525 → 00538).

## 🚫 Ne pas toucher

`customization_app` (importer `RAS_MODE` depuis `customization_app/retenue_source.py` si besoin).
