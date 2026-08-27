# Validation technique

## Tests automatisés

Commande :

```bash
python -m pytest -q
```

Résultat final : `4 passed`.

Les tests couvrent :

1. la normalisation des formes juridiques, accents, rues concaténées et adresses complètes ;
2. le matching avec fautes, le rejet d’un cas ex æquo et le traitement d’un numéro déjà présent ;
3. la validation de la clé de contrôle d’un numéro belge et le rematching d’un numéro invalide ;
4. l’indexation d’adresses d’unités d’établissement (`BAET`) et des variantes FR/NL ;
5. la lecture complète d’un fichier Excel de référence réparti sur plusieurs feuilles.

## Construction sur l’ensemble des CSV BCE fournis

La commande `build-index` a été exécutée sur l’ensemble des fichiers BCE du projet, avec les sièges sociaux actuels (`REGO`) et sans les adresses historiques.

L’index SQLite obtenu contenait :

| Élément | Nombre |
|---|---:|
| Entreprises | 1 951 608 |
| Variantes de noms | 2 054 314 |
| Variantes linguistiques d’adresses actuelles | 1 356 615 |
| Taille de l’index | environ 1,33 Go |
| Temps de construction observé dans l’environnement de test | environ 288 s |

Cette validation confirme que le script traite les fichiers BCE complets sans devoir charger l’ensemble de la base en mémoire. Le temps observé dépend naturellement du processeur, du disque et de la mémoire disponibles.

## Validation sur un extrait réel des CSV BCE

Un index de test reproductible a été construit à partir d’un extrait des fichiers fournis :

- 50 000 lignes de `enterprise.csv` ;
- 100 000 lignes de `denomination.csv` ;
- 100 000 lignes de `address.csv`.

Les cas manuels suivants ont notamment été retrouvés correctement :

- `Faryss` + `Strop straat` → `Farys`, `0200.068.636` ;
- `Sanatorium Hospitaal Lemberge` → `0200.171.970` ;
- `Association Intercommunale in BW` → `0200.362.210` ;
- `IDEA` → `0201.105.843` ;
- `Vlotter` → `0200.762.878`.

Le cas `Faryss` est également un test d’ambiguïté : `Farys Solar` existe à la même adresse. Le moteur retient `Farys` grâce à la meilleure concordance de nom et conserve le second candidat dans l’audit.

## Benchmark bruité de 1 000 lignes

Un jeu de 1 000 lignes a été généré à partir de l’extrait BCE réel, puis dégradé de façon déterministe : suppressions de caractères, accents retirés, espaces ajoutés dans les noms de rue néerlandais, champs manquants et quelques contradictions de numéro de rue.

Résultats avec la configuration par défaut :

| Mesure | Résultat |
|---|---:|
| Rappel du vrai numéro dans le top 5 | 100,0 % |
| Exactitude du premier candidat | 99,5 % |
| `MATCH_CERTAIN` | 774 lignes |
| Couverture des `MATCH_CERTAIN` | 77,4 % |
| Précision des `MATCH_CERTAIN` | 100,0 % |
| Faux `MATCH_CERTAIN` | 0 |
| `MATCH_PROBABLE` | 207 lignes |
| Couverture des `MATCH_PROBABLE` | 20,7 % |
| Précision des `MATCH_PROBABLE` | 100,0 % |
| `NO_RELIABLE_MATCH` | 19 lignes |

Le rapport de calibration a conservé les seuils par défaut : score certain `92`, écart certain `8`, score probable `72`, écart probable `3`.

## Interprétation et condition de mise en production

Ce benchmark vérifie le fonctionnement du pipeline et ses garde-fous, mais ne constitue pas une garantie de précision sur les données iRaiser. Les perturbations sont synthétiques et l’index de benchmark ne contenait que 50 000 entreprises.

Avant toute écriture automatique dans iRaiser, exécuter `evaluate` sur plusieurs centaines — idéalement plusieurs milliers — de lignes iRaiser réelles dont le numéro d’entreprise est déjà connu. La décision d’import automatique doit se fonder sur la précision réellement mesurée de `automatic_enterprise_number`, avec une attention particulière aux homonymes, sociétés domiciliées à la même adresse, changements d’adresse et champs manquants.
