# Rapprochement automatique iRaiser ↔ BCE

`enterprise_match.py` identifie le numéro d’entreprise BCE le plus plausible pour les contacts iRaiser qui ne l’ont pas renseigné. Il combine normalisation, blocage SQL, recherche plein texte, comparaison fuzzy et règles de décision conservatrices.

## Résultat de sécurité

Chaque ligne reçoit l’un des statuts suivants :

- `MATCH_CERTAIN` : le numéro peut être repris depuis `automatic_enterprise_number` ;
- `MATCH_PROBABLE` : le candidat est placé dans `suggested_enterprise_number`, mais doit être vérifié ;
- `NO_RELIABLE_MATCH` : aucun numéro n’est proposé ;
- `SKIPPED_EXISTING_NUMBER` : la ligne contenait déjà un numéro belge valide et n’a pas été rematchée. Un numéro mal formé ou dont la clé de contrôle est invalide est traité comme une donnée à corriger et la ligne est rematchée ;
- `MATCH_ERROR` : une erreur isolée s’est produite sur la ligne ; le détail figure dans `match_error` et aucun numéro automatique n’est produit.

Ne jamais importer automatiquement la colonne `candidate_enterprise_number`. Elle contient simplement le meilleur candidat brut, y compris pour les lignes non fiables. La seule colonne prévue pour une mise à jour automatique est `automatic_enterprise_number`.

## 1. Installation

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS
# source .venv/bin/activate

python -m pip install -r requirements.txt
python -m pytest -q
```

## 2. Construire l’index BCE

### Option recommandée : fichiers CSV BCE bruts

Le dossier `data/` doit contenir au minimum :

- `enterprise.csv` ;
- `denomination.csv` ;
- `address.csv`.

Construction avec les sièges sociaux (`REGO`) :

```bash
python enterprise_match.py build-index \
  --data-dir data \
  --index output/bce_reference.sqlite \
  --overwrite
```

Pour reconnaître aussi une entreprise via l’adresse d’une unité d’établissement (`BAET`), ajouter `establishment.csv` et utiliser :

```bash
python enterprise_match.py build-index \
  --data-dir data \
  --index output/bce_reference.sqlite \
  --include-establishments \
  --overwrite
```

Les adresses radiées/historiques sont exclues par défaut. C’est volontaire : une ancienne adresse ne doit pas déclencher automatiquement un mauvais numéro.

### Alternative : fichier Excel/CSV déjà généré par `table_gen.py`

```bash
python enterprise_match.py build-index \
  --reference output/entreprises_belgique_numero_nom_adresse_split.xlsx \
  --index output/bce_reference.sqlite \
  --overwrite
```

Le mode CSV brut est préférable : il conserve toutes les dénominations officielles, abréviations et noms commerciaux, ainsi que les variantes FR/NL des adresses. Le fichier aplati ne contient généralement qu’un nom et une adresse par entreprise.

## 3. Faire le matching

Avec les colonnes iRaiser déjà prévues dans le projet (`reference`, `company_name`, `street3`, `street_number`, `street_box`, `zip`, `city`, `enterprise_number`) :

```bash
python enterprise_match.py match \
  --input data/iraiser_missing_enterprise_number.xlsx \
  --index output/bce_reference.sqlite \
  --column-map column_map_iraiser.example.json \
  --config match_config.example.json \
  --output output/iraiser_enterprise_matches.xlsx \
  --output-format both
```

Le programme essaie également de détecter les colonnes automatiquement. Le fichier JSON est recommandé en production afin d’éviter qu’un changement d’intitulé dans l’export iRaiser passe inaperçu.

Pour un CSV encodé ou séparé de façon particulière :

```bash
python enterprise_match.py match \
  --input data/export_iraiser.csv \
  --index output/bce_reference.sqlite \
  --encoding cp1252 \
  --delimiter ";" \
  --output output/iraiser_enterprise_matches.xlsx
