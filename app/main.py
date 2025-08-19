import os
import subprocess
import uuid
from itertools import product
from pathlib import Path
import re
from io import StringIO
from ase.io import read, write
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse,PlainTextResponse, FileResponse
from jinja2 import Template
from fastapi.middleware.cors import CORSMiddleware

API = FastAPI(title="The Emperor")
API.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or a list of your front-end origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOT = Path.cwd()
DATA = ROOT/"data"/"jobs"
DATA.mkdir(parents=True,exist_ok=True)

DFTB_PREFIX = Path(os.getenv("DFTB_PREFIX",str(ROOT/"parameters")))

# ---- TEMPLATES ----
HSD = Template(r"""
Geometry = GenFormat {
{{ genblock }}
}

Driver = GeometryOptimization {
  Optimizer = Rational {}
  MovedAtoms = 1:-1
  MaxSteps = 100
  OutputPrefix = "geom.out"
  Convergence {GradElem = 1E-4}
}

Hamiltonian = DFTB {
  Scc = Yes
  SlaterKosterFiles {
{{ slakos }}
  }
  MaxAngularMomentum {
{% for el, l in maxl.items() -%}
    {{ el }} = "{{ l }}"
{% endfor -%}
  }
}

Options {}

Analysis {
  CalculateForces = Yes
}

ParserOptions {
  ParserVersion = 12
}
""".strip())

FENCE_RE = re.compile(r"^```.*?\n|\n```$", re.S)

def _strip_fences(txt: str) -> str:
    return FENCE_RE.sub("", txt).strip()

def _extract_inner_genformat(txt: str) -> str | None:
    lines = txt.strip().splitlines()
    if not lines: return None
    if re.match(r"^\s*Geometry\s*=\s*GenFormat\s*\{\s*$", lines[0], re.I):
        try:
            end = next(i for i, ln in enumerate(lines) if ln.strip() == "}")
        except StopIteration:
            raise HTTPException(400, "GenFormat: missing closing '}'.")
        return "\n".join(lines[1:end]).strip()
    return None

def _looks_like_poscar(txt: str) -> bool:
    L = [ln.strip() for ln in txt.splitlines()]
    if len(L) < 8: return False
    # lines 3-5 must be vectors
    try:
        for i in (2,3,4):
            a,b,c = map(float, L[i].split()[:3])
    except Exception:
        return False
    # line 6 likely symbols (not all ints), line 7 counts (all ints)
    sym = L[5].split(); cnt = L[6].split()
    sym_ok = sym and not all(t.isdigit() for t in sym)
    cnt_ok = cnt and all(t.isdigit() for t in cnt)
    return sym_ok and cnt_ok

def _is_int_tokens(tokens):
    try:
        _ = [int(t) for t in tokens]
        return True
    except Exception:
        return False

