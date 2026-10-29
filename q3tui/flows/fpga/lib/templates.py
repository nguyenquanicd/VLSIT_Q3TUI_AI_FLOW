"""FPGA families: what `fpga_synth` runs for a part — the tool, how its script is written, the form the part takes, how the numbers are read, a
default part. Internal: the part you pick (`fpga_synth.part`, the project flow's FPGA view) decides the family (`resolve`, by the part's name:
`xc7…` Xilinx 7-series, `LFE5U…` / `85k` ECP5, `10AX…` Arria 10); a `template` chosen in an older project is used only when no part is set.

Three backends: `vivado` (Xilinx, Vivado batch: `lib/vivado.py`), `yosys` (Yosys synthesis + nextpnr place and route, no licence: `lib/oss.py`)
and `quartus` (Intel, Quartus Prime Pro or Standard: `lib/quartus.py`). Each runs its own tool roles (`Template.roles`)."""

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
    match: str = ""  # regex on the lower-cased part name: the parts of this family
    quartus_family: str = ""  # quartus: the FAMILY assignment
    edition: str = ""  # quartus: "pro" | "std" (the Quartus Prime edition that has the family)

    @property
    def label(self) -> str:
        return f"{self.title} ({self.node_nm} nm)"

    @property
    def roles(self) -> list[str]:
        """The tool roles this template runs (config/tools.json): Vivado `fpga_synth`; Yosys `fpga_yosys` + nextpnr `fpga_pnr_<family>`."""
        if self.backend == "quartus":
            return [f"fpga_quartus_{self.edition}"]
        return ["fpga_synth"] if self.backend == "vivado" else ["fpga_yosys", *([self.pnr_role] if self.pnr_role else [])]

    @property
    def tool(self) -> str:
        return {"vivado": "Vivado", "yosys": "Yosys + nextpnr", "quartus": f"Quartus Prime {'Pro' if self.edition == 'pro' else 'Standard'}"}[self.backend]


