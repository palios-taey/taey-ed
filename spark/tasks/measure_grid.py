"""
measure_grid_reference.py  —  REFERENCE IMPLEMENTATION (handoff to Supervisor/taey-ed)
=====================================================================================
Deterministic CV measurement of wave/grid exercise quantities (amplitude, wavelength)
from a screenshot, to back a server-side `send_to_llm` question_type='measure_grid'.

WHY THIS EXISTS
---------------
On Khan a95ad54270 ("Wave properties", amplitude), EVERY vision reasoner under-counted
the true amplitude of 4 grid squares: worker(Qwen)=2, Grok-Heavy=1, Perplexity-DR=1-2,
Gemini-DeepThink=1 — even when Grok/Gemini stated the correct method. LLM vision cannot
reliably COUNT grid squares. This module measures them mechanically instead.

ARCHITECTURE (per Supervisor, 2026-06-15)
-----------------------------------------
- Frozen-Mac-compatible: the Mac already ships the screenshot to /generate. This CV runs
  SERVER-SIDE on that image; the Mac is unchanged.
- HARD GATE (non-negotiable): a CV that mis-measures burns mastery EXACTLY like the LLM.
  ONE validation point (a95ad54270 => 4) is necessary, NOT sufficient. Until validated
  across MULTIPLE real grid screens, grid questions ESCALATE — do NOT auto-submit an
  unvalidated measurement. So this function RETURNS A CONFIDENCE VERDICT; the caller
  must treat `confident=False` as "escalate, do not submit".
  Policy: "Deterministic-when-proven, escalate-when-not."

CALLER CONTRACT
---------------
- `per_square_unit` is parsed by the caller from the question text
  ("Each square on the grid represents N units") — default 1.0.
- `measure` in {"amplitude","wavelength"}; the caller picks it from the question
  ("What is the wave's amplitude?" / "...wavelength?").
- Returns squares AND units. Caller multiplies/uses `value_units`.
- If `confident=False`, caller ESCALATES (never auto-submits).

No scipy dependency — numpy + PIL only.
"""

from __future__ import annotations
import numpy as np
from PIL import Image


# ---- tunables (conservative; the gate prefers ESCALATE over a wrong submit) ----
RED = lambda R, G, B: (R > 180) & (G < 120) & (B < 120)        # the wave curve
DARK = lambda R, G, B: (R < 90) & (G < 90) & (B < 90)          # drawn equilibrium line
GRAY = lambda R, G, B: (np.abs(R - G) < 14) & (np.abs(G - B) < 14) & (R > 180) & (R < 234)
INT_TOL = 0.18          # |value - nearest_int| must be <= this to be "clean integer"
ASPECT_TOL = 0.18       # |1 - h_spacing/v_spacing| must be <= this (true square grid)
MIN_WAVE_COLS = 80      # the wave must span at least this many px horizontally


