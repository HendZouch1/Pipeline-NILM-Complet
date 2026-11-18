"""
Configuration centrale du banc de test NILM sur cartes STM32.
Ajoutez une entrée dans BOARD_DB pour chaque nouvelle carte que vous voulez tester.
"""

BOARD_DB = {
    "STM32MP157F-DK2": {
        "cortexes": ["Cortex-A7", "Cortex-M4"],
        "a7_ram_mb": 512,
        "a7_has_linux": True,
        "a7_runtimes": ["onnxruntime", "tflite-runtime", "elf-native"],
        "m4_flash_kb": 384,
        "m4_ram_kb": 64,
        "m4_runtimes": ["tflite-micro", "elf-baremetal(X-CUBE-AI)"],
        "connection": {
            "type": "ssh",
            # IP par défaut de l'interface USB Ethernet ST : 192.168.7.1
            # OU l'IP Wi-Fi de la carte une fois connectée à votre réseau
            # (voir "ip addr show wlan0" sur la carte après nmcli device wifi connect) :
            "host": "192.168.32.4",   # <-- remplacez par l'IP Wi-Fi si vous utilisez le Wi-Fi
            "user": "root",
            "port": 22,
            "remote_workdir": "/home/root/nilm_bench",
        },
    },

    # STM32F746G-DISCO : Cortex-M7 seul, pas de Linux -> communication série,
    # pas SSH. Contrairement à la DK2 (chargement M4 en RAM par remoteproc),
    # ici le .elf est écrit en FLASH INTERNE via la sonde ST-LINK/SWD -- il
    # persiste après reset/coupure d'alimentation. Le pipeline flashe donc
    # automatiquement le firmware à tester AVANT le run série, puis restaure
    # le firmware ST d'origine APRES (voir 'programmer' ci-dessous et
    # stm32_flasher.py).
    "STM32F746G-DISCO": {
        "cortexes": ["Cortex-M7"],
        "m7_flash_kb": 1024,   # 1 Mo de Flash sur le F746
        "m7_ram_kb": 320,      # 320 Ko de RAM (dont DTCM/ITCM)
        "connection": {
            "type": "serial",
            # Port COM du ST-LINK virtuel -- À ADAPTER (vérifiez dans le
            # Gestionnaire de périphériques Windows : "STMicroelectronics
            # STLink Virtual COM Port (COMx)")
            "port": "COM3",
            "baudrate": 115200,
            # Protocole texte ASCII ligne par ligne, identique en principe à
            # celui déjà utilisé dans ukdale_m4.sh (PING/PONG, puis un float
            # par ligne, réponse RESULT/WAIT/ERR) -- À CONFIRMER/AJUSTER
            # avec le vrai comportement de sshmm_M7.elf.
            "protocol": "ascii_line",
        },
        "programmer": {
            # Chemin vers STM32_Programmer_CLI.exe (installé avec
            # STM32CubeProgrammer, ou fourni avec STM32CubeIDE sous
            # .../STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools
            # .cubeprogrammer.win32_x.x.x.xxxxxxxx/tools/bin/).
            # À ADAPTER selon votre installation :
            "cli_path": r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32CubeProgrammer.exe",
            "swd_frequency": 4000,  # kHz
            # IMPORTANT : chemin vers UNE COPIE LOCALE du firmware ST
            # d'origine (celui déjà flashé aujourd'hui sur la carte).
            # Faites-en la sauvegarde UNE FOIS avec le bouton GUI dédié
            # (stm32_flasher.dump_current_firmware), ou fournissez
            # directement le .elf source si vous l'avez déjà.
            # Tant que ce chemin n'existe pas, le pipeline refusera de
            # lancer un test sur cette carte (sécurité : pas de flash de
            # test sans pouvoir restaurer ensuite).
            "original_firmware_path": r"C:\firmwares\firmware_ST_original.elf",
            # Délai (s) laissé à la carte pour redémarrer et réénumérer son
            # port COM virtuel après un flash (-rst redémarre le CPU
            # immédiatement, mais l'énumération USB peut prendre un instant).
            "boot_delay_s": 3,
        },
    },
}

# Seuil de détection ON/OFF pour Recall/Precision/F1 (en Watts)
ON_OFF_THRESHOLD_W = 15.0

# Marge de sécurité mémoire pour décider si un modèle .tflite tient sur le Cortex-M4
MEMORY_SAFETY_MARGIN = 0.7
