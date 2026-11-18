"""
Étape 7 : interface graphique du banc de test NILM sur cartes STM32.

Refonte visuelle :
  - palette cohérente inspirée de la charte STMicroelectronics
  - onglets : Configuration / Exécution (étapes + log) / Résultats
  - tableau de métriques colorisé CELLULE PAR CELLULE (impossible avec un
    ttk.Treeview, qui ne colore qu'une ligne entière -> table maison en Labels)
  - log couleur par catégorie ([SPLIT], [CORTEX], [BOARD], [SERIAL], [METRICS]...)
    avec horodatage, compteur d'erreurs/avertissements
  - cartes de synthèse (cortex utilisé, F1 moyen, MAE moyenne, durée...)
  - export d'un rapport HTML autonome (métriques + log) et export CSV

Correctif fonctionnel au passage : l'ancien tableau déclarait 7 colonnes alors
que metrics.py renvoie 7 métriques + l'appareil (8 valeurs, dont 'Seuil (W)').
Les colonnes sont maintenant construites dynamiquement à partir du DataFrame,
et les valeurs lues par NOM de colonne, plus par position.

Lancement :
    python gui_app.py            (ou python main.py --gui)
    python gui_app.py --demo     (aperçu de l'UI avec des données fictives,
                                  sans carte branchée)
"""
import csv
import html
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from config import BOARD_DB
from pipeline_orchestrator import run_pipeline

# =====================================================================
# PALETTE (charte ST : bleu nuit / cyan / magenta) + sémantique métrique
# =====================================================================
C = {
    "bg":          "#EEF1F6",   # fond général
    "surface":     "#FFFFFF",   # cartes, panneaux
    "surface_alt": "#F6F8FB",   # lignes alternées
    "border":      "#D6DEE9",
    "ink":         "#03234B",   # bleu nuit ST - texte principal / en-têtes
    "ink_soft":    "#5A6B85",   # texte secondaire
    "accent":      "#3CB4E6",   # cyan ST - étape en cours, liens
    "accent_dark": "#1B7FB0",
    "magenta":     "#E6007E",   # ST magenta - étapes M4 / packaging
    "violet":      "#7A5AF8",   # sélection de cortex
    "teal":        "#0E7C86",   # communication carte / série
    "ok":          "#1E9E6A",   # bon
    "ok_bg":       "#E4F5EE",
    "warn":        "#C77700",   # moyen
    "warn_bg":     "#FDF2DF",
    "bad":         "#D14343",   # mauvais
    "bad_bg":      "#FCE9E9",
    "na":          "#98A4B5",   # non applicable
    "na_bg":       "#F2F4F7",
    "log_bg":      "#0B1220",   # console
    "log_fg":      "#C9D6E8",
    "log_dim":     "#5C6B85",
}

FONT_UI = ("Segoe UI", 10)
FONT_UI_B = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI Semibold", 15)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 9)
FONT_NUM = ("Consolas", 10)
FONT_NUM_B = ("Consolas", 11, "bold")

STEP_LABELS = [
    "Lecture des entrées",
    "Split dataset (aggregate.bin + ground_truth.csv)",
    "Sélection du cortex (si / sinon)",
    "Transfert + exécution sur la carte",
    "Calcul des métriques",
]

# Ordre d'affichage des colonnes de métriques (celles absentes sont ignorées,
# celles en plus sont ajoutées à la fin -> aucune valeur ne peut être perdue).
COLUMN_ORDER = ["RMSE (W)", "MAE (W)", "EN%", "Precision", "Recall", "F1", "Seuil (W)"]
COLUMN_TOOLTIP = {
    "RMSE (W)":  "Erreur quadratique moyenne (plus bas = mieux)",
    "MAE (W)":   "Erreur absolue moyenne (plus bas = mieux)",
    "EN%":       "Précision énergétique : 100 % = énergie parfaitement restituée",
    "Precision": "Parmi les ON prédits, combien étaient réellement ON",
    "Recall":    "Parmi les ON réels, combien ont été détectés",
    "F1":        "Moyenne harmonique Precision/Recall",
    "Seuil (W)": "Seuil ON/OFF utilisé pour cet appareil",
}


# =====================================================================
# Formatage + code couleur des valeurs
# =====================================================================
def _is_na(v):
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == "" or v.strip().lower() in ("nan", "n/a")
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def fmt_value(col, v):
    """Texte affiché pour une valeur de métrique."""
    if _is_na(v):
        return "N/A"
    v = float(v)
    if col in ("RMSE (W)", "MAE (W)"):
        return f"{v:,.1f}".replace(",", " ")
    if col == "EN%":
        return f"{v:.1f} %"
    if col == "Seuil (W)":
        return f"{v:g}"
    return f"{v:.3f}"          # Precision / Recall / F1


def quality(col, v, threshold_w=15.0):
    """
    Renvoie 'ok' / 'warn' / 'bad' / 'na' pour colorer la cellule.

    Convention :
      - Precision / Recall / F1 : >= 0.80 bon, >= 0.50 moyen, sinon mauvais
      - EN%                     : >= 80 bon, >= 50 moyen, sinon mauvais
      - RMSE / MAE              : comparés au SEUIL ON/OFF de l'appareil
                                  (<= 1x seuil bon, <= 3x seuil moyen) -- un
                                  seuil absolu unique n'aurait aucun sens entre
                                  une bouilloire (3 kW) et un routeur (7 W).
      - Seuil (W)               : information, jamais colorée.
    """
    if col == "Seuil (W)" or _is_na(v):
        return "na" if _is_na(v) else "info"
    v = float(v)
    if col in ("Precision", "Recall", "F1"):
        return "ok" if v >= 0.80 else "warn" if v >= 0.50 else "bad"
    if col == "EN%":
        return "ok" if v >= 80 else "warn" if v >= 50 else "bad"
    if col in ("RMSE (W)", "MAE (W)"):
        thr = float(threshold_w) if not _is_na(threshold_w) else 15.0
        thr = max(thr, 1.0)
        return "ok" if v <= thr else "warn" if v <= 3 * thr else "bad"
    return "info"