def _wave_bbox(red: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, x1, y_crest, y_trough) of the WAVE = the largest connected
    red component. This is what isolates the curve from browser-chrome red (the streak
    flame, the red "Not quite yet" feedback panel, tab/chrome accents) which only form
    small specks. Requires scipy.ndimage; if unavailable on the server, substitute any
    8-connectivity labeler (or a union-find over the ~10k red pixels)."""
    from scipy import ndimage  # server note: optional dep; swap for a numpy union-find if absent
    lbl, n = ndimage.label(red, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return None
    sizes = ndimage.sum(np.ones_like(lbl), lbl, index=range(1, n + 1))
    biggest = int(np.argmax(sizes)) + 1
    ys, xs = np.where(lbl == biggest)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())


def _gridlines(mask_1d: np.ndarray) -> list[int]:
    """Cluster indices whose presence-count exceeds 55% of max into gridline centers."""
    if mask_1d.max() == 0:
        return []
    thr = mask_1d.max() * 0.55
    cand = [i for i, v in enumerate(mask_1d) if v > thr]
    if not cand:
        return []
    lines, grp = [], [cand[0]]
    for i in cand[1:]:
        if i - grp[-1] <= 4:
            grp.append(i)
        else:
            lines.append(int(np.mean(grp))); grp = [i]
    lines.append(int(np.mean(grp)))
    return lines


def _spacing(lines: list[int]) -> float | None:
    if len(lines) < 2:
        return None
    d = np.diff(lines)
    # use the modal/median gap; a missing line (e.g. obscured by the equilibrium
    # line) shows up as a 2x gap and the median is robust to a few of those
    med = float(np.median(d))
    if med <= 0:
        return None
    return med


def _crest_xs(red: np.ndarray, crest_y: int, band: int = 4) -> list[int]:
    """x-centers of crests = clusters of columns whose topmost red pixel is near crest_y."""
    top = np.where(red.any(axis=0), red.argmax(axis=0), 10**6)
    cols = [x for x in range(red.shape[1]) if top[x] <= crest_y + band]
    if not cols:
        return []
    cr, grp = [], [cols[0]]
    for x in cols[1:]:
        if x - grp[-1] <= 15:
            grp.append(x)
        else:
            cr.append(int(np.mean(grp))); grp = [x]
    cr.append(int(np.mean(grp)))
    return cr


def measure_wave_grid(image_path: str, measure: str = "amplitude",
                      per_square_unit: float = 1.0) -> dict:
    """Measure amplitude or wavelength of a gridded wave figure.

    Returns a dict; ALWAYS inspect `confident` before using `value_units`.
    On any detection failure returns {confident: False, reason: ...} (=> escalate).
    """
    im = Image.open(image_path).convert("RGB")
    a = np.asarray(im).astype(int)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    red_full = RED(R, G, B)

    wb = _wave_bbox(red_full)
    if wb is None:
        return {"confident": False, "reason": "no wave component found", "measure": measure}
    x0, x1, crest, trough = wb
    if x1 - x0 < MIN_WAVE_COLS:
        return {"confident": False, "reason": "wave too small / not a continuous curve", "measure": measure}

    # pad the wave component bbox to capture grid + equilibrium around it
    pad = max(8, (trough - crest) // 6)
    Y0, Y1 = max(0, crest - pad), min(a.shape[0], trough + pad)
    X0, X1 = max(0, x0 - 4), min(a.shape[1], x1 + 4)
    sub = a[Y0:Y1, X0:X1]
    rS, gS, bS = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
    redS = RED(rS, gS, bS)
    crestS = int(np.where(redS.any(axis=1))[0].min())
    troughS = int(np.where(redS.any(axis=1))[0].max())

    # grid spacing both axes (square-grid confidence gate)
    grayS = GRAY(rS, gS, bS)
    v_lines = _gridlines(grayS.sum(axis=1))   # horizontal gridlines -> vertical spacing
    h_lines = _gridlines(grayS.sum(axis=0))   # vertical gridlines  -> horizontal spacing
    v_sp = _spacing(v_lines)
    h_sp = _spacing(h_lines)
    if not v_sp or not h_sp:
        return {"confident": False, "reason": "grid spacing not detected", "measure": measure}
    aspect_err = abs(1.0 - h_sp / v_sp)

    # equilibrium: prefer a drawn dark midline; else the crest/trough midpoint
    darkS = DARK(rS, gS, bS)
    drow = darkS.sum(axis=1)
    midpoint = (crestS + troughS) / 2.0
    eq = midpoint
    if drow.max() > 0.5 * sub.shape[1]:
        cand_eq = int(np.argmax(drow))
        if abs(cand_eq - midpoint) <= v_sp:     # sanity: drawn line near the wave center
            eq = float(cand_eq)

    result = {"measure": measure, "per_square_unit": per_square_unit,
              "v_spacing_px": round(v_sp, 1), "h_spacing_px": round(h_sp, 1),
              "square_aspect_err": round(aspect_err, 3),
              "plot_bbox": [int(x0), int(Y0), int(x1), int(Y1)]}

    if measure == "amplitude":
        squares = (eq - crestS) / v_sp
        # cross-check symmetry: eq->trough should match eq->crest
        sym_err = abs(((troughS - eq) / v_sp) - squares)
        result["symmetry_err_squares"] = round(sym_err, 3)
    elif measure == "wavelength":
        cr = _crest_xs(redS, crestS)
        if len(cr) < 2:
            return {"confident": False, "reason": "fewer than 2 crests for wavelength", "measure": measure}
        gaps = np.diff(cr)
        # drop a clipped edge crest (first/last) by using the median interior gap
        squares = float(np.median(gaps)) / h_sp
        result["crest_count"] = len(cr)
    else:
        return {"confident": False, "reason": f"unsupported measure '{measure}'", "measure": measure}

    nearest = round(squares)
    int_err = abs(squares - nearest)
    result.update({"value_squares_raw": round(squares, 3),
                   "value_squares": nearest,
                   "value_units": nearest * per_square_unit,
                   "integer_err": round(int_err, 3)})

    # ---- CONFIDENCE GATE (all must hold) ----
    reasons = []
    if aspect_err > ASPECT_TOL:
        reasons.append(f"grid not square (aspect_err {aspect_err:.2f})")
    if int_err > INT_TOL:
        reasons.append(f"not a clean integer (raw {squares:.2f})")
    if nearest <= 0:
        reasons.append("non-positive measurement")
    if measure == "amplitude" and result.get("symmetry_err_squares", 0) > 0.35:
        reasons.append(f"crest/trough asymmetric ({result['symmetry_err_squares']:.2f})")
    result["confident"] = (len(reasons) == 0)
    result["reason"] = "ok" if result["confident"] else "; ".join(reasons)
    return result


if __name__ == "__main__":
    # Self-test on the validation screen (expected amplitude=4, wavelength=5).
    import sys, json
    img = sys.argv[1] if len(sys.argv) > 1 else \
        "/tmp/taey-ed-claude-diagnosing/khan_academy_a95ad54270bfedaa/screenshot.png"
    for m in ("amplitude", "wavelength"):
        print(m, json.dumps(measure_wave_grid(img, measure=m, per_square_unit=1.0)))