def validate_poscar_text(txt: str) -> dict:
    """
    Validate a VASP5 POSCAR (with symbols line) and return parsed metadata.
    Raises HTTPException(400) on clear, user-facing errors.
    """
    lines_raw = txt.splitlines()
    lines = [ln.rstrip() for ln in lines_raw if ln.strip() != ""]
    if len(lines) < 8:
        raise HTTPException(400, "POSCAR too short: expected at least 8 non-empty lines.")

    # 0: comment
    comment = lines[0]

    # 1: scale
    try:
        float(lines[1].split()[0])
    except Exception:
        raise HTTPException(400, "Line 2 (scaling factor) must be a number.")

    # 2–4: lattice vectors (3 floats each)
    for i in range(2, 5):
        parts = lines[i].split()
        if len(parts) < 3:
            raise HTTPException(400, f"Line {i+1} must have 3 lattice vector components.")
        try:
            _ = [float(x) for x in parts[:3]]
        except Exception:
            raise HTTPException(400, f"Line {i+1} contains non-numeric lattice components.")

    # 5: symbols (non-numeric tokens expected)
    sym_tokens = lines[5].split()
    if not sym_tokens or _is_int_tokens(sym_tokens):
        raise HTTPException(400, "Line 6 must list element symbols (e.g., 'C H'). This file looks like VASP4; please provide VASP5 with symbols.")

    # 6: counts (ints; length must match symbols)
    count_tokens = lines[6].split()
    if not count_tokens or not _is_int_tokens(count_tokens):
        raise HTTPException(400, "Line 7 must contain integer counts (e.g., '1 4').")
    if len(count_tokens) != len(sym_tokens):
        raise HTTPException(400, f"Counts length mismatch: found {len(count_tokens)} numbers for {len(sym_tokens)} symbols ({' '.join(sym_tokens)}).")

    counts = [int(t) for t in count_tokens]
    natoms = sum(counts)

    # Optional: "Selective dynamics"
    idx = 7
    has_sel = False
    if idx < len(lines) and lines[idx].lower().startswith("selective"):
        has_sel = True
        idx += 1

    # Coord mode line
    if idx >= len(lines):
        raise HTTPException(400, "Missing coordinate mode line (expected 'Direct' or 'Cartesian').")
    mode_line = lines[idx].strip().lower()
    if not (mode_line.startswith("direct") or mode_line.startswith("cart")):
        raise HTTPException(400, "Coordinate mode must be 'Direct' or 'Cartesian' on the line after counts (or after 'Selective dynamics').")
    idx += 1

    # Now we expect 'natoms' coordinate lines
    if idx + natoms > len(lines):
        have = max(0, len(lines) - idx)
        raise HTTPException(400, f"Not enough coordinate lines: expected {natoms}, found {have}.")

    # Basic numeric check for each coordinate line
    for k in range(natoms):
        parts = lines[idx + k].split()
        if len(parts) < 3:
            raise HTTPException(400, f"Coordinate line {k+1} under the mode must have at least 3 numbers.")
        try:
            _ = [float(parts[0]), float(parts[1]), float(parts[2])]
        except Exception:
            raise HTTPException(400, f"Coordinate line {k+1} contains non-numeric values.")

    return {
        "comment": comment,
        "symbols": sym_tokens,
        "counts": counts,
        "natoms": natoms,
        "mode": "direct" if "direct" in mode_line else "cartesian",
        "has_selective_dynamics": has_sel,
    }

def available_param_sets(prefix: Path = DFTB_PREFIX) -> list[str]:
    return sorted([p.name for p in prefix.iterdir() if p.is_dir()]) if prefix.exists() else []

def ensure_param_set_exists(param_set:str,prefix:Path = DFTB_PREFIX)-> None:
    p = prefix / param_set
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(
            f"Parameter set {param_set} not found under {prefix}."
            f"Available : {','.join(available_param_sets(prefix)) or '(none)'}"
        )
def genformat_block(symbols, positions):
    """Build the inner lines for `Geometry = GenFormat { ... }` (Cartesian, 1-based indices)."""
    species = []
    for s in symbols:
        if s not in species:
            species.append(s)
    lines = []
    lines.append(f"{len(symbols)} C")
    lines.append("  " + " ".join(species))
    lines.append("")
    for i, (sym, (x, y, z)) in enumerate(zip(symbols, positions), start=1):
        sid = species.index(sym) + 1
        lines.append(f"  {i:d} {sid:d}  {x: .11E} {y: .11E} {z: .11E}")
    return "\n".join(lines), species

def slako_block(species: list[str], param_set: str) -> str:
    """
    Produce explicit lines like:
      O-O = "parameters/mio-1-1/O-O.skf"
      O-H = "parameters/mio-1-1/O-H.skf"
      H-O = "parameters/mio-1-1/H-O.skf"
      H-H = "parameters/mio-1-1/H-H.skf"
    for every ordered pair in species × species.
    """
    base = f'parameters/{param_set}'
    lines = []
    for a, b in product(species, species):
        lines.append(f'    {a}-{b} = "{base}/{a}-{b}.skf"')
    return "\n".join(lines)

