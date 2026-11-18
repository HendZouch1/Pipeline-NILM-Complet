"""
Étape 6 du pipeline : logique de décision (SI/SINON) du cortex cible en fonction :
  - du format du modèle (.elf, .tflite, .onnx, ...)
  - de la taille du modèle
  - des capacités réelles de la carte choisie (voir config.BOARD_DB)

Lève une exception explicite si aucun cortex de la carte n'est compatible
(c'est le "est-ce possible sur cette carte ou non").
"""
from pathlib import Path
from config import BOARD_DB, MEMORY_SAFETY_MARGIN


def detect_elf_arch(elf_path):
    """Vérifie que le .elf est bien compilé pour ARM en lisant l'en-tête ELF (e_machine)."""
    with open(elf_path, "rb") as f:
        header = f.read(20)
    if header[:4] != b"\x7fELF":
        raise ValueError("Fichier .elf invalide (magic number absent).")
    endian = "little" if header[5] == 1 else "big"
    e_machine = int.from_bytes(header[18:20], byteorder=endian)
    if e_machine != 40:  # EM_ARM
        raise ValueError("Le .elf fourni n'est pas compilé pour une architecture ARM.")
    return "ARM"


def select_cortex(model_path, board_name, model_size_kb=None, elf_target_hint=None, log=print):
    """
    elf_target_hint : force le cortex cible ('M4' ou 'A7'), quel que soit le format.
    - Pour un .elf, c'est OBLIGATOIRE en pratique (l'en-tête seul ne distingue pas
      Cortex-A de Cortex-M au sein d'ARM).
    - Pour un .tflite, c'est OPTIONNEL : sans lui, la décision se fait par la
      taille (tient dans la Flash M4 ou non). Précisez 'A7' pour forcer
      l'exécution via tflite-runtime même si le modèle tiendrait sur M4 (utile
      si aucun firmware M4/TFLite-Micro compilé n'existe encore pour ce modèle).
    """
    model_path = Path(model_path)
    ext = model_path.suffix.lower()

    if board_name not in BOARD_DB:
        raise ValueError(f"Carte inconnue : {board_name}. Cartes disponibles : {list(BOARD_DB)}")

    board = BOARD_DB[board_name]
    if model_size_kb is None:
        model_size_kb = model_path.stat().st_size / 1024

    log(f"[CORTEX] Modèle={model_path.name} | format={ext} | taille={model_size_kb:.1f} Ko | carte={board_name}")

    # ---------------------------------------------------------------
    # SI le modèle est un .elf déjà compilé (donc déjà lié à un cortex)
    # ---------------------------------------------------------------
    if ext == ".elf":
        detect_elf_arch(model_path)
        if elf_target_hint and elf_target_hint.upper() in ("M4", "CORTEX-M4"):
            target = "Cortex-M4"
        elif elf_target_hint and elf_target_hint.upper() in ("M7", "CORTEX-M7"):
            target = "Cortex-M7"
        elif elf_target_hint and elf_target_hint.upper() in ("A7", "CORTEX-A7"):
            target = "Cortex-A7"
        elif len(board["cortexes"]) == 1:
            # Une seule option possible sur cette carte (ex: STM32F746G-DISCO,
            # M7 seul) -> pas besoin de deviner, c'est forcément celui-là.
            target = board["cortexes"][0]
            log(f"[CORTEX] Un seul cortex disponible sur {board_name} -> {target} "
                f"(pas d'ambiguïté possible)")
        else:
            # Carte multi-cortex sans indication -> Cortex-A7 par défaut
            # (comportement historique, à préciser explicitement si besoin).
            target = "Cortex-A7"

        if target not in board["cortexes"]:
            raise RuntimeError(f"Cortex {target} indisponible sur {board_name} "
                                f"(cortexes dispo : {board['cortexes']}).")
        log(f"[CORTEX] .elf -> exécution native directe sur {target}")
        return target

    # ---------------------------------------------------------------
    # SINON SI c'est un .tflite : on regarde si ça tient sur le M4, sinon repli A7
    # ---------------------------------------------------------------
    elif ext == ".tflite":
        forced = None
        if elf_target_hint and elf_target_hint.upper() in ("M4", "CORTEX-M4"):
            forced = "Cortex-M4"
        elif elf_target_hint and elf_target_hint.upper() in ("A7", "CORTEX-A7"):
            forced = "Cortex-A7"

        if forced:
            if forced not in board["cortexes"]:
                raise RuntimeError(f"Cortex {forced} indisponible sur {board_name}.")
            log(f"[CORTEX] .tflite -> cortex forcé explicitement : {forced}")
            return forced

        fits_on_m4 = (
            "Cortex-M4" in board["cortexes"]
            and model_size_kb <= board.get("m4_flash_kb", 0) * MEMORY_SAFETY_MARGIN
        )
        if fits_on_m4:
            limit = board["m4_flash_kb"] * MEMORY_SAFETY_MARGIN
            log(f"[CORTEX] .tflite tient dans la Flash M4 ({model_size_kb:.1f} Ko <= {limit:.1f} Ko) "
                f"-> Cortex-M4 (TFLite-Micro). Note : nécessite un firmware M4 compilé pour ce "
                f"modèle -- si vous n'en avez pas, forcez 'A7' explicitement.")
            return "Cortex-M4"
        elif "Cortex-A7" in board["cortexes"] and "tflite-runtime" in board.get("a7_runtimes", []):
            log("[CORTEX] .tflite trop volumineux pour le M4 -> repli sur Cortex-A7 (tflite-runtime, Linux)")
            return "Cortex-A7"
        else:
            raise RuntimeError(f"Aucun cortex de {board_name} ne peut exécuter ce modèle .tflite "
                                f"(trop grand pour M4, pas de Linux/tflite-runtime disponible).")

    # ---------------------------------------------------------------
    # SINON SI c'est un .onnx : nécessite obligatoirement un Cortex-A avec Linux
    # ---------------------------------------------------------------
    elif ext == ".onnx":
        if "Cortex-A7" in board.get("cortexes", []) and "onnxruntime" in board.get("a7_runtimes", []):
            log("[CORTEX] .onnx -> Cortex-A7 (onnxruntime sous Linux). ONNX non supporté sur Cortex-M4.")
            return "Cortex-A7"
        else:
            raise RuntimeError(f"{board_name} ne dispose pas d'un Cortex-A7 + runtime ONNX : "
                                f"modèle .onnx impossible à exécuter sur cette carte.")

    # ---------------------------------------------------------------
    # SINON : format non géré
    # ---------------------------------------------------------------
    else:
        raise ValueError(f"Format de modèle non supporté : {ext} "
                          f"(formats acceptés : .elf, .tflite, .onnx)")
