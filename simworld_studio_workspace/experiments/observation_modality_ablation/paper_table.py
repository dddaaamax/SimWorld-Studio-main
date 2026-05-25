"""Render the modality-ablation LaTeX table used in the paper.

Reads raw per-episode summary.json files under ``results/`` and emits a
LaTeX ``table*`` environment to stdout (SR + SoftSPL, 3 models × seen/unseen,
mean±std, with per-group bold highlighting).

The aggregation is deliberately redone from the raw files here rather than
reading ``aggregate.json`` — this script is the canonical source of the
numbers in the paper and must be independently verifiable.

Usage::

    PYTHONPATH=. python3 -m experiments.observation_modality_ablation.paper_table
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, stdev

from .config import MODALITIES, MODELS, RESULTS_DIR

MODEL_ORDER = ["qwen25_27b", "qwen25_9b", "qwen25_2b"]
MODALITY_ORDER = ["rgb", "depth", "rgb_depth", "text_only"]
SPLIT_ORDER = ["seen", "unseen"]
MODEL_LABEL = {"qwen25_27b": "Qwen3.5-27B", "qwen25_9b": "Qwen3.5-9B", "qwen25_2b": "Qwen3.5-2B"}
MODALITY_LABEL = {"rgb": "RGB", "depth": "Depth", "rgb_depth": "RGB + Depth", "text_only": "Text only"}

# Don't mark a row bold if the group's max is below this (pure noise rows).
BOLD_MIN = 0.005


def _collect(model: str, modality: str, split: str) -> tuple[list[float], list[float]]:
    dirp = RESULTS_DIR / model / modality / split
    srs, sss = [], []
    for p in dirp.rglob("ep_*/summary.json"):
        d = json.loads(p.read_text())
        srs.append(float(d.get("SR", 0) or 0))
        sss.append(float(d.get("SoftSPL", 0) or 0))
    return srs, sss


def _mstd(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    m = mean(xs)
    s = stdev(xs) if len(xs) > 1 else 0.0
    return m, s


def _fmt(m: float, s: float, bold: bool) -> str:
    if m == 0 and s == 0:
        return "0"
    if bold:
        return f"\\textbf{{.{int(round(m*1000)):03d}}}{{\\scriptsize$\\pm$.{int(round(s*1000)):03d}}}"
    return f".{int(round(m*1000)):03d}{{\\scriptsize$\\pm$.{int(round(s*1000)):03d}}}"


def main() -> None:
    data = {}
    for model in MODEL_ORDER:
        for modality in MODALITY_ORDER:
            for split in SPLIT_ORDER:
                data[(model, modality, split)] = _collect(model, modality, split)

    # Per (model, split, metric), collect bolded modalities (ties share).
    best = {}
    for model in MODEL_ORDER:
        for split in SPLIT_ORDER:
            for metric_idx, metric_name in enumerate(["sr", "ss"]):
                vals = [
                    (mod, mean(data[(model, mod, split)][metric_idx])
                          if data[(model, mod, split)][metric_idx] else 0)
                    for mod in MODALITY_ORDER
                ]
                top = max(v for _, v in vals)
                if top >= BOLD_MIN:
                    best[(model, split, metric_name)] = {
                        mod for mod, v in vals if abs(v - top) < 1e-6
                    }
                else:
                    best[(model, split, metric_name)] = set()

    out = []
    out.append(r"\begin{table*}[t]")
    out.append(r"\centering")
    out.append(r"\caption{\textbf{Ablation on observation modalities across model scales.}")
    out.append(r"Each Qwen3.5 model is evaluated on ObjectNav with three vision configurations")
    out.append(r"(RGB, Depth, RGB+Depth) and a text-only baseline where the agent receives")
    out.append(r"goal distance and bearing scalars but no image.")
    out.append(r"\textbf{Seen} reports evaluation on 15 training maps (44 episodes).")
    out.append(r"\textbf{Unseen} reports evaluation on 2 held-out maps (6 episodes), shared")
    out.append(r"across all conditions. Values are reported as mean$\pm$std. Metrics are success")
    out.append(r"rate (SR) and SoftSPL (SS), both in $[0,1]$ ($\uparrow$). Best values within")
    out.append(r"each (model scale, split, metric) group are highlighted in \textbf{bold}; tied")
    out.append(r"values share the bold marker; tied zeros are not bolded.}")
    out.append(r"\label{tab:ablation_obs}")
    out.append(r"\vspace{4pt}")
    out.append(r"\small")
    out.append(r"\setlength{\tabcolsep}{4.5pt}")
    out.append(r"\renewcommand{\arraystretch}{1.2}")
    out.append(r"\resizebox{\textwidth}{!}{%")
    out.append(r"\begin{tabular}{@{}l cc cc cc cc cc cc@{}}")
    out.append(r"\toprule")
    out.append(r"& \multicolumn{4}{c}{\textbf{" + MODEL_LABEL[MODEL_ORDER[0]] + r"}}")
    out.append(r"& \multicolumn{4}{c}{\textbf{" + MODEL_LABEL[MODEL_ORDER[1]] + r"}}")
    out.append(r"& \multicolumn{4}{c}{\textbf{" + MODEL_LABEL[MODEL_ORDER[2]] + r"}} \\")
    out.append(r"\cmidrule(lr){2-5} \cmidrule(lr){6-9} \cmidrule(lr){10-13}")
    out.append(r"\textbf{Observation}")
    out.append(r"& \multicolumn{2}{c}{\textit{Seen}} & \multicolumn{2}{c}{\textit{Unseen}}")
    out.append(r"& \multicolumn{2}{c}{\textit{Seen}} & \multicolumn{2}{c}{\textit{Unseen}}")
    out.append(r"& \multicolumn{2}{c}{\textit{Seen}} & \multicolumn{2}{c}{\textit{Unseen}} \\")
    out.append(r"\cmidrule(lr){2-3} \cmidrule(lr){4-5}")
    out.append(r"\cmidrule(lr){6-7} \cmidrule(lr){8-9}")
    out.append(r"\cmidrule(lr){10-11} \cmidrule(lr){12-13}")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$")
    out.append(r"& \textbf{SR}$\uparrow$ & \textbf{SS}$\uparrow$ \\")
    out.append(r"\midrule")

    for mod in MODALITY_ORDER:
        cells: list[str] = []
        for model in MODEL_ORDER:
            for split in SPLIT_ORDER:
                srs, sss = data[(model, mod, split)]
                m_sr, s_sr = _mstd(srs)
                m_ss, s_ss = _mstd(sss)
                cells.append(_fmt(m_sr, s_sr, mod in best[(model, split, "sr")]))
                cells.append(_fmt(m_ss, s_ss, mod in best[(model, split, "ss")]))
        out.append(f"{MODALITY_LABEL[mod]}")
        # 6 (model × split) groups × 2 metrics = 12 cells per row.
        for i in range(6):
            sep = r" \\" if i == 5 else ""
            out.append(f"& {cells[i*2]} & {cells[i*2+1]}{sep}")
        out.append("")

    out.append(r"\bottomrule")
    out.append(r"\end{tabular}")
    out.append(r"}")
    out.append(r"\end{table*}")

    print("\n".join(out))


if __name__ == "__main__":
    main()
