# gui.py – Tkinter interface
#
# Author: Vladislav Tislenko (keklick1337), 2025-2026

import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from . import PROJECT_URL, VERSION
from .core import (Cancelled, check_regions, decrypt_file, decrypted_name, download_firmware, format_size,
                   format_time, get_decryption_key)
from .csc import CSC_DICT
from .firmware import format_firmware_info
from .fus import resolve_version

XDA_URL = "https://xdaforums.com/t/gnsf-get-new-samsung-firmware-software-cross-platform-open-source.4732816/"
MONITOR_URL = "https://samsung-firmware.trustcrypt.com/"


def resource_path(*parts: str) -> str:
    """Path to a bundled resource (works from source and from a PyInstaller build)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


def open_folder(path: str) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


class ProgressView:
    """Status label + progress bar; computes speed/ETA from progress callbacks."""

    def __init__(self, parent: tk.Widget) -> None:
        self.frame = ttk.Frame(parent)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.frame, textvariable=self.status).pack(side=tk.LEFT)
        self.bar = ttk.Progressbar(self.frame, orient="horizontal", length=100, mode="determinate")
        self.bar.pack(fill=tk.X, expand=True, padx=(5, 0))
        self._stage = ""
        self._start = 0.0
        self._start_bytes = 0

    def reset(self, text: str = "Ready") -> None:
        self._stage = ""
        self.bar["value"] = 0
        self.status.set(text)

    def update(self, stage: str, done: int, total: int) -> None:
        now = time.time()
        if stage != self._stage:
            self._stage, self._start, self._start_bytes = stage, now, done
        elapsed = now - self._start
        speed = (done - self._start_bytes) / elapsed if elapsed > 0 else 0
        eta = f" • ETA {format_time((total - done) / speed)}" if speed > 0 else ""
        self.bar["value"] = done * 100 / total if total else 0
        self.status.set(f"{stage.capitalize()}: {format_size(done)}/{format_size(total)}"
                        f" • {format_size(speed)}/s{eta}")


class LogView(scrolledtext.ScrolledText):
    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, state="disabled", **kw)

    def write(self, msg: str) -> None:
        self.configure(state="normal")
        self.insert(tk.END, msg + "\n")
        self.see(tk.END)
        self.configure(state="disabled")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", tk.END)
        self.configure(state="disabled")


class GNSFGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"GetNewSamsungFirmware v{VERSION}")
        self.geometry("820x600")
        self.minsize(760, 520)
        self._set_icon()

        # Worker threads post callables here; the Tk thread runs them
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._cancel: Optional[threading.Event] = None
        self._action_buttons = []

        self._create_widgets()
        self.after(50, self._drain_ui_queue)

    # ------------------------------------------------------------------ #
    # Threading helpers
    # ------------------------------------------------------------------ #

    def ui(self, fn: Callable, *args) -> None:
        """Run fn(*args) on the Tk thread (safe to call from workers)."""
        self._ui_queue.put(lambda: fn(*args))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def _run_task(self, button: ttk.Button, idle_text: str, work: Callable[[threading.Event], None],
                  on_error: Callable[[Exception], None]) -> None:
        """Run `work` in a background thread; `button` becomes its Cancel button (see _cancel_task)."""
        cancel = threading.Event()
        self._cancel = cancel
        for b in self._action_buttons:
            b.configure(state="normal" if b is button else "disabled")
        button.configure(text="Cancel")

        def runner() -> None:
            try:
                work(cancel)
            except Cancelled:
                pass
            except Exception as e:
                self.ui(on_error, e)
            finally:
                self.ui(self._task_done, button, idle_text)

        threading.Thread(target=runner, daemon=True).start()

    def _cancel_task(self) -> bool:
        """Cancel the running task, if any. Returns True if one was running."""
        if self._cancel is None:
            return False
        self._cancel.set()
        return True

    def _task_done(self, button: ttk.Button, idle_text: str) -> None:
        self._cancel = None
        button.configure(text=idle_text)
        for b in self._action_buttons:
            b.configure(state="normal")

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _set_icon(self) -> None:
        for size in ("128", "256", "512"):
            path = resource_path("AppIcons", f"{size}.png")
            if os.path.exists(path):
                try:
                    self.iconphoto(True, tk.PhotoImage(file=path))
                except tk.TclError:
                    pass
                return

    def _create_widgets(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(top, text="Model:").pack(side=tk.LEFT)
        self.model_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.model_var, width=20).pack(side=tk.LEFT, padx=(5, 15))
        ttk.Label(top, text="CSC:").pack(side=tk.LEFT)
        self.csc_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.csc_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(top, text="e.g. SM-S928B / XSA, SM-L310 / XSG").pack(side=tk.LEFT, padx=10)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.download_tab = ttk.Frame(self.notebook)
        self.check_tab = ttk.Frame(self.notebook)
        self.decrypt_tab = ttk.Frame(self.notebook)
        self.info_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.download_tab, text="Download")
        self.notebook.add(self.check_tab, text="Check CSC Versions")
        self.notebook.add(self.decrypt_tab, text="Manual Decrypt")
        self.notebook.add(self.info_tab, text="Info")

        self._create_download_tab()
        self._create_check_tab()
        self._create_decrypt_tab()
        self._create_info_tab()

    def _create_download_tab(self) -> None:
        frm = ttk.Frame(self.download_tab)
        frm.pack(fill=tk.X, padx=5, pady=5)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Firmware Version:").grid(row=0, column=0, sticky=tk.W)
        self.ver_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.ver_var).grid(row=0, column=1, sticky=tk.EW)
        ttk.Button(frm, text="Latest", command=self._fill_latest).grid(row=0, column=2, padx=(5, 0))
        self.ver_var.trace_add("write", lambda *_: self._update_firmware_info())

        ttk.Label(frm, text="Out Dir:").grid(row=1, column=0, sticky=tk.W, pady=(5, 0))
        self.outdir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "SamsungFirmware"))
        ttk.Entry(frm, textvariable=self.outdir_var).grid(row=1, column=1, sticky=tk.EW, pady=(5, 0))
        ttk.Button(frm, text="Browse", command=self._browse_out).grid(row=1, column=2, padx=(5, 0), pady=(5, 0))

        self.nodecrypt_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Skip decrypt", variable=self.nodecrypt_var).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))
        ttk.Label(frm, text="Empty version = latest").grid(row=2, column=1, sticky=tk.E, pady=(5, 0))
        self.download_btn = ttk.Button(frm, text="Download", command=self._on_download)
        self.download_btn.grid(row=2, column=2, sticky=tk.E, padx=(5, 0), pady=(5, 0))
        self._action_buttons.append(self.download_btn)

        info = ttk.LabelFrame(self.download_tab, text="Firmware Information")
        info.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.fw_info = tk.StringVar(value="Enter a firmware version to see information")
        ttk.Label(info, textvariable=self.fw_info, justify=tk.LEFT).pack(anchor=tk.W, padx=5, pady=5)

        self.dl_progress = ProgressView(self.download_tab)
        self.dl_progress.frame.pack(fill=tk.X, padx=5, pady=(0, 5))

        logf = ttk.LabelFrame(self.download_tab, text="Log")
        logf.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log = LogView(logf, height=10)
        self.log.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _create_check_tab(self) -> None:
        frm = ttk.Frame(self.check_tab)
        frm.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(frm, text="Latest firmware per region (empty CSC = check all regions). "
                            "Double-click a row to download it.").pack(side=tk.LEFT)
        self.check_btn = ttk.Button(frm, text="Check Versions", command=self._on_check)
        self.check_btn.pack(side=tk.RIGHT)
        self._action_buttons.append(self.check_btn)

        self.check_progress = ProgressView(self.check_tab)
        self.check_progress.frame.pack(fill=tk.X, padx=5, pady=(0, 5))

        table = ttk.Frame(self.check_tab)
        table.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.tree = ttk.Treeview(table, columns=("csc", "name", "ver"), show="headings")
        for col, title, width in (("csc", "CSC", 70), ("name", "Region Name", 250), ("ver", "Latest Version", 380)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=50, stretch=col != "csc")
        self.tree.bind("<Double-1>", self._on_select)
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.grid(row=0, column=1, sticky=tk.NS)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

    def _create_decrypt_tab(self) -> None:
        frm = ttk.Frame(self.decrypt_tab, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.columnconfigure(1, weight=1)

        self.enc_file_var = tk.StringVar()
        self.dec_file_var = tk.StringVar()
        self.dec_ver_var = tk.StringVar()
        rows = (
            ("Encrypted File:", self.enc_file_var, self._browse_enc_file),
            ("Output File:", self.dec_file_var, self._browse_dec_file),
            ("Firmware Version:", self.dec_ver_var, None),
        )
        for i, (label, var, browse) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky=tk.W)
            ttk.Entry(frm, textvariable=var).grid(row=i, column=1, sticky=tk.EW, pady=3)
            if browse:
                ttk.Button(frm, text="Browse", command=browse).grid(row=i, column=2, padx=(5, 0))
        self.enc_file_var.trace_add("write", lambda *_: self._on_enc_file_change())

        ttk.Label(frm, text="Model, CSC and the exact firmware version of the file are required. "
                            "Encryption type is detected from the file name.").grid(
            row=3, column=0, columnspan=3, sticky=tk.W, pady=(5, 0))
        self.decrypt_btn = ttk.Button(frm, text="Decrypt File", command=self._on_manual_decrypt)
        self.decrypt_btn.grid(row=4, column=0, sticky=tk.W, pady=(10, 0))
        self._action_buttons.append(self.decrypt_btn)

        self.dec_progress = ProgressView(frm)
        self.dec_progress.frame.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(10, 0))

        logf = ttk.LabelFrame(frm, text="Decrypt Log")
        logf.grid(row=6, column=0, columnspan=3, sticky=tk.NSEW, pady=(10, 0))
        frm.rowconfigure(6, weight=1)
        self.dec_log = LogView(logf, height=10)
        self.dec_log.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _create_info_tab(self) -> None:
        frm = ttk.Frame(self.info_tab, padding=15)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="GetNewSamsungFirmware", font=("TkDefaultFont", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(frm, text=f"Version {VERSION} • By Keklick1337 (Vladislav Tislenko)").pack(anchor=tk.W, pady=(0, 10))
        for title, url in (("Project page:", PROJECT_URL), ("XDA Forums discussion:", XDA_URL),
                           ("Samsung Firmware Monitor:", MONITOR_URL)):
            ttk.Label(frm, text=title).pack(anchor=tk.W)
            link = ttk.Label(frm, text=url, foreground="blue", cursor="hand2")
            link.pack(anchor=tk.W, pady=(0, 5))
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open_new(u))

        about = ttk.LabelFrame(frm, text="About")
        about.pack(fill=tk.BOTH, expand=True, pady=10)
        ttk.Label(about, justify=tk.LEFT, wraplength=700, text=(
            "GNSF downloads and decrypts Samsung firmware packages straight from Samsung servers.\n\n"
            "• Phones, tablets and Galaxy Watch firmware\n"
            "• No IMEI or serial number needed\n"
            "• Automatic decryption of .enc2/.enc4 packages\n"
            "• Interrupted downloads are resumed automatically\n"
            "• Check the latest firmware across all regions\n\n"
            "A command-line version (gnsf) is also available."
        )).pack(anchor=tk.W, padx=5, pady=5)

    # ------------------------------------------------------------------ #
    # Common
    # ------------------------------------------------------------------ #

    def _model_csc(self, need_csc: bool = True) -> Optional[tuple]:
        model = self.model_var.get().strip().upper()
        csc = self.csc_var.get().strip().upper()
        missing = [n for n, v in (("Model", model), ("CSC", csc if need_csc else "x")) if not v]
        if missing:
            messagebox.showerror("Error", f"Required: {', '.join(missing)}")
            return None
        return model, csc

    def _update_firmware_info(self) -> None:
        ver = self.ver_var.get().strip()
        self.fw_info.set(format_firmware_info(ver) if ver else "Enter a firmware version to see information")

    def _fill_latest(self) -> None:
        mc = self._model_csc()
        if not mc:
            return

        def work() -> None:
            try:
                ver = resolve_version(mc[0], mc[1], None)
                self.ui(self.ver_var.set, ver)
            except Exception as e:
                self.ui(messagebox.showerror, "Error", str(e))

        threading.Thread(target=work, daemon=True).start()

    def _browse_out(self) -> None:
        d = filedialog.askdirectory(initialdir=self.outdir_var.get())
        if d:
            self.outdir_var.set(d)

    # ------------------------------------------------------------------ #
    # Download
    # ------------------------------------------------------------------ #

    def _on_download(self) -> None:
        if self._cancel_task():
            self.log.write("Cancelled (start the download again to resume)")
            return
        mc = self._model_csc()
        if not mc:
            return
        model, csc = mc
        ver = self.ver_var.get().strip()
        outdir = self.outdir_var.get().strip()
        decrypt = not self.nodecrypt_var.get()
        log = lambda msg: self.ui(self.log.write, msg)
        progress = lambda *a: self.ui(self.dl_progress.update, *a)

        def work(cancel: threading.Event) -> None:
            log("Querying firmware info...")
            full = resolve_version(model, csc, ver)
            if full != ver:
                self.ui(self.ver_var.set, full)
            path = download_firmware(model, csc, full, outdir, decrypt=decrypt,
                                     log=log, progress=progress, cancel=cancel)
            log(f"Saved to {path}")
            self.ui(self.dl_progress.status.set, "Complete")
            self.ui(open_folder, outdir)

        def on_error(e: Exception) -> None:
            self.log.write(f"Error: {e}")
            self.dl_progress.status.set("Error")
            messagebox.showerror("Error", str(e))

        self.dl_progress.reset("Starting...")
        self._run_task(self.download_btn, "Download", work, on_error)

    # ------------------------------------------------------------------ #
    # Check
    # ------------------------------------------------------------------ #

    def _on_check(self) -> None:
        if self._cancel_task():
            return
        mc = self._model_csc(need_csc=False)
        if not mc:
            return
        model, csc = mc
        regions = [csc] if csc else list(CSC_DICT)
        total = len(regions)
        self.tree.delete(*self.tree.get_children())
        self.check_progress.reset(f"Checking {total} regions...")
        counter = {"done": 0, "found": 0}

        def add_result(region: str, ver: Optional[str], err: Optional[str]) -> None:
            counter["done"] += 1
            if ver:
                counter["found"] += 1
                self.tree.insert("", tk.END, values=(region, CSC_DICT.get(region, ""), ver))
            elif len(regions) == 1:
                self.check_progress.status.set(f"{region}: {err}")
                return
            self.check_progress.bar["value"] = counter["done"] * 100 / total
            self.check_progress.status.set(f"Checked {counter['done']}/{total} • found in {counter['found']}")

        def work(cancel: threading.Event) -> None:
            check_regions(model, regions, lambda *r: self.ui(add_result, *r), cancel)

        self._run_task(self.check_btn, "Check Versions", work,
                       lambda e: messagebox.showerror("Error", str(e)))

    def _on_select(self, _evt) -> None:
        sel = self.tree.selection()
        if not sel or self._cancel is not None:
            return
        code, _name, ver = self.tree.item(sel[0], "values")
        self.csc_var.set(code)
        self.ver_var.set(ver)
        self.dec_ver_var.set(ver)
        self.notebook.select(self.download_tab)

    # ------------------------------------------------------------------ #
    # Manual decrypt
    # ------------------------------------------------------------------ #

    def _on_enc_file_change(self) -> None:
        name = self.enc_file_var.get()
        if name.endswith((".enc2", ".enc4")) and not self.dec_file_var.get():
            self.dec_file_var.set(decrypted_name(name))

    def _browse_enc_file(self) -> None:
        name = filedialog.askopenfilename(
            title="Select Encrypted File",
            filetypes=[("Encrypted Files", "*.enc2 *.enc4"), ("All Files", "*.*")])
        if name:
            self.dec_file_var.set("")
            self.enc_file_var.set(name)

    def _browse_dec_file(self) -> None:
        src = self.enc_file_var.get()
        name = filedialog.asksaveasfilename(
            title="Save Decrypted File As",
            initialfile=os.path.basename(decrypted_name(src)) if src else "")
        if name:
            self.dec_file_var.set(name)

    def _on_manual_decrypt(self) -> None:
        if self._cancel_task():
            return
        mc = self._model_csc()
        if not mc:
            return
        model, csc = mc
        src = self.enc_file_var.get().strip()
        dst = self.dec_file_var.get().strip()
        ver = self.dec_ver_var.get().strip()
        if not src or not dst or not ver:
            messagebox.showerror("Error", "Encrypted file, output file and firmware version are required")
            return
        if not os.path.exists(src):
            messagebox.showerror("Error", f"Input file doesn't exist: {src}")
            return
        if src.endswith(".enc2"):
            enc_ver = 2
        elif src.endswith(".enc4"):
            enc_ver = 4
        else:
            messagebox.showerror("Error", "Input file must end with .enc2 or .enc4")
            return

        log = lambda msg: self.ui(self.dec_log.write, msg)

        def work(cancel: threading.Event) -> None:
            log(f"Getting ENC{enc_ver} key for {ver}...")
            key = get_decryption_key(enc_ver, model, csc, ver)
            log(f"Decrypting {os.path.basename(src)}")
            decrypt_file(src, dst, key, lambda *a: self.ui(self.dec_progress.update, *a), cancel)
            log(f"Decrypted to {dst}")
            self.ui(self.dec_progress.status.set, "Complete")

        def on_error(e: Exception) -> None:
            self.dec_log.write(f"Error: {e}")
            self.dec_progress.status.set("Error")
            messagebox.showerror("Decryption Error", str(e))

        self.dec_log.clear()
        self.dec_progress.reset("Starting...")
        self._run_task(self.decrypt_btn, "Decrypt File", work, on_error)


def main() -> int:
    GNSFGUI().mainloop()
    return 0