QUALITY_COLORS = {
    "ok":   (C["ok"], C["ok_bg"]),
    "warn": (C["warn"], C["warn_bg"]),
    "bad":  (C["bad"], C["bad_bg"]),
    "na":   (C["na"], C["na_bg"]),
    "info": (C["ink_soft"], C["surface"]),
}


# =====================================================================
# Petits composants réutilisables
# =====================================================================
class ScrollArea(ttk.Frame):
    """Zone défilante verticalement (pour le tableau de métriques)."""

    def __init__(self, parent, bg=C["surface"]):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win, width=e.width),
        )
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        try:
            if self.canvas.winfo_ismapped():
                self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass


class StatCard(tk.Frame):
    """Petite carte de synthèse : libellé + grande valeur."""

    def __init__(self, parent, label, value="—", color=None):
        super().__init__(parent, bg=C["surface"], highlightthickness=1,
                         highlightbackground=C["border"], bd=0)
        tk.Label(self, text=label.upper(), bg=C["surface"], fg=C["ink_soft"],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(9, 0))
        self.value_lbl = tk.Label(self, text=value, bg=C["surface"],
                                  fg=color or C["ink"], font=("Segoe UI Semibold", 16),
                                  anchor="w")
        self.value_lbl.pack(anchor="w", padx=12, pady=(0, 10))

    def set(self, value, color=None):
        self.value_lbl.config(text=value, fg=color or C["ink"])