def guess_maxl(symbols: list[str], param_set: str) -> dict[str, str]:
    """
    Return MaxAngularMomentum dict depending on the chosen SK set.
    - 3ob-3-1: H=s; C/N/O=p; S/P/Cl/Si=d (common practice)
    - mio-1-1: H=s; most main-group = p (no d in mio)
    - matsci: include d for common transition metals (extend as needed)
    - default: H=s; others=p (safe baseline)
    """
    uniq = sorted(set(symbols))

    if param_set.startswith("3ob"):
        mapping = {
            "H": "s",
            "C": "p", "N": "p", "O": "p", "F": "p",
            "P": "d", "S": "d", "Cl": "d", "Si": "d"
        }
    elif param_set.startswith("mio"):
        mapping = {
            "H": "s",
            "C": "p", "N": "p", "O": "p", "F": "p",
            "P": "p", "S": "p", "Cl": "p", "Si": "p",
            "B": "p", "Al": "p", "Na": "p", "Mg": "p"
        }
    elif param_set.startswith("matsci"):
        mapping = {
            "H": "s",
            "C": "p", "N": "p", "O": "p", "F": "p",
            # common transition metals (extend for your use case):
            "Fe": "d", "Co": "d", "Ni": "d", "Cu": "d", "Zn": "d",
            "Ti": "d", "V": "d", "Cr": "d", "Mn": "d",
            "Mo": "d", "W": "d", "Pd": "d", "Pt": "d"
        }
    else:
        # Safe default
        mapping = {"H": "s"}

    return {el: mapping.get(el, "p") for el in uniq}

def _parse_genformat_inner(inner: str) -> tuple[int,list[str],list[tuple[int,int,float,float,float]]]:
    # tolerate leading title/lattice junk: keep only non-empty lines that “look like” GenFormat
    lines = [ln for ln in (ln.strip() for ln in inner.splitlines()) if ln]
    # find first header line that looks like "<int> <C|S>"
    start = None
    for i, ln in enumerate(lines):
        p = ln.split()
        if len(p)>=2 and p[0].isdigit() and p[1].upper() in ("C","S"):
            start = i; break
    if start is None:
        raise HTTPException(400, "Not a valid GenFormat header (expected '<N> C').")
    lines = lines[start:]
    head = lines[0].split()
    try:
        n = int(head[0])
    except Exception:
        raise HTTPException(400, "GenFormat: N (atom count) must be integer.")
    if head[1].upper() not in ("C","S"):
        raise HTTPException(400, "GenFormat: second token must be 'C' or 'S'.")
    if len(lines) < 2:
        raise HTTPException(400, "GenFormat: missing species line.")
    species = lines[1].split()
    if not species or all(t.isdigit() for t in species):
        raise HTTPException(400, "GenFormat: species line must list symbols (e.g., 'C H').")

    coords = []
    seen = set()
    for k in range(2, min(2+n, len(lines))):
        parts = lines[k].split()
        if len(parts) < 5:
            raise HTTPException(400, f"GenFormat: coord line #{k-1} must be 'i sid x y z'.")
        try:
            idx = int(parts[0]); sid = int(parts[1])
            x,y,z = map(float, parts[2:5])
        except Exception:
            raise HTTPException(400, f"GenFormat: non-numeric value on coord line #{k-1}.")
        if idx in seen:
            raise HTTPException(400, f"GenFormat: duplicated atom index {idx}.")
        if not (1 <= sid <= len(species)):
            raise HTTPException(400, f"GenFormat: species id {sid} out of 1..{len(species)}.")
        seen.add(idx)
        coords.append((idx, sid, x, y, z))

    if len(coords) != n:
        raise HTTPException(400, f"GenFormat: header says N={n} but found {len(coords)} coordinate lines.")

    # reorder by index (1..N)
    coords.sort(key=lambda t: t[0])
    # ensure indices are contiguous
    if [t[0] for t in coords] != list(range(1, n+1)):
        raise HTTPException(400, "GenFormat: atom indices must be 1..N without gaps.")

    return n, species, coords
