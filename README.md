# Banc de test automatisé NILM sur cartes STM32

Pipeline complet : dataset + modèle (`.elf` / `.tflite` / `.onnx`) → désagrégation sur carte → métriques.

## Installation

```bash
pip install -r requirements.txt
```

## Architecture (correspond aux 7 étapes demandées)

| Fichier                    | Rôle |
|----------------------------|------|
| `config.py`                | Étape 6 (partiel) : base de données des cartes (RAM/Flash/cortexes/connexion). **À adapter avec vos vraies specs et l'IP réelle de la carte.** |
| `dataset_splitter.py`      | Étape 2 : split dataset → `aggregate.bin` + `ground_truth.csv` |
| `cortex_selector.py`       | Étape 6 : logique SI/SINON qui choisit Cortex-A7 ou Cortex-M4 selon le format/la taille du modèle et vérifie la faisabilité sur la carte |
| `board_communicator.py`    | Étapes 3-4 : envoi SSH/SCP vers la carte, exécution, rapatriement de `desaggregated.csv` |
| `metrics.py`                | Étape 5 : RMSE, MAE, EN%, Precision, Recall, F1 |
| `pipeline_orchestrator.py` | Enchaîne tout, avec callbacks de log/progression pour l'interface |
| `gui_app.py`               | Étape 7 : interface Tkinter qui montre chaque étape en direct |
| `main.py`                  | Point d'entrée CLI (`--dataset --model --board`) ou GUI (`--gui`) |

## Utilisation

**Interface graphique :**
```bash
python main.py --gui
```

**Ligne de commande (pour scripts/CI) :**
```bash
python main.py --dataset data/house1.csv --model models/lstm.tflite --board STM32MP157F-DK2
```

## Points à adapter avant un test réel sur votre STM32MP157F-DK2

1. **`config.py` → `connection`** : mettez la vraie IP de la carte (par défaut `192.168.7.1` sur
   l'interface USB-Ethernet ST, à vérifier avec `ip a` sur la carte), l'utilisateur SSH, et le
   dossier de travail distant.
2. **Authentification SSH** : le code utilise l'auto-accept de clé hôte et suppose un accès sans
   mot de passe (clé SSH) ou root sans mot de passe (config par défaut Yocto/OpenSTLinux en dev).
   Ajoutez `password=...` ou `key_filename=...` dans `BoardCommunicator.connect()` selon votre setup.
3. **Exécutable côté carte** : votre `.elf`/exécutable doit lire `aggregate.bin` (tableau float32
   brut) et écrire `desaggregated.csv` (une colonne par appareil, même ordre que le dataset
   d'entrée) — c'est le contrat d'interface entre le PC et la carte.
4. **Cortex-M4** : le bloc `run_inference` pour le M4 utilise le mécanisme `remoteproc` du noyau
   Linux A7 (chargement firmware + `rpmsg`). C'est un squelette à adapter précisément à votre
   firmware M4 réel (nom du device remoteproc, driver rpmsg utilisé, etc.).
5. **Format du dataset** : CSV avec une colonne `aggregate` + une colonne par appareil
   (ex: format REDD/UK-DALE déjà agrégé en CSV). Adaptez `dataset_splitter.py` si votre format
   source diffère (HDF5, plusieurs fichiers, etc.).

## Ajouter une nouvelle carte STM32

Ajoutez simplement une entrée dans `BOARD_DB` (`config.py`) avec ses cortexes disponibles,
ses tailles mémoire, ses runtimes supportés, et son type de connexion (`ssh` ou `serial`).
Aucun autre fichier n'a besoin d'être modifié : le pipeline est générique.

## Test sans matériel (déjà validé dans ce livrable)

`dataset_splitter.py`, `cortex_selector.py` et `metrics.py` ont été testés avec un dataset
synthétique et fonctionnent de bout en bout. Seule la communication SSH réelle
(`board_communicator.py`) nécessite la carte physique pour être testée.