class Tooltip:
    """Infobulle simple (utilisée sur les en-têtes de colonnes)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg=C["ink"], fg="white",
                 font=FONT_SMALL, padx=8, pady=4).pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# =====================================================================
# Fenêtre principale
# =====================================================================
class PipelineGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Banc de test NILM — STM32")
        self.geometry("1180x860")
        self.minsize(1000, 700)
        self.configure(bg=C["bg"])

        self._apply_theme()

        # --- état ---
        self.dataset_path = tk.StringVar()
        self.model_path = tk.StringVar()
        self.board_name = tk.StringVar(value=list(BOARD_DB.keys())[0])
        self.elf_hint = tk.StringVar(value="auto")
        self.cleanup_board = tk.BooleanVar(value=True)
        self.confirm_m4 = tk.BooleanVar(value=False)
        self.m4_script_path = tk.StringVar()
        self.m4_max_samples = tk.StringVar(value="0")
        self.normalization_json_path = tk.StringVar()
        self.a7_max_samples = tk.StringVar(value="1000")
        self.serial_max_samples = tk.StringVar(value="1000")
        self.autoscroll = tk.BooleanVar(value=True)

        self._log_lines = []          # (timestamp, message) pour l'export
        self._n_errors = 0
        self._n_warnings = 0
        self._t0 = None
        self._running = False
        self._last_result = None
        self._current_step = 0

        # --- structure ---
        self._build_header()
        self._build_notebook()
        self._build_statusbar()

    # ---------------- thème ttk ----------------
    def _apply_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=C["bg"], foreground=C["ink"], font=FONT_UI)
        style.configure("TFrame", background=C["bg"])
        style.configure("Card.TFrame", background=C["surface"], relief="flat")
        style.configure("TLabel", background=C["bg"], foreground=C["ink"])
        style.configure("Card.TLabel", background=C["surface"], foreground=C["ink"])
        style.configure("Muted.TLabel", background=C["surface"], foreground=C["ink_soft"],
                        font=FONT_SMALL)
        style.configure("Section.TLabelframe", background=C["surface"],
                        bordercolor=C["border"], relief="solid", borderwidth=1)
        style.configure("Section.TLabelframe.Label", background=C["surface"],
                        foreground=C["ink"], font=FONT_UI_B)

        style.configure("TEntry", fieldbackground="white", bordercolor=C["border"],
                        lightcolor=C["border"], darkcolor=C["border"], padding=4)
        style.configure("TCombobox", fieldbackground="white", padding=3)
        style.configure("TCheckbutton", background=C["surface"], foreground=C["ink"])

        style.configure("TButton", padding=(12, 6), background="#E3E9F2",
                        foreground=C["ink"], borderwidth=0, font=FONT_UI)
        style.map("TButton", background=[("active", "#D2DCEA")])
        style.configure("Primary.TButton", background=C["ink"], foreground="white",
                        font=FONT_UI_B, padding=(18, 9), borderwidth=0)
        style.map("Primary.TButton",
                  background=[("active", "#0A3468"), ("disabled", "#9AA9BF")])
        style.configure("Ghost.TButton", background=C["surface"], foreground=C["accent_dark"])
        style.map("Ghost.TButton", background=[("active", "#E8F4FB")])

        style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(6, 6, 0, 0))
        style.configure("TNotebook.Tab", padding=(20, 9), background="#DDE4EE",
                        foreground=C["ink_soft"], font=FONT_UI_B, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C["surface"])],
                  foreground=[("selected", C["ink"])])

        style.configure("Bench.Horizontal.TProgressbar", troughcolor="#DCE3EE",
                        background=C["accent"], bordercolor="#DCE3EE",
                        lightcolor=C["accent"], darkcolor=C["accent"], thickness=8)

    # ---------------- bandeau supérieur ----------------
    def _build_header(self):
        bar = tk.Frame(self, bg=C["ink"], height=62)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="Banc de test NILM", bg=C["ink"], fg="white",
                 font=FONT_TITLE).pack(side="left", padx=(18, 10))
        tk.Label(bar, text="désagrégation de charge sur cartes STM32",
                 bg=C["ink"], fg="#9FC7E8", font=FONT_SMALL).pack(side="left", pady=(6, 0))

        self.header_ctx = tk.Label(bar, text="", bg=C["ink"], fg="#9FC7E8", font=FONT_SMALL)
        self.header_ctx.pack(side="right", padx=18)

    # ---------------- onglets ----------------
    def _build_notebook(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(10, 6))

        self.tab_cfg = ttk.Frame(self.nb, style="TFrame")
        self.tab_run = ttk.Frame(self.nb, style="TFrame")
        self.tab_res = ttk.Frame(self.nb, style="TFrame")
        self.nb.add(self.tab_cfg, text="  1 · Configuration  ")
        self.nb.add(self.tab_run, text="  2 · Exécution  ")
        self.nb.add(self.tab_res, text="  3 · Résultats  ")

        self._build_inputs(self.tab_cfg)
        self._build_run_tab(self.tab_run)
        self._build_results_tab(self.tab_res)

    # ---------- onglet configuration ----------
    def _build_inputs(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        def section(title):
            f = ttk.Labelframe(wrap, text="  " + title + "  ", style="Section.TLabelframe",
                               padding=(14, 10))
            f.pack(fill="x", pady=(0, 12))
            f.columnconfigure(1, weight=1)
            return f

        def file_row(frame, row, label, var, command, hint=None):
            ttk.Label(frame, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=5, padx=(0, 10))
            ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=5)
            ttk.Button(frame, text="Parcourir…", command=command).grid(
                row=row, column=2, padx=(8, 0))
            if hint:
                ttk.Label(frame, text=hint, style="Muted.TLabel").grid(
                    row=row + 1, column=1, sticky="w", pady=(0, 4))

        # --- Entrées principales ---
        f1 = section("Entrées obligatoires")
        file_row(f1, 0, "Dataset (CSV) :", self.dataset_path, self._pick_dataset,
                 "colonne 'aggregate' + une colonne par appareil (ex. UK-DALE house2)")
        file_row(f1, 2, "Modèle (.elf / .tflite / .onnx) :", self.model_path, self._pick_model)

        ttk.Label(f1, text="Carte STM32 :", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=5, padx=(0, 10))
        board_box = ttk.Combobox(f1, textvariable=self.board_name,
                                 values=list(BOARD_DB.keys()), state="readonly", width=34)
        board_box.grid(row=3, column=1, sticky="w", pady=5)
        board_box.bind("<<ComboboxSelected>>", lambda e: self._refresh_board_info())

        self.board_info = ttk.Label(f1, text="", style="Muted.TLabel")
        self.board_info.grid(row=4, column=1, sticky="w")

        ttk.Label(f1, text="Forcer le cortex :", style="Card.TLabel").grid(
            row=5, column=0, sticky="w", pady=5, padx=(0, 10))
        ttk.Combobox(f1, textvariable=self.elf_hint, values=["auto", "A7", "M4", "M7"],
                     state="readonly", width=10).grid(row=5, column=1, sticky="w", pady=5)

        # --- Options par cible ---
        f2 = section("Options selon la cible d'exécution")
        file_row(f2, 0, "[M4] Script de lancement (.sh) :", self.m4_script_path,
                 self._pick_m4_script, "ex. ukdale_m4.sh — obligatoire pour un .elf Cortex-M4")
        file_row(f2, 2, "JSON de normalisation / seuils :", self.normalization_json_path,
                 self._pick_normalization_json,
                 "recommandé sur toutes les cartes : seuils ON/OFF par appareil (P/R/F1 fiables)")

        samples = ttk.Frame(f2, style="Card.TFrame")
        samples.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))
        for i, (lbl, var) in enumerate([
            ("Échantillons M4", self.m4_max_samples),
            ("Échantillons A7 (.tflite)", self.a7_max_samples),
            ("Échantillons série (F746G)", self.serial_max_samples),
        ]):
            tk.Label(samples, text=lbl, bg=C["surface"], fg=C["ink_soft"],
                     font=FONT_SMALL).grid(row=0, column=i * 2, sticky="w", padx=(0, 6))
            ttk.Entry(samples, textvariable=var, width=8).grid(
                row=0, column=i * 2 + 1, padx=(0, 22))
        ttk.Label(f2, text="0 = tout le dataset", style="Muted.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w")

        # --- Sécurité ---
        f3 = section("Sécurité et nettoyage")
        ttk.Checkbutton(f3, variable=self.cleanup_board,
                        text="Supprimer les fichiers déposés sur la carte après le test "
                             "(recommandé)").grid(row=0, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(f3, variable=self.confirm_m4,
                        text="J'autorise le rechargement du firmware Cortex-M4 "
                             "(copie dans /lib/firmware + reload — opération invasive)"
                        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=3)
        tk.Label(f3, text="Carte série (STM32F746G-DISCO) : le .elf doit déjà être flashé "
                          "manuellement, le port COM se règle dans config.py.",
                 bg=C["surface"], fg=C["warn"], font=FONT_SMALL,
                 wraplength=900, justify="left").grid(row=2, column=0, columnspan=3,
                                                      sticky="w", pady=(6, 0))

        # --- Bouton de lancement ---
        actions = ttk.Frame(wrap)
        actions.pack(fill="x")
        self.run_btn = ttk.Button(actions, text="▶   Lancer le pipeline",
                                  style="Primary.TButton", command=self._launch)
        self.run_btn.pack(side="left")
        ttk.Button(actions, text="Réinitialiser", command=self._reset_form).pack(
            side="left", padx=8)

        self._refresh_board_info()

    def _refresh_board_info(self):
        cfg = BOARD_DB.get(self.board_name.get(), {})
        conn = cfg.get("connection", {})
        cortexes = " + ".join(cfg.get("cortexes", []))
        if conn.get("type") == "serial":
            link = f"série {conn.get('port')} @ {conn.get('baudrate')} bauds"
        else:
            link = f"SSH {conn.get('user')}@{conn.get('host')}:{conn.get('port')}"
        self.board_info.config(text=f"{cortexes}  ·  {link}")

    # ---------- onglet exécution ----------
    def _build_run_tab(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        # progression
        prog = tk.Frame(wrap, bg=C["surface"], highlightthickness=1,
                        highlightbackground=C["border"])
        prog.pack(fill="x", pady=(0, 10))

        top = tk.Frame(prog, bg=C["surface"])
        top.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(top, text="Avancement du pipeline", bg=C["surface"], fg=C["ink"],
                 font=FONT_UI_B).pack(side="left")
        self.elapsed_lbl = tk.Label(top, text="", bg=C["surface"], fg=C["ink_soft"],
                                    font=FONT_MONO)
        self.elapsed_lbl.pack(side="right")

        self.progress = ttk.Progressbar(prog, style="Bench.Horizontal.TProgressbar",
                                        maximum=len(STEP_LABELS), value=0)
        self.progress.pack(fill="x", padx=14, pady=(2, 10))

        steps_frame = tk.Frame(prog, bg=C["surface"])
        steps_frame.pack(fill="x", padx=14, pady=(0, 12))
        self.step_widgets = []
        for i, label in enumerate(STEP_LABELS):
            row = tk.Frame(steps_frame, bg=C["surface"])
            row.pack(fill="x", pady=1)
            icon = tk.Label(row, text="○", bg=C["surface"], fg=C["na"],
                            font=("Segoe UI", 11), width=2)
            icon.pack(side="left")
            txt = tk.Label(row, text=f"{i + 1}. {label}", bg=C["surface"], fg=C["na"],
                           font=FONT_UI, anchor="w")
            txt.pack(side="left")
            self.step_widgets.append((icon, txt))

        # console
        logf = tk.Frame(wrap, bg=C["surface"], highlightthickness=1,
                        highlightbackground=C["border"])
        logf.pack(fill="both", expand=True)

        bar = tk.Frame(logf, bg=C["surface"])
        bar.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(bar, text="Journal d'exécution", bg=C["surface"], fg=C["ink"],
                 font=FONT_UI_B).pack(side="left")
        self.log_counter = tk.Label(bar, text="", bg=C["surface"], fg=C["ink_soft"],
                                    font=FONT_SMALL)
        self.log_counter.pack(side="left", padx=12)
        ttk.Button(bar, text="Enregistrer…", style="Ghost.TButton",
                   command=self._save_log).pack(side="right")
        ttk.Button(bar, text="Copier", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right", padx=6)
        ttk.Button(bar, text="Effacer", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")
        ttk.Checkbutton(bar, text="Défilement auto", variable=self.autoscroll).pack(
            side="right", padx=12)

        holder = tk.Frame(logf, bg=C["log_bg"])
        holder.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box = tk.Text(holder, bg=C["log_bg"], fg=C["log_fg"], font=FONT_MONO,
                               relief="flat", wrap="word", padx=12, pady=10,
                               insertbackground=C["log_fg"], state="disabled")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)
        self._configure_log_tags()

    def _configure_log_tags(self):
        t = self.log_box
        t.tag_configure("time", foreground=C["log_dim"])
        t.tag_configure("default", foreground=C["log_fg"])
        t.tag_configure("split", foreground="#5FD3F3")
        t.tag_configure("cortex", foreground="#B9A2FF")
        t.tag_configure("board", foreground="#54D8C4")
        t.tag_configure("serial", foreground="#54D8C4")
        t.tag_configure("pack", foreground="#FF7BC0")
        t.tag_configure("metrics", foreground="#FFD98A")
        t.tag_configure("section", foreground="#FFFFFF", background="#17233A",
                        font=("Consolas", 9, "bold"), spacing1=4, spacing3=4)
        t.tag_configure("result", foreground="#7CE0A8", font=("Consolas", 9, "bold"))
        t.tag_configure("warn", foreground="#FFC15E")
        t.tag_configure("error", foreground="#FF8B8B", font=("Consolas", 9, "bold"))

    # ---------- onglet résultats ----------
    def _build_results_tab(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        # cartes de synthèse
        cards = ttk.Frame(wrap)
        cards.pack(fill="x", pady=(0, 12))
        self.cards = {}
        for key, label in [("cortex", "Cortex utilisé"), ("board", "Carte"),
                           ("samples", "Échantillons"), ("appliances", "Appareils"),
                           ("f1", "F1 moyen"), ("mae", "MAE moyenne"),
                           ("duration", "Durée")]:
            card = StatCard(cards, label)
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.cards[key] = card

        # tableau
        table_box = tk.Frame(wrap, bg=C["surface"], highlightthickness=1,
                             highlightbackground=C["border"])
        table_box.pack(fill="both", expand=True)

        head = tk.Frame(table_box, bg=C["surface"])
        head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(head, text="Métriques par appareil", bg=C["surface"], fg=C["ink"],
                 font=FONT_UI_B).pack(side="left")

        legend = tk.Frame(head, bg=C["surface"])
        legend.pack(side="left", padx=18)
        for txt, key in [("bon", "ok"), ("moyen", "warn"), ("faible", "bad"),
                         ("non applicable", "na")]:
            fg, bg = QUALITY_COLORS[key]
            chip = tk.Frame(legend, bg=C["surface"])
            chip.pack(side="left", padx=(0, 12))
            tk.Frame(chip, bg=fg, width=10, height=10).pack(side="left", pady=3)
            tk.Label(chip, text=" " + txt, bg=C["surface"], fg=C["ink_soft"],
                     font=FONT_SMALL).pack(side="left")

        ttk.Button(head, text="Rapport HTML", style="Ghost.TButton",
                   command=self._export_html).pack(side="right")
        ttk.Button(head, text="Exporter CSV", style="Ghost.TButton",
                   command=self._export_csv).pack(side="right", padx=6)
        ttk.Button(head, text="Ouvrir le dossier", style="Ghost.TButton",
                   command=self._open_outputs).pack(side="right")

        self.table_area = ScrollArea(table_box)
        self.table_area.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self._placeholder = tk.Label(self.table_area.inner,
                                     text="Aucun résultat pour le moment.\n"
                                          "Lancez le pipeline depuis l'onglet Configuration.",
                                     bg=C["surface"], fg=C["na"], font=FONT_UI, pady=40)
        self._placeholder.pack()

    # ---------------- barre d'état ----------------
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=C["surface"], highlightthickness=1,
                       highlightbackground=C["border"])
        bar.pack(fill="x", side="bottom")
        self.status_dot = tk.Label(bar, text="●", bg=C["surface"], fg=C["na"],
                                   font=("Segoe UI", 11))
        self.status_dot.pack(side="left", padx=(12, 4), pady=4)
        self.status_lbl = tk.Label(bar, text="Prêt", bg=C["surface"], fg=C["ink_soft"],
                                   font=FONT_SMALL)
        self.status_lbl.pack(side="left")
        self.status_right = tk.Label(bar, text="", bg=C["surface"], fg=C["ink_soft"],
                                     font=FONT_SMALL)
        self.status_right.pack(side="right", padx=12)

    def _set_status(self, text, color=C["ink_soft"], dot=C["na"]):
        self.status_lbl.config(text=text, fg=color)
        self.status_dot.config(fg=dot)

    # =================================================================
    # Sélecteurs de fichiers
    # =================================================================
    def _pick_dataset(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("Tous", "*.*")])
        if p:
            self.dataset_path.set(p)

    def _pick_model(self):
        p = filedialog.askopenfilename(
            filetypes=[("Modèles", "*.elf *.tflite *.onnx"), ("Tous", "*.*")])
        if p:
            self.model_path.set(p)

    def _pick_m4_script(self):
        p = filedialog.askopenfilename(filetypes=[("Scripts shell", "*.sh"), ("Tous", "*.*")])
        if p:
            self.m4_script_path.set(p)

    def _pick_normalization_json(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("Tous", "*.*")])
        if p:
            self.normalization_json_path.set(p)

    def _reset_form(self):
        for var in (self.dataset_path, self.model_path, self.m4_script_path,
                    self.normalization_json_path):
            var.set("")
        self.elf_hint.set("auto")
        self.confirm_m4.set(False)
        self.cleanup_board.set(True)

    # =================================================================
    # Log
    # =================================================================
    def _tag_for(self, message):
        m = message.strip()
        low = m.lower()
        if m.startswith("=====") or m.startswith("=========="):
            return "section"
        if m.startswith(">>>"):
            return "result"
        if "[erreur]" in low or "échec" in low or "erreur" in low or "refusé" in low:
            return "error"
        if "attention" in low or "avertissement" in low:
            return "warn"
        for prefix, tag in (("[SPLIT]", "split"), ("[CORTEX]", "cortex"),
                            ("[BOARD]", "board"), ("[SERIAL]", "serial"),
                            ("[M4-PACK]", "pack"), ("[METRICS]", "metrics")):
            if m.startswith(prefix):
                return tag
        return "default"

    def _log(self, message):
        for line in str(message).split("\n"):
            tag = self._tag_for(line)
            if tag == "error":
                self._n_errors += 1
            elif tag == "warn":
                self._n_warnings += 1
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_lines.append((ts, line))
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"{ts}  ", "time")
            self.log_box.insert("end", line + "\n", tag)
            self.log_box.config(state="disabled")
        if self.autoscroll.get():
            self.log_box.see("end")
        self.log_counter.config(
            text=f"{len(self._log_lines)} lignes · {self._n_warnings} avertissement(s) · "
                 f"{self._n_errors} erreur(s)")
        self.update_idletasks()

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")
        self._log_lines.clear()
        self._n_errors = self._n_warnings = 0
        self.log_counter.config(text="")

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(f"{t}  {m}" for t, m in self._log_lines))
        self._set_status("Journal copié dans le presse-papiers", C["ok"], C["ok"])

    def _save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".log",
                                         initialfile="pipeline.log",
                                         filetypes=[("Log", "*.log"), ("Texte", "*.txt")])
        if not p:
            return
        Path(p).write_text("\n".join(f"{t}  {m}" for t, m in self._log_lines),
                           encoding="utf-8")
        self._set_status(f"Journal enregistré : {p}", C["ok"], C["ok"])

    # =================================================================
    # Étapes
    # =================================================================
    def _set_step(self, step_index, label):
        """step_index : numéro d'étape en cours (1..5)."""
        self._current_step = step_index
        self.progress.config(value=step_index - 1)
        for i, (icon, txt) in enumerate(self.step_widgets):
            if i < step_index - 1:
                icon.config(text="✔", fg=C["ok"])
                txt.config(fg=C["ink"], font=FONT_UI)
            elif i == step_index - 1:
                icon.config(text="▶", fg=C["accent_dark"])
                txt.config(text=f"{i + 1}. {label}", fg=C["accent_dark"], font=FONT_UI_B)
            else:
                icon.config(text="○", fg=C["na"])
                txt.config(fg=C["na"], font=FONT_UI)
        self.update_idletasks()

    def _mark_all_done(self):
        self.progress.config(value=len(STEP_LABELS))
        for i, (icon, txt) in enumerate(self.step_widgets):
            icon.config(text="✔", fg=C["ok"])
            txt.config(text=f"{i + 1}. {STEP_LABELS[i]}", fg=C["ink"], font=FONT_UI)

    def _mark_failed(self):
        idx = max(self._current_step - 1, 0)
        if idx < len(self.step_widgets):
            icon, txt = self.step_widgets[idx]
            icon.config(text="✖", fg=C["bad"])
            txt.config(fg=C["bad"], font=FONT_UI_B)

    def _tick(self):
        if not self._running:
            return
        elapsed = time.time() - self._t0
        self.elapsed_lbl.config(text=f"⏱ {int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
        self.after(1000, self._tick)

    # =================================================================
    # Lancement du pipeline
    # =================================================================
    def _launch(self):
        if not self.dataset_path.get() or not self.model_path.get():
            messagebox.showwarning("Champs manquants",
                                   "Sélectionnez un dataset et un modèle.")
            return

        self.run_btn.config(state="disabled")
        self._clear_log()
        self._clear_table()
        self._t0 = time.time()
        self._running = True
        self._current_step = 0
        self.progress.config(value=0)
        for i, (icon, txt) in enumerate(self.step_widgets):
            icon.config(text="○", fg=C["na"])
            txt.config(text=f"{i + 1}. {STEP_LABELS[i]}", fg=C["na"], font=FONT_UI)

        self.header_ctx.config(
            text=f"{Path(self.dataset_path.get()).name}  →  "
                 f"{Path(self.model_path.get()).name}  →  {self.board_name.get()}")
        self.nb.select(self.tab_run)
        self._set_status("Pipeline en cours…", C["accent_dark"], C["accent"])
        self._tick()

        threading.Thread(target=self._run_pipeline_thread, daemon=True).start()

    def _int_or(self, var, default=0):
        try:
            return int(var.get() or default)
        except ValueError:
            return default

    def _run_pipeline_thread(self):
        hint = None if self.elf_hint.get() == "auto" else self.elf_hint.get()
        try:
            result = run_pipeline(
                dataset_csv=self.dataset_path.get(),
                model_path=self.model_path.get(),
                board_name=self.board_name.get(),
                workdir="./run_output",
                elf_target_hint=hint,
                cleanup_board=self.cleanup_board.get(),
                confirm_m4=self.confirm_m4.get(),
                launch_script=self.m4_script_path.get() or None,
                m4_max_samples=self._int_or(self.m4_max_samples, 0),
                normalization_json=self.normalization_json_path.get() or None,
                a7_max_samples=self._int_or(self.a7_max_samples, 0),
                serial_max_samples=self._int_or(self.serial_max_samples, 0),
                log=lambda msg: self.after(0, self._log, msg),
                progress_cb=lambda i, total, label: self.after(0, self._set_step, i, label),
            )
            self.after(0, self._show_results, result)
        except Exception as e:
            err = str(e)
            self.after(0, self._on_failure, err)
        finally:
            self.after(0, self._finish)

    def _finish(self):
        self._running = False
        self.run_btn.config(state="normal")

    def _on_failure(self, err):
        self._log(f"[ERREUR] {err}")
        self._mark_failed()
        self._set_status("Échec du pipeline", C["bad"], C["bad"])
        messagebox.showerror("Erreur pipeline", err)

    # =================================================================
    # Affichage des résultats
    # =================================================================
    def _clear_table(self):
        for w in self.table_area.inner.winfo_children():
            w.destroy()

    def _show_results(self, result):
        self._last_result = result
        self._mark_all_done()
        elapsed = time.time() - self._t0 if self._t0 else 0

        self._log("")
        self._log(f">>> Cortex utilisé : {result['cortex_used']}")
        self._log(f">>> Dossier de sortie : {result['outputs_dir']}")
        self._log(f">>> Durée totale : {elapsed:.1f} s")

        df = result["metrics"]
        self._build_metrics_table(df)

        # cartes de synthèse
        f1_vals = [float(v) for v in df.get("F1", []) if not _is_na(v)]
        mae_vals = [float(v) for v in df.get("MAE (W)", []) if not _is_na(v)]
        f1_mean = sum(f1_vals) / len(f1_vals) if f1_vals else float("nan")
        mae_mean = sum(mae_vals) / len(mae_vals) if mae_vals else float("nan")

        self.cards["cortex"].set(result["cortex_used"].replace("Cortex-", "Cortex "),
                                 C["accent_dark"])
        self.cards["board"].set(self.board_name.get().split("-")[0])
        self.cards["samples"].set(f"{result.get('n_samples', 0):,}".replace(",", " "))
        self.cards["appliances"].set(str(len(df.index)))
        self.cards["f1"].set(
            fmt_value("F1", f1_mean),
            QUALITY_COLORS[quality("F1", f1_mean)][0])
        self.cards["mae"].set(
            fmt_value("MAE (W)", mae_mean) + " W" if not _is_na(mae_mean) else "N/A")
        self.cards["duration"].set(f"{int(elapsed // 60)}m {int(elapsed % 60)}s")

        self.nb.select(self.tab_res)
        self._set_status(
            f"Terminé — {len(df.index)} appareil(s) évalué(s) en {elapsed:.0f} s",
            C["ok"], C["ok"])
        self.status_right.config(text=str(result["outputs_dir"]))

    def _build_metrics_table(self, df):
        self._clear_table()
        parent = self.table_area.inner

        cols = [c for c in COLUMN_ORDER if c in df.columns]
        cols += [c for c in df.columns if c not in cols]     # aucune colonne perdue
        headers = ["Appareil"] + cols

        parent.columnconfigure(0, weight=3, minsize=150)
        for j in range(1, len(headers)):
            parent.columnconfigure(j, weight=2, minsize=95)

        # en-tête
        for j, h in enumerate(headers):
            lbl = tk.Label(parent, text=h, bg=C["ink"], fg="white", font=FONT_UI_B,
                           anchor="w" if j == 0 else "center", padx=10, pady=8)
            lbl.grid(row=0, column=j, sticky="nsew", padx=(0, 1), pady=(0, 1))
            if h in COLUMN_TOOLTIP:
                Tooltip(lbl, COLUMN_TOOLTIP[h])

        # lignes
        for i, (appliance, row) in enumerate(df.iterrows(), start=1):
            base_bg = C["surface"] if i % 2 else C["surface_alt"]
            thr = row.get("Seuil (W)", 15.0)

            tk.Label(parent, text=str(appliance), bg=base_bg, fg=C["ink"],
                     font=FONT_UI_B, anchor="w", padx=10, pady=7
                     ).grid(row=i, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))

            for j, col in enumerate(cols, start=1):
                v = row.get(col)
                q = quality(col, v, thr)
                fg, bg = QUALITY_COLORS[q]
                if q == "info":
                    bg = base_bg
                text = fmt_value(col, v)
                font = FONT_NUM_B if col == "F1" and q != "na" else FONT_NUM
                tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                         anchor="center", padx=10, pady=7
                         ).grid(row=i, column=j, sticky="nsew", padx=(0, 1), pady=(0, 1))

        note = tk.Label(parent,
                        text="RMSE / MAE sont colorées par rapport au seuil ON/OFF de "
                             "l'appareil (≤ 1× seuil = bon, ≤ 3× = moyen).  "
                             "N/A = appareil jamais actif sur la fenêtre testée.",
                        bg=C["surface"], fg=C["ink_soft"], font=FONT_SMALL,
                        anchor="w", justify="left", pady=10)
        note.grid(row=len(df.index) + 1, column=0, columnspan=len(headers), sticky="w")

    # =================================================================
    # Exports
    # =================================================================
    def _outputs_dir(self):
        if self._last_result:
            return Path(self._last_result["outputs_dir"])
        return Path("./run_output")

    def _open_outputs(self):
        d = self._outputs_dir().resolve()
        if not d.exists():
            messagebox.showinfo("Dossier introuvable", f"{d} n'existe pas encore.")
            return
        if sys.platform.startswith("win"):
            os.startfile(str(d))                                   # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])

    def _export_csv(self):
        if self._last_result is None:
            messagebox.showinfo("Aucun résultat", "Lancez d'abord le pipeline.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile="metrics_report.csv",
                                         filetypes=[("CSV", "*.csv")])
        if not p:
            return
        df = self._last_result["metrics"]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["appliance"] + list(df.columns))
            for appliance, row in df.iterrows():
                w.writerow([appliance] + list(row.values))
        self._set_status(f"Métriques exportées : {p}", C["ok"], C["ok"])

    def _export_html(self):
        if self._last_result is None:
            messagebox.showinfo("Aucun résultat", "Lancez d'abord le pipeline.")
            return
        out = self._outputs_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "rapport.html"
        path.write_text(self._render_html(), encoding="utf-8")
        self._log(f">>> Rapport HTML généré : {path}")
        self._set_status(f"Rapport HTML : {path}", C["ok"], C["ok"])
        webbrowser.open(path.resolve().as_uri())

    def _render_html(self):
        res = self._last_result
        df = res["metrics"]
        cols = [c for c in COLUMN_ORDER if c in df.columns]
        cols += [c for c in df.columns if c not in cols]

        head_cells = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body_rows = []
        for appliance, row in df.iterrows():
            thr = row.get("Seuil (W)", 15.0)
            cells = []
            for c in cols:
                q = quality(c, row.get(c), thr)
                cells.append(f'<td class="q-{q}">{html.escape(fmt_value(c, row.get(c)))}</td>')
            body_rows.append(f"<tr><th scope=\"row\">{html.escape(str(appliance))}</th>"
                             + "".join(cells) + "</tr>")

        log_html = "\n".join(
            f'<span class="ts">{t}</span> <span class="l-{self._tag_for(m)}">{html.escape(m)}</span>'
            for t, m in self._log_lines)

        meta = [
            ("Carte", self.board_name.get()),
            ("Cortex utilisé", res["cortex_used"]),
            ("Dataset", Path(self.dataset_path.get()).name or "—"),
            ("Modèle", Path(self.model_path.get()).name or "—"),
            ("Échantillons", str(res.get("n_samples", "—"))),
            ("Date", datetime.now().strftime("%d/%m/%Y %H:%M")),
        ]
        meta_html = "".join(
            f'<div class="card"><span>{html.escape(k)}</span><strong>{html.escape(v)}</strong></div>'
            for k, v in meta)

        css = """
        :root{--ink:#03234B;--soft:#5A6B85;--line:#D6DEE9;--ok:#1E9E6A;--okbg:#E4F5EE;
              --warn:#C77700;--warnbg:#FDF2DF;--bad:#D14343;--badbg:#FCE9E9;--na:#98A4B5;--nabg:#F2F4F7;}
        *{box-sizing:border-box}
        body{margin:0;background:#EEF1F6;color:var(--ink);
             font-family:"Segoe UI",system-ui,sans-serif;padding:0 0 60px}
        header{background:var(--ink);color:#fff;padding:26px 40px}
        header h1{margin:0;font-size:22px;font-weight:600}
        header p{margin:6px 0 0;color:#9FC7E8;font-size:13px}
        main{max-width:1100px;margin:26px auto;padding:0 20px}
        .cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
        .card{background:#fff;border:1px solid var(--line);border-radius:8px;
              padding:12px 16px;flex:1 1 160px;display:flex;flex-direction:column;gap:4px}
        .card span{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--soft)}
        .card strong{font-size:15px}
        h2{font-size:15px;margin:26px 0 10px}
        table{width:100%;border-collapse:separate;border-spacing:0 0;background:#fff;
              border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:14px}
        th,td{padding:10px 12px;text-align:center}
        thead th{background:var(--ink);color:#fff;font-weight:600;font-size:12.5px}
        tbody th{text-align:left;font-weight:600;background:#F6F8FB}
        tbody tr:nth-child(even) th{background:#EEF2F8}
        td{font-family:Consolas,monospace;border-top:1px solid var(--line)}
        .q-ok{color:var(--ok);background:var(--okbg)}
        .q-warn{color:var(--warn);background:var(--warnbg)}
        .q-bad{color:var(--bad);background:var(--badbg)}
        .q-na{color:var(--na);background:var(--nabg)}
        .q-info{color:var(--soft)}
        .legend{font-size:12px;color:var(--soft);margin-top:8px}
        pre{background:#0B1220;color:#C9D6E8;padding:18px;border-radius:8px;
            font-family:Consolas,monospace;font-size:12px;line-height:1.55;
            overflow-x:auto;white-space:pre-wrap}
        .ts{color:#5C6B85}
        .l-split{color:#5FD3F3}.l-cortex{color:#B9A2FF}.l-board,.l-serial{color:#54D8C4}
        .l-pack{color:#FF7BC0}.l-metrics{color:#FFD98A}.l-result{color:#7CE0A8;font-weight:700}
        .l-section{color:#fff;font-weight:700}.l-warn{color:#FFC15E}.l-error{color:#FF8B8B;font-weight:700}
        """

        return (
            "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Rapport — Banc de test NILM STM32</title>"
            f"<style>{css}</style></head><body>"
            "<header><h1>Rapport de test NILM — STM32</h1>"
            "<p>Désagrégation de charge : métriques par appareil et journal d'exécution</p></header>"
            f"<main><div class='cards'>{meta_html}</div>"
            "<h2>Métriques par appareil</h2>"
            f"<table><thead><tr><th>Appareil</th>{head_cells}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table>"
            "<p class='legend'>Vert = bon · orange = moyen · rouge = faible · gris = non "
            "applicable (appareil jamais actif sur la fenêtre testée). RMSE et MAE sont "
            "évaluées relativement au seuil ON/OFF de chaque appareil.</p>"
            "<h2>Journal d'exécution</h2>"
            f"<pre>{log_html}</pre></main></body></html>"
        )

    # =================================================================
    # Mode démo (aperçu de l'UI sans carte)
    # =================================================================
    def load_demo(self):
        import pandas as pd
        nan = float("nan")
        df = pd.DataFrame(
            {
                "RMSE (W)": [128.4, 21.7, 344.9, 6.2, nan],
                "MAE (W)":  [34.1, 9.8, 96.5, 1.4, nan],
                "EN%":      [86.3, 62.1, 41.7, 91.2, nan],
                "Precision": [0.91, 0.64, 0.38, 0.88, nan],
                "Recall":    [0.87, 0.55, 0.47, 0.93, nan],
                "F1":        [0.89, 0.59, 0.42, 0.90, nan],
                "Seuil (W)": [100.0, 50.0, 200.0, 5.0, 2.0],
            },
            index=["kettle", "fridge", "washing_machine", "router", "playstation"],
        )
        self.dataset_path.set("data/ukdale_house2.csv")
        self.model_path.set("models/seq2point.tflite")
        self._t0 = time.time() - 187
        for i, label in enumerate(STEP_LABELS, start=1):
            self._set_step(i, label)
        for msg in [
            "========== ETAPE 2/5 : Split dataset (sur le PC) ==========",
            "[SPLIT] aggregate.bin généré (14400 échantillons, dtype=float32)",
            "[CORTEX] .tflite tient dans la Flash M4 -> Cortex-M4 (TFLite-Micro)",
            "[BOARD] Connexion SSH à 192.168.32.5:22 ...",
            "[M4-PACK] ukdale.bin généré : en-tête 32o (magic 'UKDL') + 14400 échantillons",
            "[BOARD] Attention : 12/14400 échantillons sans réponse RESULT",
            "[METRICS] kettle          RMSE= 128.40W  MAE=  34.10W  F1=0.89",
        ]:
            self._log(msg)
        self._show_results({"cortex_used": "Cortex-M4", "metrics": df,
                            "outputs_dir": Path("./run_output"), "n_samples": 14400,
                            "appliances": list(df.index)})


if __name__ == "__main__":
    app = PipelineGUI()
    if "--demo" in sys.argv:
        app.after(300, app.load_demo)
    app.mainloop()
    