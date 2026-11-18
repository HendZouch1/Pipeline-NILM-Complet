"""
Point d'entrée du banc de test automatisé NILM sur cartes STM32.

Usage GUI (recommandé) :
    python main.py --gui

Usage CLI (pour scripter / intégrer dans CI) :
    python main.py --dataset data/house1.csv --model models/lstm.tflite --board STM32MP157F-DK2

Exemple pour un modèle Cortex-M4 (ex: ukdale.elf + ukdale_m4.sh) :
    python main.py --dataset data/house2.csv --model models/ukdale.elf --board STM32MP157F-DK2 \
        --elf-target M4 --m4-script scripts/ukdale_m4.sh --confirm-m4 --m4-max-samples 0
"""
import argparse
from pipeline_orchestrator import run_pipeline
from config import BOARD_DB


def main():
    parser = argparse.ArgumentParser(description="Banc de test automatisé NILM sur cartes STM32")
    parser.add_argument("--gui", action="store_true", help="Lance l'interface graphique")
    parser.add_argument("--dataset", help="Chemin vers le dataset CSV (colonne 'aggregate' + appareils)")
    parser.add_argument("--model", help="Chemin vers le modèle (.elf, .tflite, .onnx)")
    parser.add_argument("--board", choices=list(BOARD_DB.keys()), help="Carte STM32 cible")
    parser.add_argument("--elf-target", choices=["A7", "M4", "M7"], default=None,
                         help="Si le modèle est un .elf, précise le cortex visé "
                              "(auto-détecté si la carte n'a qu'un seul cortex)")
    parser.add_argument("--workdir", default="./run_output", help="Dossier de sortie")
    parser.add_argument("--keep-board-files", action="store_true",
                         help="Ne pas supprimer les fichiers déposés sur la carte après le test "
                              "(par défaut ils sont supprimés)")
    parser.add_argument("--confirm-m4", action="store_true",
                         help="Autorise explicitement l'exécution sur Cortex-M4 "
                              "(copie le firmware dans /lib/firmware et recharge le M4)")
    parser.add_argument("--m4-script", default=None,
                         help="Script shell local (ex: ukdale_m4.sh). Obligatoire si le "
                              "modèle tourne sur Cortex-M4.")
    parser.add_argument("--m4-max-samples", type=int, default=0,
                         help="Nombre d'échantillons à traiter sur M4 (0 = tout le dataset, "
                              "par défaut le script ukdale_m4.sh se limite sinon à 1000)")
    parser.add_argument("--normalization-json", default=None,
                         help="Pour un modèle .tflite sur Cortex-A7 : JSON de normalisation "
                              "(aggregate_mean/std + appliance_scales). Recommandé.")
    parser.add_argument("--a7-max-samples", type=int, default=1000,
                         help="Nombre d'échantillons à traiter pour un .tflite sur Cortex-A7 "
                              "(0 = tout le dataset ; peut être lent, l'inférence tourne "
                              "échantillon par échantillon)")
    parser.add_argument("--serial-max-samples", type=int, default=1000,
                         help="Nombre d'échantillons à traiter pour une carte Cortex-M pure "
                              "sans Linux, connectée en série (ex: STM32F746G-DISCO). "
                              "Le .elf doit déjà être flashé manuellement au préalable.")
    args = parser.parse_args()

    if args.gui:
        from gui_app import PipelineGUI
        PipelineGUI().mainloop()
        return

    if not (args.dataset and args.model and args.board):
        parser.error("En mode CLI, --dataset, --model et --board sont obligatoires "
                     "(ou utilisez --gui).")

    result = run_pipeline(
        dataset_csv=args.dataset,
        model_path=args.model,
        board_name=args.board,
        workdir=args.workdir,
        elf_target_hint=args.elf_target,
        cleanup_board=not args.keep_board_files,
        confirm_m4=args.confirm_m4,
        launch_script=args.m4_script,
        m4_max_samples=args.m4_max_samples,
        normalization_json=args.normalization_json,
        a7_max_samples=args.a7_max_samples,
        serial_max_samples=args.serial_max_samples,
    )

    print("\n=== RÉSULTAT FINAL ===")
    print(f"Cortex utilisé : {result['cortex_used']}")
    print(result["metrics"])


if __name__ == "__main__":
    main()
