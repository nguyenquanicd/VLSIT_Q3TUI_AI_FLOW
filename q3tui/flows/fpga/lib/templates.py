"""FPGA templates: what `fpga_synth` runs for a family of parts — the tool, the default device, how the numbers are read.

A template is chosen with `options.template` of `fpga_synth` (Settings → fpga_synth → FPGA template); `options.part` overrides its
default device. Two backends: `vivado` (Xilinx, Vivado batch: `lib/vivado.py`) and `yosys` (Yosys synthesis + nextpnr place and route,
no licence: `lib/oss.py`)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    vendor: str
    family: str
    node_nm: int
    backend: str  # "vivado" | "yosys"
    part: str  # the default device (vivado: the part name; yosys: the nextpnr device flag, e.g. "85k")
    synth: str = ""  # yosys: the synthesis command (synth_ecp5)
    pnr_role: str = ""  # yosys: the tool role of nextpnr for this family
    devices: dict[str, str] = field(default_factory=dict)  # yosys: device -> default package
    speed: str = ""  # yosys: the default speed grade
    resources: dict[str, str] = field(default_factory=dict)  # yosys: our resource key -> nextpnr's name in "Device utilisation"
    help: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} ({self.node_nm} nm)"


TEMPLATES: dict[str, Template] = {t.id: t for t in (
    Template("xilinx7", "Xilinx 7-series — Vivado", "Xilinx", "7-series", 28, "vivado", "xc7k325tffg900-2",
             help="Kintex / Artix / Virtex / Spartan-7 in Vivado batch mode; `part` is a Vivado part name (xc7a100tcsg324-1, …)."),
    Template("ecp5", "Lattice ECP5 — Yosys + nextpnr", "Lattice", "ECP5", 40, "yosys", "85k", synth="synth_ecp5", pnr_role="fpga_pnr_ecp5",
             devices={"25k": "CABGA381", "45k": "CABGA381", "85k": "CABGA381", "um-25k": "CABGA381", "um-45k": "CABGA381", "um-85k": "CABGA381",
                      "um5g-25k": "CABGA381", "um5g-45k": "CABGA381", "um5g-85k": "CABGA381"},
             speed="6", resources={"lut": "TRELLIS_COMB", "ff": "TRELLIS_FF", "bram_tile": "DP16KD", "dsp": "MULT18X18D", "io": "TRELLIS_IO"},
             help="Open-source flow, no licence; `part` is the device (25k, 45k, 85k, um-85k, …), `package` and `speed` (6-8) are options."),
    Template("ice40", "Lattice iCE40 — Yosys + nextpnr", "Lattice", "iCE40", 40, "yosys", "hx8k", synth="synth_ice40", pnr_role="fpga_pnr_ice40",
             devices={"hx1k": "tq144", "hx4k": "tq144", "hx8k": "ct256", "lp1k": "qn84", "lp4k": "cm81", "lp8k": "cm81", "up3k": "uwg30", "up5k": "sg48"},
             resources={"lut": "ICESTORM_LC", "bram_tile": "ICESTORM_RAM", "dsp": "ICESTORM_DSP", "io": "SB_IO"},
             help="Small low-power devices: a design that does not fit stops the step. `part` is hx8k, up5k, lp8k, …"),
)}
DEFAULT = "xilinx7"


def get(template_id: str | None) -> Template:
    t = TEMPLATES.get(str(template_id or DEFAULT))
    if t is None:
        raise ValueError(f"unknown FPGA template '{template_id}' (templates: {', '.join(TEMPLATES)})")
    return t


def choices() -> tuple[tuple[str, str], ...]:
    """(id, label) for the Settings dialog."""
    return tuple((t.id, t.label) for t in TEMPLATES.values())


def device(t: Template, part: str | None) -> tuple[str, str]:
    """(device, note): the part asked for when it belongs to the template, else its default and why. A part of another family left in
    the settings (a Vivado part after switching to ECP5) is not an error: the template's default is used and the log says so."""
    part = str(part or "").strip()
    if not part:
        return t.part, ""
    ok = part.lower().startswith(("xc", "xa", "xq")) if t.backend == "vivado" else part.lower() in t.devices
    if ok:
        return part if t.backend == "vivado" else part.lower(), ""
    return t.part, f"part '{part}' is not a {t.family} device: using {t.part}"
