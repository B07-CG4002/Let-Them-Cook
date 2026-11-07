"""Guided extra PICKUP recordings without hand lifting.

The gesture class remains PICKUP.  The selected thumb variant is stored only
in the recording_variant metadata so these samples can be merged into the
existing PICKUP class later.

Run from the repository root:
    python -m tools.pickup_static_gui --person P02 --date 20261105
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path


STATIC_VARIANTS = {
    "thumb straight": "PICKUP; stationary no-lift closure; thumb straight",
    "thumb bent": "PICKUP; stationary no-lift closure; thumb bent",
}


def variant_note(choice: str) -> str:
    try:
        return STATIC_VARIANTS[choice]
    except KeyError as exc:
        raise ValueError(f"Unknown thumb variant: {choice}") from exc


def launch(args):
    import tkinter as tk
    from tkinter import ttk, messagebox

    from src.hand_ai_flex3.wide_record_gui import WideApp
    from src.hand_ai_flex3.record_both_gui import pair_settings, validate_session_settings

    class StaticPickupApp(WideApp):
        def __init__(self, root):
            super().__init__(root, gyro_dps=args.gyro_dps)
            root.title(f"PICKUP extra recorder — v5 ±{args.gyro_dps} dps")
            root.geometry("1120x900")

            self.vars["mode"].set(args.mode)
            self.vars["person"].set(args.person)
            self.vars["session"].set(args.session)
            self.vars["split"].set(args.split)
            self.vars["left"].set("PICKUP")
            self.vars["right"].set("PICKUP")
            self.vars["count"].set(str(args.count))
            self.vars["output"].set(args.output)

            form = self.field_widgets["output"].master
            row = form.grid_size()[1]
            ttk.Label(form, text="Thumb variant").grid(row=row, column=0, padx=8, pady=3)
            self.variant = tk.StringVar(value=args.thumb_variant)
            self.variant_widget = ttk.Combobox(
                form, textvariable=self.variant,
                values=tuple(STATIC_VARIANTS), state="readonly", width=55,
            )
            self.variant_widget.grid(row=row, column=1, pady=3, sticky="ew")
            self.inputs.append((self.variant_widget, tuple(STATIC_VARIANTS)))

            self.instruction = tk.StringVar(
                value="PICKUP: open hand still → close hand without lifting → hold closed still"
            )
            ttk.Label(
                form, textvariable=self.instruction, foreground="#8b0000"
            ).grid(row=row + 1, column=0, columnspan=2, pady=5)
            ttk.Label(
                form,
                text="The saved model class is PICKUP. Thumb variant is metadata only.",
            ).grid(row=row + 2, column=0, columnspan=2, pady=2)

            # This mode always records PICKUP on both selected hands.
            self.field_widgets["left"].configure(state="disabled")
            self.field_widgets["right"].configure(state="disabled")
            self.controls()

        def controls(self):
            super().controls()
            if hasattr(self, "variant_widget"):
                self.variant_widget.configure(state="disabled" if self.busy else "readonly")
                self.field_widgets["left"].configure(state="disabled")
                self.field_widgets["right"].configure(state="disabled")

        def start(self):
            if not self.worker or not self.connected:
                return
            try:
                v = {key: value.get() for key, value in self.vars.items()}
                note = variant_note(self.variant.get())
                config = pair_settings(
                    v["person"], v["session"], v["split"],
                    "PICKUP", "PICKUP", v["count"], v["output"],
                )
                config = self.worker.prepare_config(
                    {device: config[device] for device in self.worker.devices}
                )
                for settings in config.values():
                    settings["recording_variant"] = note
                validate_session_settings(config)
                self.worker.cancel_recording.clear()
                self.busy = True
                self.worker.commands.put(dict(settings=config))
                self.phase.set(f"READY — {note}")
                self.controls()
            except (ValueError, OSError) as error:
                messagebox.showerror("Recording settings", str(error))

    root = tk.Tk()
    StaticPickupApp(root)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", required=True)
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--session")
    parser.add_argument("--split", choices=("train", "validation", "test"), default="train")
    parser.add_argument("--mode", choices=("LEFT only", "RIGHT only", "BOTH"), default="BOTH")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--output", default="data/hand_raw_flex3/pickup_static")
    parser.add_argument("--thumb-variant", choices=tuple(STATIC_VARIANTS), default="thumb straight")
    parser.add_argument("--gyro-dps", type=int, choices=(1000, 2000), default=2000)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    if args.session is None:
        args.session = f"{args.person}_pickup_static_{args.date}"
    launch(args)


if __name__ == "__main__":
    main()
