"""
Orchestrateur du banc de test automatisé : enchaîne les étapes 1 à 5
pour n'importe quel triplet (dataset, modèle, carte).
"""
from pathlib import Path
from dataset_splitter import split_dataset
from cortex_selector import select_cortex
from board_communicator import BoardCommunicator
from serial_communicator import run_serial_inference
from metrics import compute_metrics
from m4_packaging import pack_ukdale_bin, convert_m4_output_to_wide
from config import BOARD_DB

# Script d'inférence .tflite générique, fourni avec ce projet (pas à sélectionner
# par l'utilisateur -- contrairement à ukdale_m4.sh qui est spécifique à chaque
# firmware M4, ce script s'adapte automatiquement à n'importe quel .tflite seq2point).
TFLITE_INFER_SCRIPT = Path(__file__).parent / "board_scripts" / "tflite_infer.py"


def run_pipeline(dataset_csv, model_path, board_name, workdir="./run_output",
                  elf_target_hint=None, log=print, progress_cb=None,
                  cleanup_board=True, confirm_m4=False, launch_script=None,
                  m4_max_samples=0, normalization_json=None, a7_max_samples=0,
                  serial_max_samples=0):
    """
    cleanup_board       : si True (par défaut), supprime tout ce qui a été déposé
                          sur la carte après le test (cartes SSH uniquement).
    confirm_m4          : doit être explicitement True pour Cortex-M4.
    launch_script       : chemin LOCAL vers ukdale_m4.sh (ou équivalent). Requis
                          si le cortex choisi est Cortex-M4 (modèles .elf M4).
    m4_max_samples      : nombre d'échantillons à traiter sur M4 (0 = tout).
    normalization_json  : pour un modèle .tflite sur Cortex-A7, JSON contenant
                          aggregate_mean/std + appliance_scales + appliances.
    a7_max_samples      : nombre d'échantillons pour un .tflite sur Cortex-A7.
    serial_max_samples  : nombre d'échantillons pour une carte Cortex-M pure
                          (connexion série, ex: STM32F746G-DISCO). 0 = tout.
    """
    def step(i, label):
        if progress_cb:
            progress_cb(i, 5, label)

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    step(1, "Lecture des entrées")
    log("========== ETAPE 1/5 : Entrées ==========")
    log(f"Dataset : {dataset_csv}")
    log(f"Modèle  : {model_path}")
    log(f"Carte   : {board_name}")

    step(2, "Split dataset -> aggregate.bin + ground_truth.csv")
    log("========== ETAPE 2/5 : Split dataset (sur le PC) ==========")
    split_info = split_dataset(dataset_csv, workdir, log=log)

    step(3, "Sélection du cortex cible (si/sinon)")
    log("========== ETAPE 3/5 : Sélection du cortex ==========")
    cortex = select_cortex(model_path, board_name, elf_target_hint=elf_target_hint, log=log)

    if cortex == "Cortex-M4" and not launch_script:
        raise RuntimeError(
            "Le modèle nécessite Cortex-M4 mais aucun script de lancement M4 "
            "(ex: ukdale_m4.sh) n'a été fourni."
        )

    model_ext = Path(model_path).suffix.lower()
    connection_type = BOARD_DB[board_name]["connection"].get("type", "ssh")

    step(4, f"Transfert + exécution sur {board_name} ({cortex})")
    log("========== ETAPE 4/5 : Transfert + exécution sur la carte ==========")

    if connection_type == "serial":
        # Carte Cortex-M pure (pas de Linux/SSH) : le .elf est déjà flashé
        # manuellement au préalable, le pipeline dialogue juste par le port
        # série avec le firmware déjà en place. Rien à transférer, rien à
        # nettoyer après coup.
        conn = BOARD_DB[board_name]["connection"]
        log(f"[SERIAL] Carte série détectée -> communication directe via {conn['port']} "
            f"(le .elf doit déjà être flashé manuellement sur la carte)")
        desagg_csv = run_serial_inference(
            port=conn["port"], baudrate=conn.get("baudrate", 115200),
            aggregate_bin_path=split_info["aggregate_bin"],
            output_csv_path=workdir / "desaggregated.csv",
            appliances=split_info["appliances"],
            max_samples=serial_max_samples, log=log,
        )

    else:
        # Cartes Linux (SSH/SCP), ex: STM32MP157F-DK2
        board_cfg = BOARD_DB[board_name]["connection"]
        comm = BoardCommunicator(board_cfg, log=log)
        elf_name = None
        try:
            comm.connect()

            if cortex == "Cortex-A7" and model_ext == ".tflite":
                model_name, script_name, norm_name = comm.send_files_a7_tflite(
                    split_info["aggregate_bin"], model_path, TFLITE_INFER_SCRIPT,
                    normalization_json_path=normalization_json,
                )
                remote_csv = comm.run_inference_a7_tflite(
                    model_name, script_name, norm_name=norm_name, max_samples=a7_max_samples,
                )
                desagg_csv = comm.fetch_result(remote_csv, workdir / "desaggregated.csv")

            elif cortex == "Cortex-A7":  # .elf natif Linux
                exec_name = comm.send_files_a7(split_info["aggregate_bin"], model_path)
                remote_csv = comm.run_inference_a7(exec_name)
                desagg_csv = comm.fetch_result(remote_csv, workdir / "desaggregated.csv")

            else:  # Cortex-M4
                import numpy as np
                aggregate_array = np.fromfile(split_info["aggregate_bin"], dtype="<f4")
                ukdale_bin_path = workdir / "ukdale.bin"
                pack_ukdale_bin(aggregate_array, ukdale_bin_path, log=log)

                elf_name, script_name = comm.send_files_m4(ukdale_bin_path, model_path, launch_script)
                remote_csv = comm.run_inference_m4(elf_name, script_name, confirm_m4=confirm_m4,
                                                    max_samples=m4_max_samples)
                raw_m4_csv = comm.fetch_result(remote_csv, workdir / "disaggregated_raw.csv")
                desagg_csv = convert_m4_output_to_wide(
                    raw_m4_csv, split_info["appliances"],
                    output_path=workdir / "desaggregated.csv", log=log,
                )
                desagg_csv = workdir / "desaggregated.csv"

            if cleanup_board:
                comm.cleanup_remote(elf_name=elf_name if cortex == "Cortex-M4" else None)
        finally:
            comm.close()

    step(5, "Calcul des métriques")
    log("========== ETAPE 5/5 : Calcul des métriques (sur le PC) ==========")
    metrics_df = compute_metrics(desagg_csv, split_info["ground_truth_csv"],
                                  normalization_json=normalization_json, log=log)
    metrics_df.to_csv(workdir / "metrics_report.csv")
    log(f"Rapport de métriques sauvegardé -> {workdir / 'metrics_report.csv'}")

    return {
        "cortex_used": cortex,
        "metrics": metrics_df,
        "outputs_dir": workdir,
        "n_samples": split_info["n_samples"],
        "appliances": split_info["appliances"],
    }
