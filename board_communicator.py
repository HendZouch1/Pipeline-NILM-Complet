"""
Étapes 3 et 4 du pipeline : communication PC <-> carte STM32MP15x (Linux embarqué OpenSTLinux).
Transport utilisé : SSH/SCP (via paramiko + scp).

Cortex-A7 : l'exécutable envoyé est lancé directement (natif Linux).

Cortex-M4 (cas ukdale_m4.sh) : le script attend des chemins et variables d'environnement
précis (BASE_DIR, INPUT_BIN, OUTPUT_CSV, FIRMWARE_NAME, MAX_SAMPLES...) et exige que le
firmware soit physiquement présent dans /lib/firmware/. Ce module respecte ce contrat
exact plutôt que d'inventer un protocole générique.

Note sur les noms de fichiers : les fichiers locaux (côté PC) peuvent avoir des noms
"à risque" pour un shell (espaces, parenthèses -- fréquent avec des téléchargements
de navigateur type "modele (1).tflite"). Pour éviter tout problème de découpage de
commande shell sur la carte, chaque fichier est renommé avec un nom "sûr" au moment
de l'envoi (voir _safe_remote_name), indépendamment de son nom d'origine sur le PC.
"""
import re
import paramiko
from pathlib import Path
from scp import SCPClient


def _safe_remote_name(local_path):
    """Nom de fichier distant sûr pour le shell : on garde uniquement lettres,
    chiffres, points, tirets et underscores ; tout le reste (espaces,
    parenthèses, accents...) est remplacé par '_'."""
    name = Path(local_path).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


# Environnement virtuel dédié à l'inférence .tflite sur Cortex-A7 : isolé du
# Python système (--system-site-packages pour réutiliser tflite_runtime déjà
# installé, mais avec un numpy propre à ce venv, sans toucher au numpy système).
# Créé automatiquement UNE SEULE FOIS par le pipeline si absent, puis réutilisé
# pour tous les futurs modèles .tflite -- vous n'avez plus à y repenser.
A7_TFLITE_VENV = "/home/root/.nilm_venv"