TEMPLATES: dict[str, Template] = {t.id: t for t in (
    Template("xilinx7", "Xilinx 7-series — Vivado", "Xilinx", "7-series", 28, "vivado", "xc7k325tffg900-2",
             help="Kintex / Artix / Virtex / Spartan-7 in Vivado batch mode; `part` is a Vivado part name (xc7a100tcsg324-1, …).", match=r"^x[caq]7[aksvz]"),
    Template("ecp5", "Lattice ECP5 — Yosys + nextpnr", "Lattice", "ECP5", 40, "yosys", "85k", synth="synth_ecp5", pnr_role="fpga_pnr_ecp5",
             devices={"25k": "CABGA381", "45k": "CABGA381", "85k": "CABGA381", "um-25k": "CABGA381", "um-45k": "CABGA381", "um-85k": "CABGA381",
                      "um5g-25k": "CABGA381", "um5g-45k": "CABGA381", "um5g-85k": "CABGA381"},
             speed="6", resources={"lut": "TRELLIS_COMB", "ff": "TRELLIS_FF", "bram_tile": "DP16KD", "dsp": "MULT18X18D", "io": "TRELLIS_IO"},
             help="Open-source flow, no licence; `part` is the device (25k, 45k, 85k, um-85k, …), `package` and `speed` (6-8) are options.",
             match=r"^lfe5u|^(um5g-|um-)?(25|45|85)k$"),
    Template("ice40", "Lattice iCE40 — Yosys + nextpnr", "Lattice", "iCE40", 40, "yosys", "hx8k", synth="synth_ice40", pnr_role="fpga_pnr_ice40",
             devices={"hx1k": "tq144", "hx4k": "tq144", "hx8k": "ct256", "lp1k": "qn84", "lp4k": "cm81", "lp8k": "cm81", "up3k": "uwg30", "up5k": "sg48"},
             resources={"lut": "ICESTORM_LC", "bram_tile": "ICESTORM_RAM", "dsp": "ICESTORM_DSP", "io": "SB_IO"},
             help="Small low-power devices: a design that does not fit stops the step. `part` is hx8k, up5k, lp8k, …", match=r"^ice40|^(hx|lp|up)\d+k$"),
    # Intel: Quartus Prime Pro for the newer families, Standard for the older ones (each edition has its own tool role)
    Template("arria10", "Intel Arria 10 — Quartus Prime Pro", "Intel", "Arria 10", 20, "quartus", "10AX115N2F40E2LG", match=r"^10a[xs]",
             quartus_family="Arria 10", edition="pro", help="`part` is a Quartus device name (10AX115N2F40E2LG, …)."),
    Template("stratix10", "Intel Stratix 10 — Quartus Prime Pro", "Intel", "Stratix 10", 14, "quartus", "1SG280HU2F50E2VG", match=r"^1s[gxm]",
             quartus_family="Stratix 10", edition="pro"),
    Template("agilex7", "Intel Agilex 7 — Quartus Prime Pro", "Intel", "Agilex 7", 10, "quartus", "AGFB014R24B2E2V", match=r"^ag[fi]",
             quartus_family="Agilex 7", edition="pro"),
    Template("cyclone10gx", "Intel Cyclone 10 GX — Quartus Prime Pro", "Intel", "Cyclone 10 GX", 20, "quartus", "10CX220YF780E5G", match=r"^10cx",
             quartus_family="Cyclone 10 GX", edition="pro"),
    Template("cyclonev", "Intel Cyclone V — Quartus Prime Standard", "Intel", "Cyclone V", 28, "quartus", "5CEBA4F23C7", match=r"^5c[egs]",
             quartus_family="Cyclone V", edition="std"),
    Template("max10", "Intel MAX 10 — Quartus Prime Standard", "Intel", "MAX 10", 55, "quartus", "10M50DAF484C7G", match=r"^10m\d",
             quartus_family="MAX 10", edition="std"),
    Template("cyclone10lp", "Intel Cyclone 10 LP / IV — Quartus Prime Standard", "Intel", "Cyclone 10 LP", 60, "quartus", "10CL025YU256C8G",
             match=r"^10cl|^ep4ce", quartus_family="Cyclone 10 LP", edition="std"),
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


def for_part(part: str) -> Template | None:
    """The family of a part, by its name (None: no family here synthesizes it)."""
    import re

    low = str(part or "").strip().lower()
    return next((t for t in TEMPLATES.values() if t.match and re.search(t.match, low)), None) if low else None


def resolve(options: dict) -> tuple[Template | None, str, str]:
    """(family, device, note) of a step's options: the part decides the family; without a part, a template chosen before (older projects), else
    the default family's part, said in the note. A part no family here synthesizes: (None, part, why)."""
    part = str(options.get("part") or (options.get("fpga") or {}).get("part") or "").strip()
    if part:
        t = for_part(part)
        old = TEMPLATES.get(str(options.get("template") or ""))
        if t is None and old is not None and not device(old, part)[1]:  # (a device an older project's template names: 'hx8k' under ice40)
            t = old
        if t is None:
            return None, part, (f"part '{part}': no FPGA family here synthesizes it (Xilinx 7-series with Vivado; Lattice ECP5 / iCE40 with Yosys + "
                                f"nextpnr; Intel {', '.join(x.family for x in TEMPLATES.values() if x.vendor == 'Intel')} with Quartus)")
        dev, note = device(t, part)
        return t, dev, note
    t = get(options.get("template"))
    return t, t.part, ("" if options.get("template") else f"no FPGA part chosen: using {t.part} ({t.title}) — pick the part (the project flow's FPGA view)")


def device(t: Template, part: str | None) -> tuple[str, str]:
    """(device, note): the part asked for when it belongs to the template, else its default and why. A part of another family left in
    the settings (a Vivado part after switching to ECP5) is not an error: the template's default is used and the log says so."""
    part = str(part or "").strip()
    if not part:
        return t.part, ""
    import re

    if t.backend == "yosys":
        from q3tui.flows.fpga.lib import devices  # (LFE5UM5G-85F → um5g-85k, iCE40HX8K-CT256 → hx8k: the form nextpnr takes)

        part = devices.identify(part)["device"] if for_part(part) is t else part
    ok = part.lower() in t.devices if t.backend == "yosys" else bool(t.match and re.search(t.match, part.lower()))
    if ok:
        return part.lower() if t.backend == "yosys" else part, ""
    return t.part, f"part '{part}' is not a {t.family} device: using {t.part}"
