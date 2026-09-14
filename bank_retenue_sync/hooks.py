app_name = "bank_retenue_sync"
app_title = "Bank Retenue Sync"
app_publisher = "Wassim Koubaa"
app_description = "Ingestion des certificats de retenue a la source (TEJ) vers ERPNext"
app_email = "koubaawassim@gmail.com"
app_license = "mit"

# L'app est inutilisable sans ERPNext : ses DocTypes pointent Account, Purchase Invoice, Mode of
# Payment, Warehouse…, et ses doc_events vivent sur Purchase Invoice. Sans cette ligne,
# `bench install-app` sur un site nu echoue au sync des DocTypes avec une erreur de Link obscure.
required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Taches planifiees
#
# Ordre voulu : l'export bancaire (lent, scraping Playwright) alimente le registre a 06h00 ;
# les depenses recurrentes le relisent a 06h45, donc sur des donnees fraiches ; l'audit constate
# en fin de matinee. Chaque tache est un no-op si le coupe-circuit `enabled` des Settings est
# decoche, et aucune ne soumet d'ecriture.
#
# Aucun `fixtures` : les DocTypes, la Page et le Workspace vivent en fichiers de module. Les
# passer en fixtures les ferait entrer en collision avec ceux de customization_app au migrate.
# ---------------------------------------------------------------------------
scheduler_events = {
    "cron": {
        # SEPT VERIFICATIONS BANCAIRES PAR JOUR (5h et 7h ajoutees le 2026-08-21 a la demande
        # de l'utilisateur — les depots de la veille au soir sont traites des l'aube).
        # Chacune est complete : nouvel export, capture du solde, identification, puis les
        # creations declenchees par le releve (brouillons d'ENCAISSEMENT a soumission
        # humaine, versements d'especes, declaration fiscale et CNSS verifies par l'email du
        # comptable, depenses recurrentes actives, echeances de contrats, reglement des dettes
        # Aramex/honoraire au virement emis) et l'ecriture mensuelle de frais. L'ecriture etant
        # CUMULATIVE et recalculee depuis zero, sept passages ne produisent qu'une seule
        # ecriture par mois, toujours a jour. Le tick ne fait que METTRE EN FILE le travail
        # (queue longue, dedup par job_id) : deux passages ne s'empilent jamais.
        "0 5,7,9,11,13,15,17 * * *": ["bank_retenue_sync.tasks.daily.verification_bancaire"],
        # Factures recues par EMAIL (Total, Aramex, note d'honoraire) : CINQ passages par jour a
        # partir de 8h (demande utilisateur 2026-08-21). L'ingestion est idempotente (reference
        # periodisee), relire la boite ne cree jamais deux fois. Les passages banque de 5h et 7h
        # peuvent voir un prelevement AVANT la facture email du jour : sans piece, ils ne creent
        # rien et le passage suivant rattrape — c'est le fonctionnement nominal, pas un defaut.
        "0 8,10,12,14,16 * * *": ["bank_retenue_sync.tasks.daily.factures_email"],
        # Les depenses a date fixe sont creees TOT, avant la banque : c'est tout leur interet.
        # Salaires (2 j avant la fin du mois), loyer (le 15, un mois sur deux), honoraire
        # comptable (le 25). Elles anticipent le prelevement au lieu de le subir.
        "0 5 * * *": ["bank_retenue_sync.tasks.daily.depenses_calendaires"],
        # Puis les flux qui se LISENT au releve, apres la premiere verification de la journee.
        "30 9 * * *": ["bank_retenue_sync.tasks.daily.depenses_recurrentes"],
        # Carte technologique : son releve est un flux a part, la carte n'etant pas la banque.
        "40 9 * * *": ["bank_retenue_sync.tasks.daily.paiements_carte"],
        # Certificats de retenue a la source : le portail TEJ est alimente par nos clients, pas
        # par nous. TROIS passages par jour (demande utilisateur 2026-08-21), aux minutes :20
        # dans les CREUX entre les passages banque (heures pile) et les depots TEJ (:10) — le
        # service n'a qu'un worker navigateur, les jobs se font la queue, pas la guerre.
        # L'empreinte du fichier coupe court si le portail n'a pas bouge depuis le passage
        # precedent ; seul le rapprochement est rejoue, et il est bon marche.
        "20 7,12,16 * * *": ["bank_retenue_sync.tasks.daily.certificats_ras"],
        # Depots de certificats EMIS en attente d'analyse chez TEJ. CINQ passages (12h10 et
        # 16h10 ajoutes le 2026-08-21 a la demande de l'utilisateur) : un depot soumis le matin
        # doit devenir un PDF remis au fournisseur dans la journee, pas le lendemain. L'appel
        # est court et en LECTURE SEULE — il ne resoumet jamais rien — et les heures :10 sont
        # decalees de la banque (heures pile) et des certificats recus (:20) pour ne pas se
        # disputer le worker unique du service, qui pilote un navigateur.
        "10 10,12,14,16,18 * * *": ["bank_retenue_sync.tasks.daily.depots_tej"],
        # Export des certificats EMIS regenere une fois par jour (9h50, creux avant les depots
        # de 10h10) : les certificats crees A LA MAIN sur le portail apparaissent dans le recap
        # des retenues d'achat au plus tard le lendemain matin, sans attendre une soumission.
        "50 9 * * *": ["bank_retenue_sync.tasks.daily.export_emis"],
        "35 9 * * *": ["bank_retenue_sync.tasks.daily.contrats_financement"],
        # Confirmation des ordres : en fin de journee, une fois les cinq passages faits.
        "30 17 * * *": ["bank_retenue_sync.tasks.daily.confirmation_ordres"],
        "45 17 * * *": ["bank_retenue_sync.tasks.daily.audit_quotidien"],
    },
}