def _format_genformat(n: int, species: list[str], coords) -> str:
    # canonical, scientific notation, fixed width
    out = []
    out.append(f"{n:d} C")
    out.append("  " + " ".join(species))
    out.append("")  # blank line
    for (idx, sid, x, y, z) in coords:
        out.append(f"  {idx:1d} {sid:1d}  {x: .11E} {y: .11E} {z: .11E}")
    return "\n".join(out)

def sanitize_geometry_to_genformat(text: str) -> dict:
    """
    Normalize arbitrary paste (fenced, HSD-wrapped GenFormat, POSCAR, messy GenFormat)
    into canonical GenFormat inner block. Returns dict with:
      kind: 'genformat'|'poscar'
      genblock: canonical GenFormat inner block
      species_order: [...]
      symbols_per_atom: [...]
    """
    raw = _strip_fences(text)

    # Case A: Full HSD with Geometry=GenFormat { ... }
    inner = _extract_inner_genformat(raw)
    if inner is not None:
        n, species, coords = _parse_genformat_inner(inner)
        syms = [species[sid-1] for (_,sid,_,_,_) in coords]
        return {
            "kind": "genformat",
            "genblock": _format_genformat(n, species, coords),
            "species_order": species,
            "symbols_per_atom": syms,
        }

    # Case B: Looks like POSCAR → convert via ASE
    if _looks_like_poscar(raw):
        try:
            atoms = read(StringIO(raw), format="vasp")
        except Exception as e:
            raise HTTPException(400, f"POSCAR detected but ASE failed to parse: {e}")
        symbols = atoms.get_chemical_symbols()
        positions = atoms.get_positions()
        genblock, species = genformat_block(symbols, positions)  # your existing helper
        return {
            "kind": "poscar",
            "genblock": genblock,
            "species_order": species,
            "symbols_per_atom": symbols,
        }

    # Case C: Likely bare/messy GenFormat inner → parse/repair → format
    n, species, coords = _parse_genformat_inner(raw)
    syms = [species[sid-1] for (_,sid,_,_,_) in coords]
    return {
        "kind": "genformat",
        "genblock": _format_genformat(n, species, coords),
        "species_order": species,
        "symbols_per_atom": syms,
    }

def parse_genformat(genblock: str) -> tuple[list[str], list[str]]:
    """
    Parse a GenFormat block to recover (symbols list per-atom order, species order).
    Assumes lines like:
      N C
      <blank>
      i sid x y z
    """
    raw = [ln.strip() for ln in genblock.strip().splitlines()]
    # Remove empty lines except between species and coords it’s ok
    lines = [ln for ln in raw if ln]

    if len(lines) < 4:
        raise ValueError("GenFormat too short.")

    # lines[0] like "N C" (count + 'C' keyword); lines[1] species
    species_line = lines[1]
    species = species_line.split()
    symbols: list[str] = []

    # remaining lines are coordinates, each like: i sid x y z
    for ln in lines[2:]:
        parts = ln.split()
        if len(parts) < 5:
            continue
        try:
            sid = int(parts[1])
        except ValueError:
            continue
        if sid < 1 or sid > len(species):
            raise ValueError(f"Species id {sid} out of range.")
        symbols.append(species[sid - 1])

    if not symbols:
        # fallback: at least return unique species
        symbols = species[:]
    return symbols, species

@API.get("/health")
def health():
    return {
        "status": "ok",
        "DFTB_PREFIX": str(DFTB_PREFIX),
        "param_sets": available_param_sets()
    }

@API.get("/param-sets")
def list_param_sets():
    return {"param_sets": available_param_sets()}

@API.get("/file/{job_id}/{filename}")
def get_job_file(job_id: str, filename: str):
    work = DATA / job_id
    if not work.exists():
        raise HTTPException(404, "job not found")
    path = work / filename
    if not path.exists():
        raise HTTPException(404, "file not found")
    # text-ish files streamed as text, others as download
    TEXT_EXT = {".log", ".out", ".txt", ".hsd"}
    return PlainTextResponse(path.read_text()) if path.suffix in TEXT_EXT else FileResponse(path)