```

## 4. Fichiers produits

Avec `--output-format both`, le programme crée :

- un classeur Excel avec les onglets `all_results`, `match_certain`, `to_review`, `no_reliable_match`, `skipped`, `errors` et `top_candidates` ;
- un dossier CSV contenant les mêmes catégories.

Colonnes principales :

| Colonne | Usage |
|---|---|
| `match_status` | Décision finale |
| `automatic_enterprise_number` | À importer uniquement pour `MATCH_CERTAIN` |
| `suggested_enterprise_number` | Candidat certain ou probable |
| `candidate_enterprise_number` | Meilleur candidat brut, même s’il est refusé |
| `match_score` | Score explicable de 0 à 100, pas une probabilité |
| `second_best_score` | Score du deuxième candidat |
| `score_gap` | Écart entre le premier et le deuxième candidat |
| `strong_evidence` | Présence d’une combinaison nom + adresse suffisamment forte |
| `hard_contradictions` | Ex. code postal ou numéro de rue contradictoire |
| `existing_enterprise_number_valid` | Validité structurelle et clé de contrôle du numéro déjà présent |
| `match_reasons` | Preuves ayant soutenu la décision |
| `candidate_generation_rules` | Blocs ayant retrouvé le candidat |

## 5. Évaluer et calibrer les seuils

Utiliser un export iRaiser de contacts dont le numéro d’entreprise est déjà connu. Le programme masque ce numéro pendant le rapprochement, puis compare la prédiction à la vérité.

```bash
python enterprise_match.py evaluate \
  --input data/iraiser_known_enterprise_numbers.xlsx \
  --index output/bce_reference.sqlite \
  --column-map column_map_iraiser.example.json \
  --output output/matching_evaluation.json \
  --target-precision 0.995 \
  --target-probable-precision 0.95
```

Le rapport contient notamment :

- le rappel de génération des candidats ;
- l’exactitude du premier candidat ;
- la précision et la couverture des `MATCH_CERTAIN` ;
- le nombre de faux matches certains ;
- une proposition de configuration lorsque l’échantillon est assez grand. Par sécurité, cette proposition ne diminue jamais les seuils du fichier de configuration utilisé pour lancer l’évaluation ; elle peut uniquement les maintenir ou les rendre plus stricts.

Pour une calibration crédible, utiliser idéalement plusieurs centaines de lignes iRaiser réelles, incluant des erreurs, des champs manquants, des noms fréquents et des cas ambigus. Ne pas calibrer sur le fichier BCE lui-même : les données y sont trop propres et produiraient des seuils artificiellement optimistes.

## 6. Logique du moteur

1. Les noms sont passés en minuscules, désaccentués et débarrassés de la ponctuation et des formes juridiques (`SRL`, `SA`, `NV`, `BV`, `ASBL`, etc.).
2. Les rues, numéros, boîtes, codes postaux et villes sont normalisés séparément.
3. SQLite retrouve un nombre limité de candidats via plusieurs blocs : nom exact, code postal + numéro, code postal + rue, ville + rue, rue + numéro, préfixe de nom et recherche FTS5.
4. RapidFuzz calcule plusieurs similarités sur le nom et la rue.
5. Les champs discriminants ne reçoivent pas le même poids. Un code postal ou un numéro de rue contradictoire pénalise fortement le candidat.
6. Le meilleur candidat est comparé au deuxième. Un score élevé mais ex æquo n’est jamais automatique.
7. `MATCH_CERTAIN` exige un score élevé, un écart suffisant, l’absence de contradiction dure et une combinaison forte de preuves.

## 7. Commandes Makefile

Le Makefile regroupe la génération de la base Excel, le moteur de matching et le workflow Git du projet :

```bash
make install
make testq
make check-data
make run         # exécute table_gen.py
make index       # sièges sociaux uniquement
make index-full  # sièges + unités d’établissement
make match
make evaluate
make inspect
```

Les chemins peuvent être remplacés à l’appel :

```bash
make match IRAISER_INPUT=data/mon_export.xlsx MATCH_OUTPUT=output/mon_resultat.xlsx
```

Pour le workflow Git établi du dépôt, le message se passe avec la variable `m` :

```bash
make status
make sync m="Ajout du moteur de rapprochement iRaiser BCE"
```

## 8. Limites assumées

- Le score par défaut est une règle explicable, pas une probabilité calibrée.
- Une ligne ne contenant qu’un nom très générique ne sera pas automatiquement attribuée.
- Les sociétés domiciliées à la même adresse peuvent rester ambiguës.
- La qualité finale dépend de la couverture de la base BCE et du choix des seuils évalués sur des données iRaiser réelles.
- Le programme ne modifie jamais iRaiser directement. Il produit d’abord des fichiers auditables pour éviter les écritures incorrectes.