# Le raccourci « Paiements a faire » vit dans le workspace `Accounting`, qui appartient a
# ERPNext : il serait ecrase a chaque mise a jour. On le reinstalle donc apres chaque migration.
after_migrate = "bank_retenue_sync.install.after_migrate"
# Le meme geste a l'install : `bench install-app` seul n'appelle PAS after_migrate (verifie dans
# frappe/installer.py), et un deploiement qui installerait sans migrer laisserait le workspace
# sans ses raccourcis. install.py est idempotent, le double appel est sans effet.
after_install = "bank_retenue_sync.install.after_migrate"

fixtures = []

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "bank_retenue_sync",
# 		"logo": "/assets/bank_retenue_sync/logo.png",
# 		"title": "Bank Retenue Sync",
# 		"route": "/bank_retenue_sync",
# 		"has_permission": "bank_retenue_sync.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/bank_retenue_sync/css/bank_retenue_sync.css"
# app_include_js = "/assets/bank_retenue_sync/js/bank_retenue_sync.js"

# include js, css files in header of web template
# web_include_css = "/assets/bank_retenue_sync/css/bank_retenue_sync.css"
# web_include_js = "/assets/bank_retenue_sync/js/bank_retenue_sync.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "bank_retenue_sync/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "bank_retenue_sync/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "bank_retenue_sync.utils.jinja_methods",
# 	"filters": "bank_retenue_sync.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "bank_retenue_sync.install.before_install"
# after_install = "bank_retenue_sync.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "bank_retenue_sync.uninstall.before_uninstall"
# after_uninstall = "bank_retenue_sync.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "bank_retenue_sync.utils.before_app_install"
# after_app_install = "bank_retenue_sync.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "bank_retenue_sync.utils.before_app_uninstall"
# after_app_uninstall = "bank_retenue_sync.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "bank_retenue_sync.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# ⚠️ LE CONTROLE DE L'ACHAT LOCAL S'ACCROCHE AVANT ET APRES LA VALIDATION, PAS PENDANT.
# ⚠️ `before_validate` ET NON `validate` : c'est la seule position ou ERPNext recalcule ENSUITE ce
# qui depend de ce qu'on change. Poser la date de comptabilisation apres son controle laissait une
# date d'echeance anterieure a la facture, et la ligne de retenue n'etait plus reprise dans les
# totaux. Avant, tout se recalcule naturellement.
# La ligne de retenue se pose tant que la facture est modifiable — apres validation, la
# table des taxes est figee. `before_submit` refuse tant qu'il manque la preuve, le stock, la
# retenue ou la concordance des totaux : c'est le dernier instant ou refuser coute moins cher que
# corriger.
doctype_js = {
    "Purchase Invoice": "public/js/purchase_invoice.js",
    # Retenue a la source prelevee sur une DEPENSE DE CAISSE : la piece est une ecriture de
    # journal, pas une facture. Le bouton y ouvre la meme emission TEJ, par l'adaptateur de
    # `tej/emis_journal` (demande utilisateur 04/09/2026).
    "Journal Entry": "public/js/journal_entry_retenue.js",
}

