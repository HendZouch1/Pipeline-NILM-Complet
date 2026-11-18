"""
Étape 5 du pipeline : calcul des métriques de performance NILM
en comparant desaggregated.csv (retour de la carte) et ground_truth.csv (référence PC).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from config import ON_OFF_THRESHOLD_W

# En dessous de cette énergie totale réelle (en Watts cumulés sur la fenêtre),
# l'appareil est considéré comme "quasiment jamais actif" : EN% n'a alors aucun
# sens (division par ~0 -> pourcentages négatifs absurdes type -55616%) et
# Precision/Recall/F1 n'ont rien à mesurer. On rapporte N/A plutôt que des
# chiffres trompeurs.
MIN_TRUE_ENERGY_FOR_EN = 1.0


def load_thresholds(normalization_json):
    """
    Charge les seuils ON/OFF PAR APPAREIL depuis le JSON de normalisation
    (clé 'thresholds_w'). Bien plus juste qu'un seuil global unique : un
    seuil de 15 W rendrait 'playstation' (max 2 W) ou 'router' (max 7 W)
    structurellement indétectables, avec un F1 de 0 qui ne reflète pas la
    qualité réelle du modèle mais juste un seuil inadapté.
    """
    if not normalization_json:
        return None
    path = Path(normalization_json)
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    return data.get("thresholds_w")


def compute_metrics(desaggregated_csv, ground_truth_csv, threshold_w=ON_OFF_THRESHOLD_W,
                     normalization_json=None, log=print):
    pred_df = pd.read_csv(desaggregated_csv)
    true_df = pd.read_csv(ground_truth_csv)

    per_appliance_thresholds = load_thresholds(normalization_json)
    if per_appliance_thresholds:
        log(f"[METRICS] Seuils ON/OFF par appareil chargés depuis le JSON "
            f"({len(per_appliance_thresholds)} appareils)")
    else:
        log(f"[METRICS] Pas de JSON de seuils fourni -> seuil global unique de "
            f"{threshold_w} W pour tous les appareils (peut fausser P/R/F1 des "
            f"appareils de faible puissance)")

    common_cols = [c for c in true_df.columns if c in pred_df.columns]
    if not common_cols:
        raise ValueError("Aucune colonne commune entre desaggregated.csv et ground_truth.csv "
                          "(vérifiez que les noms d'appareils correspondent).")

    n = min(len(pred_df), len(true_df))
    pred_df, true_df = pred_df.iloc[:n], true_df.iloc[:n]

    rows = {}
    never_on = []
    for appliance in common_cols:
        y_pred = pred_df[appliance].to_numpy(dtype=float)
        y_true = true_df[appliance].to_numpy(dtype=float)

        valid = ~(np.isnan(y_pred) | np.isnan(y_true))
        n_excluded = int((~valid).sum())
        y_pred_valid, y_true_valid = y_pred[valid], y_true[valid]

        # Seuil spécifique à cet appareil si disponible, sinon seuil global
        thr = (per_appliance_thresholds or {}).get(appliance, threshold_w)

        if len(y_pred_valid) == 0:
            rmse = mae = en_pct = float("nan")
            precision = recall = f1 = float("nan")
        else:
            rmse = float(np.sqrt(np.mean((y_pred_valid - y_true_valid) ** 2)))
            mae = float(np.mean(np.abs(y_pred_valid - y_true_valid)))

            total_true_energy = float(np.sum(y_true_valid))
            if total_true_energy >= MIN_TRUE_ENERGY_FOR_EN:
                en_pct = float(1 - (np.sum(np.abs(y_pred_valid - y_true_valid))
                                     / (2 * total_true_energy))) * 100
            else:
                # Appareil quasiment jamais actif dans cette fenêtre : EN% non significatif
                en_pct = float("nan")

            true_state = y_true_valid > thr
            pred_state = y_pred_valid > thr
            tp = int(np.sum(true_state & pred_state))
            fp = int(np.sum(~true_state & pred_state))
            fn = int(np.sum(true_state & ~pred_state))

            if not true_state.any():
                # Aucune activation réelle dans la fenêtre -> Recall/F1 indéfinis.
                # Les rapporter à 0.00 laisserait croire à un échec du modèle,
                # alors qu'il n'y avait simplement rien à détecter.
                never_on.append(appliance)
                precision = float("nan") if not pred_state.any() else 0.0
                recall = float("nan")
                f1 = float("nan")
            else:
                precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
                recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
                f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

        def _r(v, nd=3):
            return round(v, nd) if not (isinstance(v, float) and np.isnan(v)) else float("nan")

        rows[appliance] = {
            "RMSE (W)": _r(rmse),
            "MAE (W)": _r(mae),
            "EN%": _r(en_pct, 2),
            "Precision": _r(precision),
            "Recall": _r(recall),
            "F1": _r(f1),
            "Seuil (W)": thr,
        }
        excl = f" ({n_excluded} exclu(s))" if n_excluded else ""
        note = "  [jamais actif dans cette fenêtre]" if appliance in never_on else ""
        log(f"[METRICS] {appliance:<15} RMSE={rmse:7.2f}W  MAE={mae:7.2f}W  "
            f"EN%={en_pct:7.1f}%  P={precision:.2f}  R={recall:.2f}  F1={f1:.2f}  "
            f"(seuil={thr}W){excl}{note}")

    if never_on:
        log(f"[METRICS] Note : {len(never_on)} appareil(s) jamais actif(s) dans cette "
            f"fenêtre de test ({', '.join(never_on)}) -> P/R/F1 non applicables. "
            f"Augmentez le nombre d'échantillons pour les évaluer.")

    return pd.DataFrame(rows).T
