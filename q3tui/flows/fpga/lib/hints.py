"""What the other flows learned about the constraints, shown where you set them: the SDC tab and the sdc step tell the clock the ASIC flow's
last synthesis could reach (`schemas/asic_synth.json`: Fmax per clock for the library), so a frequency typed here comes with the answer
"the ASIC library reaches about N MHz". A hint is read from a file the other flow wrote: nothing here runs a tool, and no hint is
shown when that flow has not run."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, sha256_json


LIBRARY_OPTION = "pipeline.step_options.asic_synth.library"  # the ASIC flow's library: one choice for both flows, made from either


def asic_default_library() -> str:
    """The library the ASIC flow's flow file names (its `asic_synth` options), what is used while q3tui.yaml sets none."""
    from q3tui.flows import _front

    f = Path(__file__).resolve().parents[2] / "asic" / "asic_synth" / "md" / "skill.md"
    lib = (_front(f.read_text())[0].get("options") or {}).get("library") if f.is_file() else None
    return str(lib or "")


def asic_library(project) -> str:
    """The standard-cell library (.db) the ASIC flow, and so the estimate, uses: q3tui.yaml's `asic_synth.library`, else the flow file's."""
    mine = (project.cfg.pipeline.step_options.get("asic_synth") or {}).get("library")
    lib = mine if isinstance(mine, str) else (mine[0] if mine else None)
    return str(Path(str(lib or asic_default_library())).expanduser()) if (lib or asic_default_library()) else ""


def rtl_sha(project) -> str:
    """A fingerprint of the RTL files (src/rtl/*.sv, *.v): what an ASIC run was made from."""
    d = project.rtl_dir
    files = sorted(d.glob("*.sv")) + sorted(d.glob("*.v")) if d.is_dir() else []
    return sha256_json({f.name: f.read_text(errors="replace") for f in files})[:12]


def _lib_name(project, path: str) -> str:
    """"GF180MCU 7-track 5 V · tt_025C_5v00" for a catalogued library (the ASIC flow's pdks.json), else the file's last part."""
    try:
        from q3tui.flows.asic.lib import pdks

        return pdks.short_name(path, project)
    except Exception:  # noqa: BLE001 - a hint never breaks a view
        return Path(path).stem.split("__")[-1]


def _from(project, name: str, source: str) -> tuple[dict[str, dict], bool]:
    d = read_json(project.schemas_dir / name, {}) or {}
    groups = (d.get("timing") or {}).get("groups") or []
    lib = ", ".join(_lib_name(project, x) for x in d.get("library") or [])
    stale = bool(d.get("rtl_sha")) and d.get("rtl_sha") != rtl_sha(project)
    return ({g["group"]: {"fmax_mhz": g.get("fmax_mhz"), "target_mhz": g.get("target_mhz"), "bound": g.get("bound", "estimate"), "library": lib, "stale": stale,
                          "source": source} for g in groups if g.get("group")}, stale)


def asic_hint(project) -> dict[str, dict]:
    """{clock name: {fmax_mhz, target_mhz, bound, library, stale, source}} for the clocks the ASIC flow knows (empty: nothing yet). `source` is
    "synthesis" (`asic_synth`, exact) or "estimate" (`asic_estimate`, quick); a fresh synthesis wins, then a fresh estimate, then whatever is
    left (`stale`: the RTL changed since)."""
    full, full_stale = _from(project, "asic_synth.json", "synthesis")
    quick, quick_stale = _from(project, "asic_estimate.json", "estimate")
    for pick in ([full] if full and not full_stale else []) + ([quick] if quick and not quick_stale else []) + [full, quick]:
        if pick:
            return pick
    return {}


def asic_note(project, clock: str) -> str:
    """The note for a clock: "ASIC ≈ 254 MHz (tt_025C_5v00)", with "≥" when the ASIC met the clock it was given (it can go faster); "" without a hint."""
    h = asic_hint(project).get(clock)
    if not h or not h.get("fmax_mhz"):
        return ""
    tag = ", ".join(x for x in ("quick estimate" if h["source"] == "estimate" else "", h["library"]) if x)
    return f"ASIC {'≥' if h['bound'] == 'at_least' else '≈'} {h['fmax_mhz']:g} MHz" + (f" ({tag})" if tag else "") + (" — RTL changed since" if h["stale"] else "")
