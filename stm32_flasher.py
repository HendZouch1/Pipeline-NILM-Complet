"""
Flashage automatique du firmware sur cartes Cortex-M PURES (pas de Linux/remoteproc),
via la sonde ST-LINK intégrée et STM32CubeProgrammer en ligne de commande
(STM32_Programmer_CLI.exe).

Contrairement à la STM32MP157F-DK2 (Cortex-M4 chargé en RAM par remoteproc),
ici le .elf est écrit en Flash interne : il persiste après reset/coupure
d'alimentation tant qu'on ne reflashe pas autre chose. Il n'existe pas
d'équivalent simple à "cat /sys/class/remoteproc/.../firmware" pour savoir
quel firmware est actuellement en Flash -- la seule façon fiable de revenir
à l'état d'origine est de conserver une copie locale du firmware ST
(dump_current_firmware, à faire UNE FOIS avant le premier test) et de la
reflasher explicitement après chaque test.
"""
import subprocess
from pathlib import Path


def _run_cli(cli_path, args, timeout=60, log=print):
    cli_path = Path(cli_path)
    if not cli_path.exists():
        raise FileNotFoundError(
            f"STM32_Programmer_CLI.exe introuvable à : {cli_path}\n"
            f"Vérifiez le chemin 'cli_path' dans config.py (dépend de votre "
            f"installation de STM32CubeProgrammer ou STM32CubeIDE)."
        )
    cmd = [str(cli_path)] + args
    log(f"[FLASH] {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"STM32_Programmer_CLI n'a pas répondu après {timeout}s "
            f"(sonde ST-LINK débranchée ou carte non alimentée ?)."
        )
    if result.stdout.strip():
        log(f"[FLASH][stdout] {result.stdout.strip()}")
    if result.returncode != 0:
        raise RuntimeError(
            f"STM32_Programmer_CLI a échoué (code {result.returncode}) : "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def flash_firmware(cli_path, elf_path, swd_frequency=4000, log=print):
    """
    Flashe un .elf sur la carte via ST-LINK/SWD et redémarre le CPU (-rst).

    cli_path       : chemin vers STM32_Programmer_CLI.exe (voir config.py).
    elf_path       : chemin LOCAL du .elf à flasher (ex: sshmm_M7.elf, ou le
                     firmware ST d'origine pour la restauration).
    swd_frequency  : fréquence SWD en kHz (4000 = valeur sûre par défaut ;
                     augmentez si le flash est trop lent, réduisez si la
                     sonde/le câble donne des erreurs de connexion).
    """
    elf_path = Path(elf_path)
    if not elf_path.exists():
        raise FileNotFoundError(f"Firmware introuvable : {elf_path}")

    log(f"[FLASH] Flashage de {elf_path.name} via ST-LINK (SWD, {swd_frequency} kHz)...")
    _run_cli(
        cli_path,
        ["-c", "port=SWD", f"freq={swd_frequency}", "-w", str(elf_path), "-v", "-rst"],
        timeout=90, log=log,
    )
    log(f"[FLASH] {elf_path.name} flashé et carte redémarrée.")


def dump_current_firmware(cli_path, output_path, flash_base="0x08000000",
                           flash_size="0x100000", swd_frequency=4000, log=print):
    """
    Sauvegarde UNE FOIS le contenu Flash actuel en .bin brut, AVANT votre tout
    premier test automatisé -- pour pouvoir restaurer le firmware ST d'origine
    même si vous n'avez plus son .elf source.

    flash_size="0x100000" (1 Mo) = taille totale de la Flash interne du
    STM32F746NGH6 (carte DISCO) ; ajustez si votre variante diffère.

    ATTENTION : produit un .bin brut (adresse + octets), pas un .elf avec
    symboles -- suffisant pour reflasher/restaurer, mais pas pour du debug.
    """
    output_path = Path(output_path)
    log(f"[FLASH] Sauvegarde de la Flash actuelle ({flash_size} octets depuis {flash_base})...")
    _run_cli(
        cli_path,
        ["-c", "port=SWD", f"freq={swd_frequency}", "-u", flash_base, flash_size, str(output_path)],
        timeout=60, log=log,
    )
    log(f"[FLASH] Firmware ST d'origine sauvegardé -> {output_path}")
    return output_path
