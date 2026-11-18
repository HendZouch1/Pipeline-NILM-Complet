"""
Étape 2 du pipeline (exécutée sur le PC) : sépare le dataset brut en :
  - aggregate.bin    : signal agrégé, format binaire brut consommé par l'exécutable embarqué
  - ground_truth.csv : puissance de référence par appareil (pour le calcul des métriques)

Format d'entrée attendu : un CSV avec une colonne 'aggregate' et une colonne par appareil
(ex: dataset type REDD / UK-DALE déjà converti en CSV).
"""
import numpy as np
import pandas as pd
from pathlib import Path


def split_dataset(dataset_csv_path, output_dir, sample_dtype="float32", log=print):
    dataset_csv_path = Path(dataset_csv_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"[SPLIT] Lecture du dataset : {dataset_csv_path.name}")
    df = pd.read_csv(dataset_csv_path)

    if "aggregate" not in df.columns:
        raise ValueError("Le dataset doit contenir une colonne 'aggregate'.")

    appliance_cols = [c for c in df.columns if c != "aggregate"]
    if not appliance_cols:
        raise ValueError("Aucune colonne d'appareil trouvée pour construire le ground truth.")

    # aggregate.bin : tableau brut envoyé à la carte
    aggregate = df["aggregate"].to_numpy(dtype=sample_dtype)
    aggregate_bin_path = output_dir / "aggregate.bin"
    aggregate.tofile(aggregate_bin_path)
    log(f"[SPLIT] aggregate.bin généré ({aggregate.size} échantillons, dtype={sample_dtype})")

    # ground_truth.csv : puissance réelle par appareil, alignée sur les mêmes indices temporels
    ground_truth_path = output_dir / "ground_truth.csv"
    df[appliance_cols].to_csv(ground_truth_path, index=False)
    log(f"[SPLIT] ground_truth.csv généré ({len(appliance_cols)} appareils : {appliance_cols})")

    return {
        "aggregate_bin": aggregate_bin_path,
        "ground_truth_csv": ground_truth_path,
        "n_samples": aggregate.size,
        "appliances": appliance_cols,
    }
