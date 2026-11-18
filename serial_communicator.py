"""
Communication PC <-> carte via port série (UART), pour les cartes Cortex-M
PURES sans Linux (ex: STM32F746G-DISCO) -- pas de SSH/SCP possible ici.

Contrairement à la STM32MP157F-DK2, le firmware (.elf) n'est PAS envoyé par
le pipeline à chaque test : vous le flashez une fois, manuellement, via
STM32CubeIDE ou STM32CubeProgrammer. Le pipeline se contente ensuite de
dialoguer avec ce firmware déjà en place, via le port série.

Protocole supposé (ASCII ligne par ligne, à confirmer/ajuster selon votre
firmware réel -- inspiré du protocole déjà utilisé dans ukdale_m4.sh) :
  PC  -> carte : "PING\n"
  carte -> PC  : une ligne contenant "PONG ..." (confirme que le firmware
                 tourne et est prêt)
  Pour chaque échantillon :
  PC  -> carte : "<valeur_watts>\n"  (ex: "3022.500000\n")
  carte -> PC  : une ou plusieurs lignes, dont une commençant par "RESULT "
                 (avec les puissances par appareil, format "nom=valeur"),
                 "WAIT " (pas encore prêt, on relit), ou "ERR " (erreur).
"""
import csv
import json
import time

try:
    import serial
except ImportError:
    serial = None


def _parse_result_line(line, appliances):
    """
    Parse une ligne 'RESULT input=...W state=... predicted=...W kettle=2944W ...'.

    IMPORTANT : un appareil absent de la ligne signifie très probablement que le
    firmware l'a jugé ~0W (en dessous de son seuil de report interne, pour ne
    pas surcharger la ligne UART) -- PAS une donnée manquante. On le traite donc
    comme 0.0, pas comme NaN (qui devrait rester réservé aux échantillons pour
    lesquels aucun RESULT du tout n'a été reçu, ex: WAIT/ERR/timeout).
    """
    values = {}
    for token in line.split()[1:]:
        if "=" not in token:
            continue
        key, val = token.split("=", 1)
        if val.endswith("W"):
            val = val[:-1]
        try:
            values[key] = float(val)
        except ValueError:
            pass
    return {a: values.get(a, 0.0) for a in appliances}


def run_serial_inference(port, baudrate, aggregate_bin_path, output_csv_path,
                          appliances, max_samples=0, timeout=5.0, log=print):
    if serial is None:
        raise RuntimeError(
            "Le paquet 'pyserial' n'est pas installé sur le PC. "
            "Installez-le avec : pip install pyserial"
        )

    import numpy as np
    aggregate = np.fromfile(aggregate_bin_path, dtype="<f4")
    n_samples = len(aggregate)
    if max_samples and max_samples > 0:
        n_samples = min(n_samples, max_samples)
    log(f"[SERIAL] {len(aggregate)} échantillons dans aggregate.bin, "
        f"traitement de {n_samples} échantillon(s)")

    log(f"[SERIAL] Ouverture du port {port} @ {baudrate} bauds...")
    with serial.Serial(port, baudrate, timeout=timeout) as ser:
        time.sleep(2)  # laisse le temps au port de se stabiliser après ouverture
        ser.reset_input_buffer()

        log("[SERIAL] Test du protocole avec PING...")
        ser.write(b"PING\n")
        response = ser.readline().decode(errors="replace").strip()
        if "PONG" not in response:
            raise RuntimeError(
                f"Le firmware ne répond pas comme attendu à PING (reçu : "
                f"'{response}'). Vérifiez le port COM et le protocole réel "
                f"de sshmm_M7.elf -- il faudra peut-être adapter ce module."
            )
        log(f"[SERIAL] Réponse : {response}")

        rows = []
        t0 = time.time()
        for i in range(n_samples):
            watts = float(aggregate[i])
            ser.write(f"{watts:.6f}\n".encode("ascii"))

            status, values = "NO_RESPONSE", {a: float("nan") for a in appliances}
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = ser.readline().decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("RESULT"):
                    status = "RESULT"
                    values = _parse_result_line(line, appliances)
                    break
                elif line.startswith("ERR"):
                    status = "ERROR"
                    break
                elif line.startswith("WAIT"):
                    continue  # on continue d'attendre

            row = {"sample_index": i + 1, "aggregate_watts": watts, "status": status}
            row.update(values)
            rows.append(row)

            if (i + 1) % 200 == 0 or (i + 1) == n_samples:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                log(f"[SERIAL] {i + 1}/{n_samples} échantillons traités ({rate:.1f} échant./s)")

    fieldnames = ["sample_index", "aggregate_watts", "status"] + appliances
    with open(output_csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log(f"[SERIAL] Terminé. {output_csv_path} écrit ({len(rows)} lignes).")
    return output_csv_path