class BoardCommunicator:
    def __init__(self, connection_cfg, log=print):
        self.cfg = connection_cfg
        self.log = log
        self.transport = None
        self._virtualenv_installed_by_us = False  # pour savoir s'il faut le désinstaller ensuite

    def connect(self):
        """
        Reproduit exactement ce que fait la commande `ssh root@IP` sur cette carte :
        elle réussit SANS mot de passe car le serveur SSH accepte la méthode
        d'authentification 'none' pour root (config de dev courante sur les images
        ST). paramiko.SSHClient.connect() classique n'essaie PAS cette méthode
        automatiquement -- d'où l'erreur 'No authentication methods available'
        malgré une connexion manuelle qui marche. On utilise donc l'API bas-niveau
        Transport pour reproduire ce comportement précisément.
        """
        self.log(f"[BOARD] Connexion SSH à {self.cfg['host']}:{self.cfg['port']} ...")
        self.transport = paramiko.Transport((self.cfg["host"], self.cfg["port"]))
        self.transport.start_client(timeout=15)

        try:
            self.transport.auth_none(self.cfg["user"])
            self.log("[BOARD] Authentifié sans mot de passe (auth 'none', comme le ssh manuel).")
        except paramiko.BadAuthenticationType as e:
            if "password" in e.allowed_types:
                password = self.cfg.get("password", "")
                self.transport.auth_password(self.cfg["user"], password)
                self.log("[BOARD] Authentifié par mot de passe.")
            else:
                raise RuntimeError(
                    f"Le serveur SSH de la carte exige une méthode d'authentification "
                    f"non supportée ici : {e.allowed_types}. Ajoutez 'password' dans "
                    f"connection_cfg si un mot de passe root est défini."
                )

        self._exec(f"mkdir -p {self.cfg['remote_workdir']}/data")
        self.transport.set_keepalive(15)  # évite une déconnexion silencieuse pendant les
                                            # opérations longues (pip install, compilation...)
        self._sync_board_date()
        self.log("[BOARD] Connexion établie.")

    def _sync_board_date(self):
        """
        Cette carte n'a pas d'horloge temps réel (RTC) à pile : elle redémarre
        toujours avec une date arbitraire/fausse, ce qui fait échouer toute
        vérification de certificat HTTPS (pip, etc.) avec une erreur du type
        'certificate is not yet valid'. On copie simplement l'heure du PC (fiable,
        aucune dépendance réseau externe type NTP) vers la carte, à chaque connexion.
        """
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        code, out, err = self._exec(f"date -u -s '{now_utc}' 2>&1")
        if code == 0:
            self.log(f"[BOARD] Date de la carte synchronisée sur celle du PC (UTC {now_utc})")
        else:
            self.log(f"[BOARD] Avertissement : échec de la synchronisation de date "
                      f"(non bloquant, mais risque d'échec des installations réseau HTTPS) : "
                      f"{out}{err}")

    def _exec(self, cmd, timeout=30):
        import time
        chan = self.transport.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        out, err = b"", b""
        deadline = time.time() + timeout
        try:
            while True:
                got_data = False
                if chan.recv_ready():
                    out += chan.recv(4096)
                    got_data = True
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(4096)
                    got_data = True
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    break
                if time.time() > deadline:
                    chan.close()
                    raise TimeoutError(
                        f"Commande sur la carte non terminée après {timeout}s "
                        f"(réseau lent/instable, ou commande réellement bloquée) : {cmd}"
                    )
                if not got_data:
                    time.sleep(0.05)  # évite de saturer le CPU en boucle d'attente active
        except (OSError, EOFError) as e:
            raise RuntimeError(
                f"Connexion SSH interrompue pendant l'exécution de la commande "
                f"(réseau instable ?) : {cmd} -- détail : {e or type(e).__name__}"
            )
        exit_code = chan.recv_exit_status()
        chan.close()
        return exit_code, out.decode(errors="replace"), err.decode(errors="replace")

    # ---------- Cortex-A7 : exécution native directe ----------

    def send_files_a7(self, aggregate_bin, executable_path):
        remote_dir = self.cfg["remote_workdir"]
        exec_name = _safe_remote_name(executable_path)
        with SCPClient(self.transport) as scp:
            self.log(f"[BOARD] Envoi de {Path(aggregate_bin).name} -> {remote_dir}/aggregate.bin")
            scp.put(str(aggregate_bin), remote_path=f"{remote_dir}/aggregate.bin")
            self.log(f"[BOARD] Envoi de {Path(executable_path).name} -> {remote_dir}/{exec_name}")
            scp.put(str(executable_path), remote_path=f"{remote_dir}/{exec_name}")
        self._exec(f"chmod +x {remote_dir}/{exec_name}")
        return exec_name

    def run_inference_a7(self, exec_name, timeout=180):
        remote_dir = self.cfg["remote_workdir"]
        cmd = f"cd {remote_dir} && ./{exec_name} --input aggregate.bin --output desaggregated.csv"
        self.log(f"[BOARD] Exécution sur Cortex-A7 : {cmd}")
        code, out, err = self._exec(cmd, timeout=timeout)
        if out.strip():
            self.log(f"[BOARD][stdout] {out.strip()}")
        if code != 0:
            raise RuntimeError(f"Échec sur Cortex-A7 (code {code}) : {err}")
        return f"{remote_dir}/desaggregated.csv"

    # ---------- Cortex-A7 : modèle .tflite générique via tflite_infer.py ----------

    def send_files_a7_tflite(self, aggregate_bin, tflite_model_path, infer_script_path,
                              normalization_json_path=None):
        """
        Envoie aggregate.bin + le modèle .tflite + le script d'inférence générique
        (board_scripts/tflite_infer.py) + le JSON de normalisation (si fourni).
        Contrairement à ukdale.elf, le .tflite n'est pas exécutable : c'est
        tflite_infer.py qui joue le rôle de runtime.
        """
        remote_dir = self.cfg["remote_workdir"]
        model_name = _safe_remote_name(tflite_model_path)
        script_name = _safe_remote_name(infer_script_path)

        with SCPClient(self.transport) as scp:
            self.log(f"[BOARD] Envoi de {Path(aggregate_bin).name} -> {remote_dir}/aggregate.bin")
            scp.put(str(aggregate_bin), remote_path=f"{remote_dir}/aggregate.bin")
            self.log(f"[BOARD] Envoi de {Path(tflite_model_path).name} -> {remote_dir}/{model_name}")
            scp.put(str(tflite_model_path), remote_path=f"{remote_dir}/{model_name}")
            self.log(f"[BOARD] Envoi de {Path(infer_script_path).name} -> {remote_dir}/{script_name}")
            scp.put(str(infer_script_path), remote_path=f"{remote_dir}/{script_name}")
            norm_name = None
            if normalization_json_path:
                norm_name = _safe_remote_name(normalization_json_path)
                self.log(f"[BOARD] Envoi de {Path(normalization_json_path).name} -> {remote_dir}/{norm_name}")
                scp.put(str(normalization_json_path), remote_path=f"{remote_dir}/{norm_name}")

        return model_name, script_name, norm_name

    def ensure_a7_tflite_venv(self, model_name):
        """
        Détermine quel interpréteur Python utiliser pour l'inférence .tflite,
        en testant dans l'ordre :
          1. Le python3 SYSTÈME directement -- le cas idéal, si un paquet
             compatible est déjà installé (ex: python3-libtensorflow-lite via
             le dépôt X-LINUX-AI officiel de ST, qui garantit une compatibilité
             numpy/tflite cohérente nativement). Aucun venv nécessaire.
          2. Si le système ne fonctionne pas, repli sur le venv isolé (créé/
             réparé automatiquement), pour rester compatible avec d'autres
             cartes qui n'auraient pas ce paquet pré-installé.

        IMPORTANT : la vérification instancie un VRAI tflite.Interpreter avec le
        modèle réel (model_name, déjà envoyé sur la carte) -- un simple `import
        tflite_runtime.interpreter` ne suffit pas, car l'incompatibilité numpy ne
        se déclenche qu'au moment de charger un modèle (CreateWrapperFromFile),
        pas à l'import du module.
        """
        remote_dir = self.cfg["remote_workdir"]

        def _check(python_bin):
            cmd = (
                f"{python_bin} -c \"import tflite_runtime.interpreter as tflite; "
                f"i = tflite.Interpreter(model_path='{remote_dir}/{model_name}'); "
                f"i.allocate_tensors(); print('OK')\" 2>&1"
            )
            code, out, err = self._exec(cmd)
            return code == 0 and "OK" in out

        # 1) Python3 système en priorité (le plus simple et rapide si ça marche)
        if _check("python3"):
            self.log("[BOARD] python3 système fonctionne directement avec ce modèle "
                      "(paquet tflite compatible déjà installé) -- pas de venv nécessaire.")
            return "python3"
        self.log("[BOARD] python3 système ne fonctionne pas avec ce modèle "
                  "-> repli sur le venv isolé...")

        python_bin = f"{A7_TFLITE_VENV}/bin/python3"
        check_cmd = (
            f"{python_bin} -c \"import tflite_runtime.interpreter as tflite; "
            f"i = tflite.Interpreter(model_path='{remote_dir}/{model_name}'); "
            f"i.allocate_tensors(); print('OK')\" 2>&1"
        )

        code, out, err = self._exec(check_cmd)
        if code == 0 and "OK" in out:
            self.log(f"[BOARD] Venv .tflite déjà prêt et fonctionnel ({A7_TFLITE_VENV})")
            return python_bin

        self.log(f"[BOARD] Venv .tflite absent ou cassé -> (re)création automatique "
                  f"(une seule fois, ça peut prendre 1-2 min)...")
        self._exec(f"rm -rf {A7_TFLITE_VENV}")

        code, out, err = self._exec(
            f"python3 -m venv --system-site-packages {A7_TFLITE_VENV} 2>&1", timeout=120
        )
        if code != 0:
            if "No module named venv" in (out + err):
                self.log("[BOARD] Module 'venv' absent de cette image -> repli sur "
                          "'virtualenv' (installé via pip, déjà fonctionnel chez vous)")
                code, out, err = self._exec(
                    "pip3 install virtualenv 2>&1", timeout=180
                )
                if code != 0 and "externally-managed-environment" in (out + err):
                    self.log("[BOARD] pip système récent (PEP 668) -> nouvel essai avec "
                              "--break-system-packages")
                    code, out, err = self._exec(
                        "pip3 install --break-system-packages virtualenv 2>&1", timeout=180
                    )
                if code != 0:
                    raise RuntimeError(f"Échec de l'installation de virtualenv : {out}{err}")
                self._virtualenv_installed_by_us = True
                self.log("[BOARD] virtualenv installé au niveau système (sera désinstallé "
                          "en fin de test, voir cleanup_remote)")

                code, out, err = self._exec(
                    f"python3 -m virtualenv --system-site-packages {A7_TFLITE_VENV} 2>&1",
                    timeout=120,
                )
                if code != 0:
                    raise RuntimeError(f"Échec de la création via virtualenv : {out}{err}")
            else:
                raise RuntimeError(f"Échec de la création du venv : {out}{err}")

        # Stratégie retenue (après investigation) : mise à jour de numpy DANS le
        # venv uniquement, via piwheels.org (dépôt de paquets précompilés ARM
        # 32-bit -- numpy y est très bien couvert, contrairement à tflite_runtime
        # qui n'y existe simplement pas du tout, confirmé par des essais réels).
        # piwheels s'est montré instable en connexion (SSL/timeout intermittents)
        # -> plusieurs tentatives avant d'abandonner.
        self.log("[BOARD] Mise à jour de numpy DANS le venv uniquement (système non touché), "
                  "via piwheels.org...")
        max_attempts = 4
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            self.log(f"[BOARD] Installation de numpy (tentative {attempt}/{max_attempts})...")
            code, out, err = self._exec(
                f"{python_bin} -m pip install --index-url https://www.piwheels.org/simple "
                f"--upgrade numpy 2>&1",
                timeout=600,
            )
            if code == 0:
                break
            last_err = (out + err).strip()[:400]
            self.log(f"[BOARD]   -> échec (réseau instable ?), nouvel essai. Détail : {last_err}")
        else:
            raise RuntimeError(
                f"Échec de l'installation de numpy via piwheels après {max_attempts} "
                f"tentatives (réseau probablement trop instable) : {last_err}"
            )

        # Vérification avec le VRAI modèle, pas juste un import.
        code, out, err = self._exec(check_cmd)
        if code != 0 or "OK" not in out:
            raise RuntimeError(
                f"numpy mis à jour mais tflite_runtime ne fonctionne toujours pas "
                f"avec le vrai modèle : {(err or out).strip()[:400]}"
            )

        self.log("[BOARD] Venv .tflite prêt et fonctionnel (numpy système inchangé).")
        return python_bin

    def run_inference_a7_tflite(self, model_name, script_name, norm_name=None,
                                 max_samples=0, timeout=1800):
        remote_dir = self.cfg["remote_workdir"]
        python_bin = self.ensure_a7_tflite_venv(model_name)
        cmd = (f"cd {remote_dir} && {python_bin} {script_name} "
               f"--model {model_name} --input aggregate.bin --output desaggregated.csv "
               f"--max-samples {max_samples}")
        if norm_name:
            cmd += f" --normalization-json {norm_name}"
        self.log(f"[BOARD] Exécution sur Cortex-A7 (.tflite) : {cmd}")
        code, out, err = self._exec(cmd, timeout=timeout)
        if out.strip():
            self.log(f"[BOARD][stdout]\n{out.strip()}")
        if code != 0:
            raise RuntimeError(f"Échec de tflite_infer.py (code {code}) : {err}")
        self.log("[BOARD] Désagrégation A7 (.tflite) terminée.")
        return f"{remote_dir}/desaggregated.csv"

    # ---------- Cortex-M4 : contrat exact de ukdale_m4.sh ----------

    def send_files_m4(self, ukdale_bin_path, elf_path, script_path):
        """
        Envoie les 3 fichiers dans remote_workdir : ukdale.bin, l'elf (sous un nom
        distant sûr), et le script. Le firmware n'est PAS encore copié vers
        /lib/firmware/ ici -- ça se fait juste avant l'exécution (voir run_inference_m4),
        pour rester isolé le plus longtemps possible.
        """
        remote_dir = self.cfg["remote_workdir"]
        elf_name = _safe_remote_name(elf_path)
        script_name = _safe_remote_name(script_path)

        with SCPClient(self.transport) as scp:
            self.log(f"[BOARD] Envoi de ukdale.bin -> {remote_dir}/data/ukdale.bin")
            scp.put(str(ukdale_bin_path), remote_path=f"{remote_dir}/data/ukdale.bin")
            self.log(f"[BOARD] Envoi de {Path(elf_path).name} -> {remote_dir}/{elf_name}")
            scp.put(str(elf_path), remote_path=f"{remote_dir}/{elf_name}")
            self.log(f"[BOARD] Envoi de {Path(script_path).name} -> {remote_dir}/{script_name}")
            scp.put(str(script_path), remote_path=f"{remote_dir}/{script_name}")

        self._exec(f"chmod +x {remote_dir}/{script_name}")
        return elf_name, script_name

    def run_inference_m4(self, elf_name, script_name, confirm_m4, max_samples=0, timeout=600):
        """
        Reproduit exactement ce que vous faites à la main, avec les chemins de
        remote_workdir injectés via les variables d'environnement que ukdale_m4.sh
        sait déjà lire (BASE_DIR, INPUT_BIN, OUTPUT_CSV, FIRMWARE_NAME, MAX_SAMPLES) --
        pour que le test reste isolé dans remote_workdir, à l'exception du firmware
        qui DOIT être copié dans /lib/firmware/ (obligatoire pour remoteproc, c'est
        pour ça que confirm_m4 est requis).
        """
        if not confirm_m4:
            raise RuntimeError(
                "Exécution sur Cortex-M4 refusée : ceci copie le firmware dans "
                "/lib/firmware/ et recharge le coprocesseur M4 (arrête ce qui y "
                "tournait déjà). Passez confirm_m4=True (ou cochez la case dédiée "
                "dans l'interface) pour l'autoriser explicitement."
            )

        remote_dir = self.cfg["remote_workdir"]
        firmware_path = f"/lib/firmware/{elf_name}"

        self.log(f"[BOARD] Copie du firmware vers {firmware_path} (requis par remoteproc)")
        code, out, err = self._exec(f"cp {remote_dir}/{elf_name} {firmware_path}")
        if code != 0:
            raise RuntimeError(f"Échec de la copie du firmware : {err}")

        env = (
            f"BASE_DIR={remote_dir} "
            f"INPUT_BIN={remote_dir}/data/ukdale.bin "
            f"OUTPUT_CSV={remote_dir}/data/disaggregated.csv "
            f"FIRMWARE_NAME={elf_name} "
            f"MAX_SAMPLES={max_samples}"
        )
        cmd = f"cd {remote_dir} && {env} bash {script_name}"
        self.log(f"[BOARD] Exécution sur Cortex-M4 : {cmd}")

        code, out, err = self._exec(cmd, timeout=timeout)
        if out.strip():
            self.log(f"[BOARD][stdout]\n{out.strip()}")
        if code != 0:
            raise RuntimeError(f"Échec de ukdale_m4.sh (code {code}) : {err}")
        self.log("[BOARD] Désagrégation M4 terminée.")
        return f"{remote_dir}/data/disaggregated.csv"

    # ---------- Commun ----------

    def fetch_result(self, remote_csv_path, local_output_path):
        with SCPClient(self.transport) as scp:
            scp.get(remote_csv_path, local_path=str(local_output_path))
        self.log(f"[BOARD] {Path(remote_csv_path).name} rapatrié -> {local_output_path}")
        return local_output_path

    def cleanup_remote(self, elf_name=None):
        """Supprime le dossier de travail créé (remote_workdir) ET le firmware copié
        dans /lib/firmware/ (uniquement le fichier qu'on y a nous-mêmes déposé).
        Note : A7_TFLITE_VENV (.nilm_venv) n'est PAS supprimé ici -- il est
        volontairement conservé pour être réutilisé instantanément lors des
        prochains tests .tflite, sans avoir à tout recréer."""
        remote_dir = self.cfg["remote_workdir"]
        self.log(f"[BOARD] Nettoyage : suppression de {remote_dir}")
        self._exec(f"rm -rf {remote_dir}")
        if elf_name:
            self.log(f"[BOARD] Nettoyage : suppression de /lib/firmware/{elf_name}")
            self._exec(f"rm -f /lib/firmware/{elf_name}")

        if self._virtualenv_installed_by_us:
            self.log("[BOARD] Nettoyage : désinstallation de virtualenv du système "
                      "(installé pour ce test uniquement -- .nilm_venv lui-même est conservé)")
            code, out, err = self._exec("pip3 uninstall -y virtualenv 2>&1")
            if code != 0:
                self.log(f"[BOARD] Avertissement : désinstallation de virtualenv échouée "
                          f"(non bloquant) : {out}{err}")
            else:
                self.log("[BOARD] virtualenv désinstallé du système avec succès.")

    def close(self):
        if self.transport:
            self.transport.close()
            self.log("[BOARD] Connexion fermée.")