@API.post("/prepare-poscar")
async def prepare_poscar(
    file: UploadFile = File(...),
    param_set: str = Form("mio-1-1")
):
    # Validate param set
    try:
        ensure_param_set_exists(param_set)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

        # Read file *once*, validate POSCAR format clearly, then let ASE parse
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        meta = validate_poscar_text(text)
    except HTTPException as e:
        # forward the friendly 400 to client
        raise e

    job_id = str(uuid.uuid4())
    work = DATA / job_id
    work.mkdir(parents=True, exist_ok=True)

    # Save POSCAR and read with ASE
    poscar = work / "POSCAR"
    poscar.write_text(text)

    atoms = read(str(poscar))
    symbols = atoms.get_chemical_symbols()
    positions = atoms.get_positions()

    # Build GenFormat + species list
    genblock, species = genformat_block(symbols, positions)

    # Build SlaterKosterFiles lines for the present species (relative to ./parameters)
    slakos_lines = slako_block(species, param_set)

    # Adaptive MaxAngularMomentum
    maxl = guess_maxl(symbols, param_set)

    # Render HSD
    hsd_text = HSD.render(genblock=genblock, slakos=slakos_lines, maxl=maxl)
    (work / "dftb_in.hsd").write_text(hsd_text)

    # Ensure relative path works: create ./parameters -> $DFTB_PREFIX symlink
    dst = work / "parameters"
    if not dst.exists():
        dst.symlink_to(DFTB_PREFIX, target_is_directory=True)

    return {
        "job_id": job_id,
        "prepared": True,
        "elements": sorted(set(symbols)),
        "species_order": species,
        "param_set": param_set
    }


@API.post("/run/{job_id}")
def run(job_id: str):
    work = DATA / job_id
    if not (work / "dftb_in.hsd").exists():
        return JSONResponse({"error":"prepare first"}, status_code=400)

    # Recreate symlink if needed
    dst = work / "parameters"
    if not dst.exists():
        dst.symlink_to(DFTB_PREFIX, target_is_directory=True)

    env = os.environ.copy()
    r = subprocess.run(["bash","-lc","dftb+ > out.log 2>&1"], cwd=work, env=env, timeout=1200)
    ok = (work / "detailed.out").exists()
    return {"job_id": job_id, "ok": ok, "rc": r.returncode}


@API.get("/results/{job_id}")
def results(job_id: str):
    work = DATA / job_id
    out = {"job_id": job_id, "files": sorted([p.name for p in work.iterdir()]) if work.exists() else []}
    det = work / "detailed.out"
    if det.exists():
        E = None
        for line in det.read_text().splitlines():
            if "Total Energy:" in line:
                E = line.strip().split()[-2]  # Hartree (as printed)
                break
        out["total_energy_Hartree"] = E
    return out

@API.post("/prepare-genformat")
async def prepare_genformat(
    genformat: str = Form(...),
    param_set: str = Form("mio-1-1")
):
    try:
        ensure_param_set_exists(param_set)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    norm = sanitize_geometry_to_genformat(genformat)  # ← normalize & validate
    symbols = norm["symbols_per_atom"]
    species = norm["species_order"]

    job_id = str(uuid.uuid4())
    work = DATA / job_id
    work.mkdir(parents=True, exist_ok=True)

    slakos_lines = slako_block(species, param_set)
    maxl = guess_maxl(symbols, param_set)

    hsd_text = HSD.render(genblock=norm["genblock"], slakos=slakos_lines, maxl=maxl)
    (work / "dftb_in.hsd").write_text(hsd_text)
    (work / "GENFORMAT.txt").write_text(norm["genblock"])
    (work / "INPUT_RAW.txt").write_text(_strip_fences(genformat))

    dst = work / "parameters"
    if not dst.exists():
        dst.symlink_to(DFTB_PREFIX, target_is_directory=True)

    return {
        "job_id": job_id,
        "prepared": True,
        "elements": sorted(set(symbols)),
        "species_order": species,
        "param_set": param_set,
        "detected_input": norm["kind"],  # 'genformat' or 'poscar'
    }