# Boutons « Écarts Aramex » (perte / ajustement / avoir) sur le brouillon d'encaissement.
# ⚠️ PAS via doctype_js : « Encaissement Paiement » est un DocType CUSTOM et FormMeta.add_code
# (frappe/desk/form/meta.py) commence par `if self.custom: return` — le hook doctype_js est
# silencieusement ignore pour les doctypes custom. Le fichier est donc charge globalement ;
# il ne fait qu'enregistrer un frappe.ui.form.on("Encaissement Paiement"), inerte ailleurs.
# Le parametre ?v= force le navigateur a recharger le fichier a chaque evolution (les assets
# bruts n'ont pas le hash de build des bundles) — INCREMENTER a chaque modification du JS.
app_include_js = [
    "/assets/bank_retenue_sync/js/encaissement_paiement.js?v=4",
    # Badge « nombre de virements a faire » sur le raccourci Rapport de l'espace Comptabilite
    # (le compteur natif des raccourcis ne couvre que les DocTypes).
    "/assets/bank_retenue_sync/js/paiements_a_faire_compteur.js?v=1",
]

# Vue liste : l'etat du certificat TEJ de chaque facture. Charge APRES le fichier d'ERPNext, ce
# qui permet d'etendre `listview_settings` au lieu de l'ecraser (cf. le fichier).
doctype_list_js = {
    "Purchase Invoice": "public/js/purchase_invoice_list.js",
    # Pastille « certificat de retenue » sur chaque ecriture de caisse : a emettre, incomplet,
    # depot en analyse, emis. Sans elle, l'etat ne se voit qu'en ouvrant les ecritures une par une
    # (demande utilisateur 05/09/2026).
    "Journal Entry": "public/js/journal_entry_list.js",
}

doc_events = {
    "Purchase Invoice": {
        "before_validate": "bank_retenue_sync.achat.facture.a_l_enregistrement",
        "before_submit": "bank_retenue_sync.achat.facture.avant_validation",
    },
    # Un lot Aramex porteur d'ecarts bloquants (delta de paiement, ligne d'advice sans piece)
    # ne peut pas etre soumis tant qu'un humain n'a pas resolu — cf. encaissement/ecarts.py.
    "Encaissement Paiement": {
        "before_submit": "bank_retenue_sync.encaissement.ecarts.before_submit",
        # La piece soumise (ou annulee) change les ecarts de rapprochement du mois : l'ecriture
        # cumulative de frais est resynchronisee en job, sans attendre le cron du lendemain.
        "on_submit": "bank_retenue_sync.expenses.fees.rafraichir_apres_encaissement",
        "on_cancel": "bank_retenue_sync.expenses.fees.rafraichir_apres_encaissement",
    },
}

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"bank_retenue_sync.tasks.all"
# 	],
# 	"daily": [
# 		"bank_retenue_sync.tasks.daily"
# 	],
# 	"hourly": [
# 		"bank_retenue_sync.tasks.hourly"
# 	],
# 	"weekly": [
# 		"bank_retenue_sync.tasks.weekly"
# 	],
# 	"monthly": [
# 		"bank_retenue_sync.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "bank_retenue_sync.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "bank_retenue_sync.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "bank_retenue_sync.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["bank_retenue_sync.utils.before_request"]
# after_request = ["bank_retenue_sync.utils.after_request"]

# Job Events
# ----------
# before_job = ["bank_retenue_sync.utils.before_job"]
# after_job = ["bank_retenue_sync.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"bank_retenue_sync.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

