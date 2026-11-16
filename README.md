# Bank Retenue Sync

Automatisation bancaire et fiscale pour ERPNext (société Aquaworld & Servicing) :

- **Banque Zitouna** — registre des mouvements, identification, solde par capture, frais mensuels ;
- **BIAT** (depuis le 16/11/2026) — les mouvements du compte BIAT entrent au même registre, chacun
  portant sa banque (`BRS Bank Movement.banque`). `bank/biat.py` les importe (service
  `/jobs/biat/mouvements/export`, ou le fichier d'export) et RECONNAÎT chaque ligne : remise de chèques
  et son numéro de bordereau, commission de la remise, versement d'espèces ; un libellé inconnu sort
  « à vérifier ». Page « Identification bancaire » : choix de la banque en tête des filtres. Rien
  n'est créé pour la BIAT (ni écriture ni encaissement) : l'Encaissement Paiement vise encore le
  compte Zitouna. Tout le reste de l'app ne lit que Zitouna (`registry.BANQUE_DEFAUT`) ;
  ses frais vont dans une écriture mensuelle À PART (« BIAT frais bancaires MM-AAAA », tout en
  « Frais bancaire », même interrupteur et mêmes règles de remplacement que les frais Zitouna) ;
- **Bordereaux de remise** — `BRS Bordereau Remise` : le scan du bordereau remis à la banque (BIAT ou
  Zitouna ; chèques, traites, espèces), rangé avec sa banque, son numéro et sa date. Depuis la caisse
  journalière (bouton « Bordereaux de remise », app customization_app), on donne seulement le scan —
  photo ou fichier (`deposer`) : l'IA y lit aussi la banque, le numéro, la date et la nature, et le
  bordereau est collecté tout seul (`collecter_par_ia`). Si elle ne peut pas l'identifier (banque
  illisible, numéro du nom de fichier différent du numéro imprimé, panne), la saisie à la main est
  proposée, préremplie ; le nom du fichier rangé à la façon de l'entreprise (« Ordre de versement
  Chèques Bq BIAT-7473685-du 09-11-2026.pdf ») fait foi pour la banque, le numéro et la date. Depuis
  la liste, la fiche se saisit à la main et la lecture suit. En arrière-plan, l'IA lit le scan
  (`ai/bordereau_extract` : page recadrée sur le bordereau, écriture manuscrite, modèle du réglage
  « Modèle IA de lecture des bordereaux », gpt-6.1-sol par défaut), liste les chèques ou les traites
  et cherche chacun parmi les paiements d'ERPNext (`api/bordereaux.rapprocher` : même numéro, numéro
  à un chiffre près avec le même montant, montant unique du portefeuille, pièce déjà sortie du
  portefeuille). Chaque ligne sort « Trouvé », « À vérifier » (l'écart est dit) ou « Introuvable » ;
  un écart de montant sous 1 DT sur le bon numéro est un arrondi : noté, pas signalé. La personne qui
  remet le bordereau ne voit qu'un verdict — « Tout est bon » ou « À signaler », avec une phrase par
  chose à signaler (champ `a_signaler`) ; le détail pièce par pièce est réservé au contrôle.
  C'est un contrôle : rien n'est comptabilisé, une lecture ratée laisse le bordereau collecté, et
  l'on peut corriger une ligne sur la fiche puis « Revérifier dans ERPNext ». L'identification d'une
  remise BIAT reprend ce résultat dans sa raison ;
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
  bank_retenue_sync.tests.test_biat \
  bank_retenue_sync.tests.test_rapport
```

⚠️ Dette connue : 11 des 16 modules historiques (`test_cartes`, `test_ecarts`, `test_virements`…)
échouent depuis avant 08/2026 sur du code inchangé — la CI ne porte que sur les modules sains
ci-dessus, jusqu'à résorption.

## License

mit
