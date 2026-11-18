"""
Adaptateur spécifique au protocole de ukdale_m4.sh / votre main.c Cortex-M4 :
  - pack_ukdale_bin()          : construit ukdale.bin (en-tête 32 octets 'UKDL' + floats)
  - convert_m4_output_to_wide(): reconvertit disaggregated.csv (format JSON par ligne)
                                  en un DataFrame "une colonne par appareil", pour
                                  pouvoir réutiliser metrics.py sans le modifier.
"""
import json
import struct
from pathlib import Path

import numpy as np
import pandas as pd

UKDL_MAGIC = b"UKDL"
HEADER_SIZE = 32


def pack_ukdale_bin(aggregate_array, output_path, extra_header_bytes=None, log=print):
    """
    ATTENTION - à confirmer avec vous : le contenu exact des 28 octets restants de
    l'en-tête (après le magic 'UKDL') dépend du parsing fait côté main.c sur le M4.
    Ce script les remplit de zéros par défaut (padding neutre). Si votre firmware
    attend un champ précis dans cet en-tête (nombre d'échantillons, version,
    fréquence d'échantillonnage...), passez-le via extra_header_bytes -- sinon il
    faut me montrer le code de parsing du header dans main.c pour que je le génère
    correctement ici.
    """
    output_path = Path(output_path)
    header = bytearray(HEADER_SIZE)
    header[:4] = UKDL_MAGIC
    if extra_header_bytes:
        header[4:4 + len(extra_header_bytes)] = extra_header_bytes

    aggregate_array = np.asarray(aggregate_array, dtype="<f4")
    with open(output_path, "wb") as f:
        f.write(bytes(header))
        aggregate_array.tofile(f)

    log(f"[M4-PACK] ukdale.bin généré : en-tête 32o (magic 'UKDL') + "
        f"{aggregate_array.size} échantillons float32 -> {output_path}")
    return output_path


def convert_m4_output_to_wide(m4_csv_path, appliances, output_path=None, log=print):
    """
    Le CSV produit par ukdale_m4.sh a une colonne 'appliances_json' contenant par
    exemple '{"kettle": 2944.0, "fridge": 80.5}'. On l'éclate en une colonne par
    appareil pour correspondre au format attendu par metrics.py / ground_truth.csv.
    """
    m4_csv_path = Path(m4_csv_path)
    df = pd.read_csv(m4_csv_path)

    rows = []
    n_no_response = 0
    for _, row in df.iterrows():
        raw = row.get("appliances_json", "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {}
        if row.get("status") != "RESULT":
            n_no_response += 1
        rows.append({app: parsed.get(app, float("nan")) for app in appliances})

    wide = pd.DataFrame(rows)

    if n_no_response:
        log(f"[M4-PACK] Attention : {n_no_response}/{len(df)} échantillons sans "
            f"réponse RESULT (status WAIT/ERROR/NO_RESPONSE) -> valeurs NaN")

    if output_path:
        output_path = Path(output_path)
        wide.to_csv(output_path, index=False)
        log(f"[M4-PACK] Sortie M4 reconvertie au format 'une colonne par appareil' -> {output_path}")

    return wide
