"""
BluetoothScanner.py
====================================
This is an example file with correct docstring examples

| Author: Sandler Wiekamp
| Date: 2026 November 10

ble_explorer_gui.py - Bluetooth Low Energy scanner / explorer with a GUI.

Features
  * Continuous scan for nearby BLE devices, list refreshed every 2 seconds
  * Devices sorted from weakest signal to strongest
  * Your selected device stays selected while it moves around the list
  * Connect to the selected device (double-click works too)
  * Cancel a connection attempt that is still in progress
  * Browse the device's services, then a service's characteristics
  * Read, write and subscribe to notifications/indications on characteristics
  * Disconnect

Requirements
  pip install bleak            (tkinter ships with Python; on Debian/Ubuntu
                                you may need: sudo apt install python3-tk)

Run
  python BluetoothScanner.py

Platform notes
  * Linux: BlueZ must be running. If scanning fails with a permissions error,
    add your user to the 'bluetooth' group or run with sudo.
  * macOS: give your terminal / IDE Bluetooth permission in
    System Settings > Privacy & Security > Bluetooth. Addresses shown are
    UUIDs assigned by macOS, not real MAC addresses.
  * Windows 10/11: works out of the box with a built-in or USB BLE adapter.

Design
  tkinter is not thread-safe and bleak is asyncio-based, so an asyncio event
  loop runs in a background thread. The GUI hands coroutines to that loop, and
  results come back through a queue the GUI drains every 50 ms. Tk widgets are
  only ever touched from the main thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    print("Error: the 'bleak' package is required. Install it with:\n"
          "    pip install bleak", file=sys.stderr)
    sys.exit(1)


REFRESH_MS = 2000          # device list refresh interval
STALE_AFTER_S = 30         # drop devices not heard from for this long
CONNECT_TIMEOUT_S = 20     # give up on a connection attempt after this long
MAX_LOG_LINES = 1000

# A short list of common Bluetooth SIG company identifiers.
# Full list: https://www.bluetooth.com/specifications/assigned-numbers/
COMPANY_IDS = {
    0x0002: "Intel",
    0x0006: "Microsoft",
    0x000F: "Broadcom",
    0x004C: "Apple",
    0x0059: "Nordic Semiconductor",
    0x0075: "Samsung",
    0x0087: "Garmin",
    0x00E0: "Google",
    0x0157: "Huami (Amazfit)",
    0x0171: "Amazon",
    0x02E5: "Espressif",
    0x038F: "Xiaomi",
    0x0499: "Ruuvi Innovations",
}

WRITE_FORMATS = ("Text (UTF-8)", "Hex", "Decimal bytes")


def company_name(company_id: int) -> str:
    return COMPANY_IDS.get(company_id, f"0x{company_id:04X}")


def parse_write_input(text: str, fmt: str) -> bytes:
    """Turn what the user typed into bytes. Raises ValueError on bad input."""
    if fmt == "Hex":
        cleaned = re.sub(r"0x", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[\s,:\-]", "", cleaned)
        if len(cleaned) % 2:
            raise ValueError("Hex input needs an even number of digits, e.g. 01 A0 FF")
        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            raise ValueError("Hex input may only contain 0-9 and A-F, e.g. 01 A0 FF") from None
    if fmt == "Decimal bytes":
        parts = [p for p in re.split(r"[\s,]+", text.strip()) if p]
        try:
            values = [int(p) for p in parts]
        except ValueError:
            raise ValueError("Decimal bytes must be whole numbers, e.g. 1 160 255") from None
        if any(v < 0 or v > 255 for v in values):
            raise ValueError("Each decimal byte must be between 0 and 255")
        return bytes(values)
    return text.encode("utf-8")


def format_value(data: bytes) -> str:
    """Show bytes as hex, plus text and integer views where they make sense."""
    if not data:
        return "(empty - 0 bytes)"
    lines = [f"Hex ({len(data)} bytes): {data.hex(' ').upper()}"]
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            lines.append(f"Text: {text}")
    except UnicodeDecodeError:
        pass
    if len(data) in (1, 2, 4, 8):
        lines.append(f"Integer (little-endian): {int.from_bytes(data, 'little')}"
                     f"  /  signed: {int.from_bytes(data, 'little', signed=True)}")
    return "\n".join(lines)


@dataclass
class SeenDevice:
    device: Any                 # bleak BLEDevice, needed to connect
    name: Optional[str]
    rssi: int
    last_seen: float            # time.monotonic()
    manufacturer: str = ""


class AsyncWorker:
    """Runs an asyncio event loop in a background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="ble-asyncio", daemon=True)
        self.thread.start()
        self._ready.wait()

    def _run(self) -> None:
        if sys.platform == "win32":
            # bleak needs this thread to be in a multi-threaded COM apartment.
            try:
                from bleak.backends.winrt.util import uninitialize_sta
                uninitialize_sta()
            except Exception:
                pass
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn: Callable, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class BLEExplorer:
    """The application window and all of its behaviour."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.worker = AsyncWorker()
        self.ui_queue: queue.Queue = queue.Queue()

        # Shared between the asyncio thread (writer) and GUI thread (reader).
        self._seen_lock = threading.Lock()
        self._seen: dict[str, SeenDevice] = {}

        # Only touched from the asyncio thread.
        self._scanner: Optional[BleakScanner] = None
        self._scan_lock: Optional[asyncio.Lock] = None
        self._connect_task: Optional[asyncio.Task] = None

        # GUI-thread state.  state is one of: idle, connecting, connected, disconnecting
        self.state = "idle"
        self.scan_wanted = True
        self.client: Optional[BleakClient] = None
        self.connected_address: Optional[str] = None
        self.connected_label = ""
        self.connecting_address: Optional[str] = None
        self.connecting_label = ""
        self.cancel_requested = False
        self.services: dict[str, Any] = {}       # tree iid -> BleakGATTService
        self.chars: dict[str, Any] = {}          # tree iid -> BleakGATTCharacteristic
        self.notifying: set[int] = set()         # characteristic handles
        self.last_values: dict[int, str] = {}    # characteristic handle -> formatted value

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_ui_queue()
        self._set_scanning(True)
        self._refresh_loop()
        self._update_controls()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self) -> None:
        root = self.root
        root.title("BLE Explorer")
        root.geometry("1280x800")
        root.minsize(960, 620)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # Toolbar
        bar = ttk.Frame(root, padding=(8, 6))
        bar.pack(side="top", fill="x")
        self.scan_btn = ttk.Button(bar, text="Stop scanning", width=15,
                                   command=lambda: self._set_scanning(not self.scan_wanted))
        self.scan_btn.pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.connect_btn = ttk.Button(bar, text="Connect", command=self.connect_selected)
        self.connect_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel connection", command=self.cancel_connect)
        self.cancel_btn.pack(side="left", padx=(4, 0))
        self.disconnect_btn = ttk.Button(bar, text="Disconnect", command=self.disconnect)
        self.disconnect_btn.pack(side="left", padx=(4, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.hide_unnamed = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Hide unnamed devices", variable=self.hide_unnamed,
                        command=self._update_device_list).pack(side="left")
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(bar, textvariable=self.status_var, anchor="e").pack(side="right")

        # Log (packed before the main area so it keeps its height)
        log_frame = ttk.LabelFrame(root, text="Log", padding=4)
        log_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        main = ttk.PanedWindow(root, orient="horizontal")
        main.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        # Left: devices
        self.devices_frame = ttk.LabelFrame(main, padding=4)
        main.add(self.devices_frame, weight=3)
        self.tree = self._make_tree(self.devices_frame, [
            ("name", "Name", 170, "w"),
            ("address", "Address", 170, "w"),
            ("rssi", "RSSI (dBm)", 80, "e"),
            ("mfr", "Manufacturer", 120, "w"),
            ("seen", "Last seen", 75, "e"),
        ])
        self.tree.tag_configure("connected", background="#c8ecc8")
        self.tree.tag_configure("connecting", background="#fff2b3")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_controls())
        self.tree.bind("<Double-1>", self._on_device_double_click)

        # Right: services / characteristics / operations
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=4)

        svc_frame = ttk.LabelFrame(right, text="Services", padding=4)
        right.add(svc_frame, weight=2)
        self.conn_var = tk.StringVar(value="Not connected")
        ttk.Label(svc_frame, textvariable=self.conn_var, font=("TkDefaultFont", 10, "bold")
                  ).pack(side="top", anchor="w", pady=(0, 4))
        self.svc_tree = self._make_tree(svc_frame, [
            ("desc", "Service", 190, "w"),
            ("uuid", "UUID", 270, "w"),
            ("handle", "Handle", 65, "e"),
            ("count", "Chars", 50, "e"),
        ])
        self.svc_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_service_select())

        char_frame = ttk.LabelFrame(right, text="Characteristics of selected service", padding=4)
        right.add(char_frame, weight=2)
        self.char_tree = self._make_tree(char_frame, [
            ("desc", "Characteristic", 170, "w"),
            ("uuid", "UUID", 270, "w"),
            ("handle", "Handle", 65, "e"),
            ("props", "Properties", 210, "w"),
            ("notify", "Notifying", 70, "center"),
        ])
        self.char_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_char_select())

        ops = ttk.LabelFrame(right, text="Selected characteristic", padding=6)
        right.add(ops, weight=1)
        ops.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(ops)
        btn_row.grid(row=0, column=0, columnspan=4, sticky="w")
        self.read_btn = ttk.Button(btn_row, text="Read", command=self.read_char)
        self.read_btn.pack(side="left")
        self.notify_btn = ttk.Button(btn_row, text="Start notify", width=13,
                                     command=self.toggle_notify)
        self.notify_btn.pack(side="left", padx=(4, 0))

        ttk.Label(ops, text="Write:").grid(row=1, column=0, sticky="w", pady=4)
        self.write_var = tk.StringVar()
        self.write_entry = ttk.Entry(ops, textvariable=self.write_var)
        self.write_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        self.write_entry.bind("<Return>", lambda _e: self.write_char())
        self.write_fmt = tk.StringVar(value=WRITE_FORMATS[0])
        ttk.Combobox(ops, textvariable=self.write_fmt, values=WRITE_FORMATS,
                     state="readonly", width=14).grid(row=1, column=2, pady=4)
        self.write_btn = ttk.Button(ops, text="Write", command=self.write_char)
        self.write_btn.grid(row=1, column=3, padx=(4, 0), pady=4)

        ttk.Label(ops, text="Value:").grid(row=2, column=0, sticky="nw")
        self.value_text = tk.Text(ops, height=4, wrap="word", state="disabled")
        self.value_text.grid(row=2, column=1, columnspan=3, sticky="nsew", padx=4)
        ops.rowconfigure(2, weight=1)

    @staticmethod
    def _make_tree(parent, columns) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(side="top", fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns],
                            show="headings", selectmode="browse")
        for col_id, heading, width, anchor in columns:
            tree.heading(col_id, text=heading, anchor=anchor)
            tree.column(col_id, width=width, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    @staticmethod
    def _enable(widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _update_controls(self) -> None:
        s = self.state
        self._enable(self.connect_btn, s == "idle" and bool(self.tree.selection()))
        self._enable(self.cancel_btn, s == "connecting" and not self.cancel_requested)
        self._enable(self.disconnect_btn, s == "connected")

        ch = self._selected_char() if s == "connected" else None
        props = set(ch.properties) if ch is not None else set()
        self._enable(self.read_btn, "read" in props)
        can_write = bool(props & {"write", "write-without-response"})
        self._enable(self.write_btn, can_write)
        self._enable(self.write_entry, can_write)
        self._enable(self.notify_btn, bool(props & {"notify", "indicate"}))
        notifying = ch is not None and ch.handle in self.notifying
        self.notify_btn.configure(text="Stop notify" if notifying else "Start notify")

        if s == "connecting":
            status = ("Cancelling..." if self.cancel_requested
                      else f"Connecting to {self.connecting_label}...")
        elif s == "connected":
            status = f"Connected to {self.connected_label}"
        elif s == "disconnecting":
            status = "Disconnecting..."
        else:
            status = "Idle"
        scan = "scanning" if self.scan_wanted else "scan stopped"
        if s == "connecting":
            scan = "scan paused"
        self.status_var.set(f"{status}  |  {scan}")

    # ------------------------------------------------- thread plumbing --

    def run_async(self, coro, on_done: Callable[[concurrent.futures.Future], None]):
        """Run a coroutine on the asyncio thread; call on_done(future) on the GUI thread."""
        fut = self.worker.submit(coro)
        fut.add_done_callback(lambda f: self.ui_queue.put(lambda: on_done(f)))
        return fut

    def _poll_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:  # keep the GUI alive no matter what
                self.log(f"Internal error: {type(exc).__name__}: {exc}")
        self.root.after(50, self._poll_ui_queue)

    @staticmethod
    def _error_text(fut: concurrent.futures.Future) -> Optional[str]:
        """None if the future succeeded, otherwise a readable error message."""
        if fut.cancelled():
            return "cancelled"
        exc = fut.exception()
        if exc is None:
            return None
        if isinstance(exc, asyncio.TimeoutError):
            return "timed out"
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    # -------------------------------------------------------- scanning --

    def _get_scan_lock(self) -> asyncio.Lock:
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        return self._scan_lock

    async def _start_scan(self) -> None:
        async with self._get_scan_lock():
            if self._scanner is not None:
                return
            scanner = BleakScanner(detection_callback=self._on_advertisement)
            await scanner.start()
            self._scanner = scanner

    async def _stop_scan(self) -> None:
        async with self._get_scan_lock():
            scanner, self._scanner = self._scanner, None
            if scanner is not None:
                try:
                    await scanner.stop()
                except Exception:
                    pass

    def _on_advertisement(self, device, adv) -> None:
        """Called by bleak (asyncio thread) for every advertisement received."""
        name = adv.local_name or device.name
        mfr = ", ".join(company_name(cid) for cid in (adv.manufacturer_data or {}))
        rssi = adv.rssi if adv.rssi is not None else -127
        with self._seen_lock:
            prev = self._seen.get(device.address)
            self._seen[device.address] = SeenDevice(
                device=device,
                name=name or (prev.name if prev else None),
                rssi=rssi,
                last_seen=time.monotonic(),
                manufacturer=mfr or (prev.manufacturer if prev else ""),
            )

    def _set_scanning(self, wanted: bool) -> None:
        self.scan_wanted = wanted
        self.scan_btn.configure(text="Stop scanning" if wanted else "Start scanning")
        self._update_controls()
        if self.state == "connecting":
            return  # scanning is paused during a connection attempt and resumes after
        self.run_async(self._start_scan() if wanted else self._stop_scan(),
                       lambda f: self._on_scan_toggled(f, wanted))

    def _on_scan_toggled(self, fut, wanted: bool) -> None:
        err = self._error_text(fut)
        if err and wanted:
            self.scan_wanted = False
            self.scan_btn.configure(text="Start scanning")
            self.log(f"Could not start scanning: {err}")
            self.log("Check that Bluetooth is on and this program is allowed to use it.")
        elif wanted:
            self.log("Scanning for devices...")
        else:
            self.log("Scanning stopped.")
        self._update_controls()

    def _refresh_loop(self) -> None:
        try:
            self._update_device_list()
        finally:
            self.root.after(REFRESH_MS, self._refresh_loop)

    def _update_device_list(self) -> None:
        """Re-sort and redraw the device list, keeping the selection.

        Rows use the device address as their Treeview id and are moved rather
        than deleted/re-inserted, so the selected row stays selected even when
        its position in the list changes.
        """
        now = time.monotonic()
        keep = set(self.tree.selection())
        keep.update(a for a in (self.connected_address, self.connecting_address) if a)

        with self._seen_lock:
            for addr in [a for a, d in self._seen.items()
                         if now - d.last_seen > STALE_AFTER_S and a not in keep]:
                del self._seen[addr]
            devices = list(self._seen.values())

        if self.hide_unnamed.get():
            devices = [d for d in devices if d.name or d.device.address in keep]
        devices.sort(key=lambda d: (d.rssi, d.device.address))  # weakest first

        wanted = {d.device.address for d in devices}
        for iid in self.tree.get_children():
            if iid not in wanted:
                self.tree.delete(iid)

        for index, d in enumerate(devices):
            addr = d.device.address
            age = now - d.last_seen
            values = (d.name or "(unknown)", addr, d.rssi, d.manufacturer or "-",
                      "now" if age < 1 else f"{age:.0f}s ago")
            if addr == self.connected_address:
                tags: tuple = ("connected",)
            elif addr == self.connecting_address:
                tags = ("connecting",)
            else:
                tags = ()
            if self.tree.exists(addr):
                self.tree.item(addr, values=values, tags=tags)
                self.tree.move(addr, "", index)
            else:
                self.tree.insert("", index, iid=addr, values=values, tags=tags)

        self.devices_frame.configure(
            text=f"Nearby devices: {len(devices)}  (weakest -> strongest signal, "
                 f"refreshes every {REFRESH_MS // 1000} s)")
        self._update_controls()

    # ------------------------------------------------------ connecting --

    def _device_label(self, addr: str) -> str:
        with self._seen_lock:
            entry = self._seen.get(addr)
        return f"{entry.name} ({addr})" if entry and entry.name else addr

    def _on_device_double_click(self, event) -> None:
        if self.tree.identify_row(event.y):
            self.connect_selected()

    def connect_selected(self) -> None:
        if self.state != "idle":
            return
        selection = self.tree.selection()
        if not selection:
            return
        addr = selection[0]
        with self._seen_lock:
            entry = self._seen.get(addr)
        if entry is None:
            self.log("That device is no longer in range.")
            return

        self.state = "connecting"
        self.cancel_requested = False
        self.connecting_address = addr
        self.connecting_label = self._device_label(addr)
        self.log(f"Connecting to {self.connecting_label}...")
        self._update_device_list()
        self.run_async(self._connect(entry.device), lambda f: self._on_connect_done(f, addr))

    async def _connect(self, device) -> BleakClient:
        """asyncio thread. Pauses scanning while connecting (more reliable on all OSes)."""
        self._connect_task = asyncio.current_task()
        client: Optional[BleakClient] = None
        try:
            if self.cancel_requested:
                raise asyncio.CancelledError()
            await self._stop_scan()
            new_client = BleakClient(device, disconnected_callback=self._on_link_lost_async,
                                     timeout=CONNECT_TIMEOUT_S)
            client = new_client
            await new_client.connect()
            return new_client
        except BaseException:
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), 5)
                except BaseException:
                    pass
            raise
        finally:
            self._connect_task = None
            if self.scan_wanted:
                try:
                    await self._start_scan()
                except Exception:
                    pass

    def cancel_connect(self) -> None:
        if self.state != "connecting" or self.cancel_requested:
            return
        self.cancel_requested = True
        self.log("Cancelling connection attempt...")
        self.worker.call(self._cancel_connect_task)
        self._update_controls()

    def _cancel_connect_task(self) -> None:
        """asyncio thread."""
        task = self._connect_task
        if task is not None and not task.done():
            task.cancel()

    def _on_connect_done(self, fut, addr: str) -> None:
        label = self.connecting_label
        self.connecting_address = None
        err = self._error_text(fut)

        if err == "cancelled" or (err is None and self.cancel_requested):
            if err is None:  # connected just before the cancel landed: undo it
                self.run_async(self._disconnect_client(fut.result()), lambda f: None)
            self.state = "idle"
            self.log("Connection attempt cancelled.")
        elif err is not None:
            self.state = "idle"
            self.log(f"Could not connect to {label}: {err}")
        else:
            client = fut.result()
            if not client.is_connected:
                self.state = "idle"
                self.log(f"{label} disconnected immediately after connecting.")
            else:
                self.client = client
                self.connected_address = addr
                self.connected_label = label
                self.state = "connected"
                self.log(f"Connected to {label}.")
                self._show_services()

        self.cancel_requested = False
        self._set_scanning_label()
        self._update_device_list()

    def _set_scanning_label(self) -> None:
        self.scan_btn.configure(text="Stop scanning" if self.scan_wanted else "Start scanning")

    def disconnect(self) -> None:
        if self.state != "connected" or self.client is None:
            return
        client = self.client
        self.state = "disconnecting"
        self.log(f"Disconnecting from {self.connected_label}...")
        self._update_controls()
        self.run_async(self._disconnect_client(client),
                       lambda f: self._on_link_lost(client))

    async def _disconnect_client(self, client) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception:
            pass

    def _on_link_lost_async(self, client) -> None:
        """bleak's disconnected_callback (asyncio thread)."""
        self.ui_queue.put(lambda: self._on_link_lost(client))

    def _on_link_lost(self, client) -> None:
        """GUI thread. Safe to call more than once for the same client."""
        if client is not self.client:
            return
        expected = self.state == "disconnecting"
        label = self.connected_label
        self.client = None
        self.connected_address = None
        self.connected_label = ""
        self.state = "idle"
        self.notifying.clear()
        self.last_values.clear()
        self._clear_services()
        self.log(f"Disconnected from {label}." if expected
                 else f"{label} disconnected unexpectedly.")
        self._update_device_list()

    # ------------------------------------------- services & characteristics --

    def _clear_services(self) -> None:
        self.svc_tree.delete(*self.svc_tree.get_children())
        self.char_tree.delete(*self.char_tree.get_children())
        self.services.clear()
        self.chars.clear()
        self.conn_var.set("Not connected")
        self._set_text(self.value_text, "")

    def _show_services(self) -> None:
        self._clear_services()
        if self.client is None:
            return
        services = sorted(self.client.services, key=lambda s: s.handle)
        self.conn_var.set(f"{self.connected_label}: {len(services)} service(s)")
        for svc in services:
            iid = f"s{svc.handle}"
            self.services[iid] = svc
            self.svc_tree.insert("", "end", iid=iid, values=(
                svc.description or "Unknown service", svc.uuid,
                f"0x{svc.handle:04X}", len(svc.characteristics)))
        self.log(f"Found {len(services)} service(s). Select one to see its characteristics.")

    def _on_service_select(self) -> None:
        self.char_tree.delete(*self.char_tree.get_children())
        self.chars.clear()
        selection = self.svc_tree.selection()
        svc = self.services.get(selection[0]) if selection else None
        if svc is not None:
            for ch in sorted(svc.characteristics, key=lambda c: c.handle):
                iid = f"c{ch.handle}"
                self.chars[iid] = ch
                self.char_tree.insert("", "end", iid=iid, values=(
                    ch.description or "Unknown characteristic", ch.uuid,
                    f"0x{ch.handle:04X}", ", ".join(ch.properties),
                    "on" if ch.handle in self.notifying else ""))
        self._on_char_select()

    def _selected_char(self):
        selection = self.char_tree.selection()
        return self.chars.get(selection[0]) if selection else None

    def _on_char_select(self) -> None:
        ch = self._selected_char()
        self._set_text(self.value_text, self.last_values.get(ch.handle, "") if ch else "")
        self._update_controls()

    def _char_label(self, ch) -> str:
        desc = ch.description or "Unknown"
        return f"{desc} [{ch.uuid}]"

    def _show_value(self, ch, data: bytes) -> None:
        text = format_value(bytes(data))
        self.last_values[ch.handle] = text
        if self._selected_char() is ch:
            self._set_text(self.value_text, text)

    def read_char(self) -> None:
        ch, client = self._selected_char(), self.client
        if ch is None or client is None or "read" not in ch.properties:
            return
        self.log(f"Reading {self._char_label(ch)}...")
        self.run_async(client.read_gatt_char(ch),
                       lambda f: self._on_read_done(f, client, ch))

    def _on_read_done(self, fut, client, ch) -> None:
        if client is not self.client:
            return
        err = self._error_text(fut)
        if err:
            self.log(f"Read failed for {self._char_label(ch)}: {err}")
            return
        data = bytes(fut.result())
        self._show_value(ch, data)
        self.log(f"Read {self._char_label(ch)}: {data.hex(' ').upper() or '(empty)'}")

    def write_char(self) -> None:
        ch, client = self._selected_char(), self.client
        if ch is None or client is None:
            return
        if "write" in ch.properties:
            response = True
        elif "write-without-response" in ch.properties:
            response = False
        else:
            return
        try:
            data = parse_write_input(self.write_var.get(), self.write_fmt.get())
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc), parent=self.root)
            return
        self.log(f"Writing {data.hex(' ').upper() or '(empty)'} to {self._char_label(ch)}"
                 f" ({'with' if response else 'without'} response)...")
        self.run_async(client.write_gatt_char(ch, data, response=response),
                       lambda f: self._on_write_done(f, client, ch))

    def _on_write_done(self, fut, client, ch) -> None:
        if client is not self.client:
            return
        err = self._error_text(fut)
        self.log(f"Write failed for {self._char_label(ch)}: {err}" if err
                 else f"Write to {self._char_label(ch)} succeeded.")

    def toggle_notify(self) -> None:
        ch, client = self._selected_char(), self.client
        if ch is None or client is None:
            return
        if ch.handle in self.notifying:
            self.log(f"Stopping notifications on {self._char_label(ch)}...")
            self.run_async(client.stop_notify(ch),
                           lambda f: self._on_notify_changed(f, client, ch, False))
        elif {"notify", "indicate"} & set(ch.properties):
            self.log(f"Starting notifications on {self._char_label(ch)}...")
            self.run_async(client.start_notify(ch, self._make_notify_handler(client, ch)),
                           lambda f: self._on_notify_changed(f, client, ch, True))

    def _make_notify_handler(self, client, ch):
        def handler(_sender, data) -> None:  # asyncio thread
            payload = bytes(data)
            self.ui_queue.put(lambda: self._on_notification(client, ch, payload))
        return handler

    def _on_notify_changed(self, fut, client, ch, enabled: bool) -> None:
        if client is not self.client:
            return
        err = self._error_text(fut)
        if err:
            self.log(f"Could not {'start' if enabled else 'stop'} notifications on "
                     f"{self._char_label(ch)}: {err}")
            return
        if enabled:
            self.notifying.add(ch.handle)
        else:
            self.notifying.discard(ch.handle)
        iid = f"c{ch.handle}"
        if self.char_tree.exists(iid):
            self.char_tree.set(iid, "notify", "on" if enabled else "")
        self.log(f"Notifications {'on' if enabled else 'off'} for {self._char_label(ch)}.")
        self._update_controls()

    def _on_notification(self, client, ch, data: bytes) -> None:
        if client is not self.client or ch.handle not in self.notifying:
            return
        self._show_value(ch, data)
        self.log(f"Notification from {self._char_label(ch)}: "
                 f"{data.hex(' ').upper() or '(empty)'}")

    # ---------------------------------------------------------- shutdown --

    async def _shutdown(self, client) -> None:
        task = self._connect_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                pass
        await self._stop_scan()

    def _on_close(self) -> None:
        self.scan_wanted = False
        self.cancel_requested = True
        try:
            self.worker.submit(self._shutdown(self.client)).result(timeout=12)
        except Exception:
            pass
        self.worker.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    BLEExplorer(root)
    root.mainloop()


if __name__ == "__main__":
    main()