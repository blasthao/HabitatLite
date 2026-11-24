from pathlib import Path
import os
import sys
import re
import time
import json
import queue
import threading
import traceback
from contextlib import redirect_stdout, redirect_stderr

import streamlit as st
import pandas as pd

try:
    from habitatlite.core import (
        scan_pairs, get_mask_labels, validate_pairs, run_habit_analysis,
        inspect_habit_environment,
    )
except ModuleNotFoundError as exc:
    if exc.name == "habit" or (exc.name and exc.name.startswith("habit.")):
        st.set_page_config(page_title="HabitatLite", layout="wide")
        st.error(
            "未找到 HABIT 核心依赖（Python 模块名为 habit）。"
            "请在启动 Streamlit 的同一个 Python 环境中安装 habitat-analysis。"
        )
        st.code(
            "python -m pip install habitat-analysis -i https://pypi.org/simple",
            language="bash",
        )
        st.caption("安装后请重新启动 Streamlit。验证命令：python -c \"import habit; print(habit.__version__)\"")
        st.stop()
    raise

APP_ROOT = Path(os.environ.get("HABITAT_LITE_HOME", Path(__file__).resolve().parent)).expanduser().resolve()
DEFAULT_IMAGES = APP_ROOT / "images"
DEFAULT_MASKS = APP_ROOT / "masks"
DEFAULT_OUTPUT = APP_ROOT / "habitat_results"
APP_UI_VERSION = "0.10.2"  # Adds paper-style display-only voxel-cluster PCA


class _QueueTextStream:
    """File-like stream that forwards writes from the worker thread to a queue."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, event_queue, stream_name):
        self.event_queue = event_queue
        self.stream_name = stream_name

    def write(self, value):
        if value:
            self.event_queue.put(("log", self.stream_name, str(value)))
        return len(value) if value is not None else 0

    def flush(self):
        return None

    def isatty(self):
        return False


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _run_habit_analysis_with_live_logs(pairs, output_dir, cfg, progress_bar, status_slot, log_slot, timing_collector=None):
    """Run HABIT in a worker and render callbacks/stdout/stderr in Streamlit's main thread."""
    events = queue.Queue()
    state = {}
    stdout_proxy = _QueueTextStream(events, "stdout")
    stderr_proxy = _QueueTextStream(events, "stderr")
    completed_lines = []
    current_lines = {"stdout": "", "stderr": ""}
    last_message = "准备任务……"

    def worker_callback(percent, message):
        # Streamlit APIs must only be called by the script's main thread.
        events.put(("progress", percent, str(message)))

    def worker():
        try:
            with redirect_stdout(stdout_proxy), redirect_stderr(stderr_proxy):
                state["summary"] = run_habit_analysis(
                    pairs, output_dir, cfg, callback=worker_callback
                )
        except BaseException as exc:
            state["error"] = exc
            state["traceback"] = traceback.format_exc()
        finally:
            events.put(("finished", None, None))

    def observe_tqdm_line(line):
        if not timing_collector or not line:
            return
        cleaned = _ANSI_ESCAPE_RE.sub("", line).strip()
        stage_key = None
        if "Cohort.map[_ComputeUnits]" in cleaned:
            stage_key = "Cohort.map[_ComputeUnits]"
        elif "Cohort.map[_AssignPrecomputedUnits]" in cleaned:
            stage_key = "Cohort.map[_AssignPrecomputedUnits]"
        if stage_key is None:
            return
        match = re.search(r"(?<!\d)(\d{1,3})%", cleaned)
        if not match:
            return
        percent = int(match.group(1))
        stamp = time.perf_counter()
        item = timing_collector.setdefault(stage_key, {})
        if "start_perf_counter" not in item:
            item["start_perf_counter"] = stamp
        item["last_percent"] = percent
        if percent >= 100 and "end_perf_counter" not in item:
            item["end_perf_counter"] = stamp
            item["elapsed_seconds"] = round(stamp - item["start_perf_counter"], 3)

    def consume_text(stream_name, chunk):
        # Handle \r as a terminal line rewrite (as used by tqdm), not as a new line.
        current = current_lines.get(stream_name, "")
        for char in chunk:
            if char == "\r":
                observe_tqdm_line(current)
                current = ""
            elif char == "\n":
                observe_tqdm_line(current)
                cleaned = _ANSI_ESCAPE_RE.sub("", current).strip()
                if cleaned:
                    completed_lines.append(cleaned)
                current = ""
            else:
                current += char
        observe_tqdm_line(current)
        current_lines[stream_name] = current

    def render_logs():
        active = []
        for name in ("stderr", "stdout"):
            line = _ANSI_ESCAPE_RE.sub("", current_lines.get(name, "")).strip()
            if line:
                active.append(line)
        lines = completed_lines[-24:] + active
        if lines:
            log_slot.code("\n".join(lines[-26:]), language="text")

    thread = threading.Thread(target=worker, name="HabitatLite-HABIT-worker", daemon=True)
    thread.start()
    finished = False
    last_render = 0.0
    while not finished or thread.is_alive():
        try:
            event = events.get(timeout=0.15)
        except queue.Empty:
            event = None

        changed = False
        if event is not None:
            kind, a, b = event
            if kind == "progress":
                try:
                    progress_bar.progress(min(100, max(0, int(a))), text=str(b))
                except Exception:
                    progress_bar.progress(0, text=str(b))
                last_message = str(b)
                status_slot.caption(last_message)
            elif kind == "log":
                consume_text(a, b)
                changed = True
            elif kind == "finished":
                finished = True
                changed = True

        # Drain queued writes so frequent tqdm updates do not cause excessive rerenders.
        while True:
            try:
                extra = events.get_nowait()
            except queue.Empty:
                break
            kind, a, b = extra
            if kind == "progress":
                try:
                    progress_bar.progress(min(100, max(0, int(a))), text=str(b))
                except Exception:
                    progress_bar.progress(0, text=str(b))
                last_message = str(b)
                status_slot.caption(last_message)
            elif kind == "log":
                consume_text(a, b)
                changed = True
            elif kind == "finished":
                finished = True
                changed = True

        now = time.monotonic()
        if changed and (now - last_render >= 0.18 or finished):
            render_logs()
            last_render = now

    thread.join()
    # Flush any final non-newline-terminated tqdm status lines.
    for name in ("stderr", "stdout"):
        tail = _ANSI_ESCAPE_RE.sub("", current_lines.get(name, "")).strip()
        if tail:
            observe_tqdm_line(tail)
            completed_lines.append(tail)
    render_logs()

    if "error" in state:
        status_slot.error("HABIT 分析线程异常终止。详细 traceback 已写入下方日志。")
        if state.get("traceback"):
            completed_lines.extend(state["traceback"].splitlines()[-30:])
            render_logs()
        raise state["error"]
    return state.get("summary")


def _create_kmeans_feature_scatter(output_dir, cfg):
    """Visualize HABIT's exported assignments for either shared-cohort design.

    direct_pooling: each point is an ROI voxel.
    two_step: each point is a supervoxel.

    One feature is shown as a 1-D feature-space strip with categorical habitat rows;
    two features use their native axes; >2 features use a PCA projection.
    The function never refits K-means. For large voxel tables, it samples points
    only for display while retaining the full table for cluster counts/centres.
    """
    output_dir = Path(output_dir)
    unit_label = "ROI voxel" if cfg.get("design") == "direct_pooling" else "supervoxel"

    candidates = []
    preferred_dirs = [output_dir / "feature_tables", output_dir / "habit_raw"]
    for folder in preferred_dirs:
        if folder.exists():
            candidates.extend(p for p in folder.rglob("*.csv") if p.is_file())
    if not candidates:
        candidates = [p for p in output_dir.rglob("*.csv") if p.is_file()]

    # Prefer HABIT's unit/cluster assignment table, but do not assume a single
    # fixed filename across HABIT patch versions.
    label_names = ("habitats", "habitat", "cluster", "cluster_id", "label")
    assignment_tables = []
    for path in candidates:
        try:
            header = pd.read_csv(path, nrows=5)
        except Exception:
            continue
        lower_to_original = {str(c).strip().lower(): c for c in header.columns}
        label_col = next((lower_to_original[n] for n in label_names if n in lower_to_original), None)
        if label_col is None:
            continue
        path_text = str(path).lower()
        score = 0
        if "feature_tables" in path_text:
            score += 30
        if "habit_features" in path.name.lower():
            score += 25
        if "2_habitats" in path.name.lower() or "features_2" in path.name.lower():
            score += 20
        if "habit_raw" in path_text:
            score += 10
        assignment_tables.append((score, path.stat().st_mtime, path, label_col))

    if not assignment_tables:
        return None

    _, _, table_path, label_col = max(assignment_tables, key=lambda x: (x[0], x[1]))

    try:
        import numpy as np
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from sklearn.decomposition import PCA

        data = pd.read_csv(table_path)
        metadata = {
            "subject", "subject_id", "patient", "patient_id", "case", "case_id",
            "supervoxel", "supervoxel_id", "voxel", "voxel_id", "unit", "unit_id",
            "habitats", "habitat", "cluster", "cluster_id", "label", "count",
            "voxel_count", "n_voxels", "id", "index", "voxel_index", "unit_index",
            "x", "y", "z", "i", "j", "k", "row", "col", "column", "slice",
            "x_index", "y_index", "z_index", "world_x", "world_y", "world_z",
        }
        feature_cols = [c for c in data.columns if str(c).strip().lower() not in metadata]
        numeric_cols = []
        for col in feature_cols:
            converted = pd.to_numeric(data[col], errors="coerce")
            if converted.notna().sum() >= 3 and converted.nunique(dropna=True) > 1:
                data[col] = converted
                numeric_cols.append(col)
        if not numeric_cols:
            return {"error": f"在分配表 {table_path.name} 中没有找到可绘图的数值特征列。"}

        labels = pd.to_numeric(data[label_col], errors="coerce")
        valid = labels.notna() & data[numeric_cols].notna().all(axis=1)
        plotted = data.loc[valid, numeric_cols].astype(float)
        plotted_labels = labels.loc[valid].astype(int)
        if len(plotted) < 3:
            return {"error": "有效聚类分配记录少于 3 条，无法绘制散点图。"}

        varying_cols = [c for c in numeric_cols if plotted[c].std(ddof=0) > 0]
        if not varying_cols:
            return {"error": "所有候选聚类特征均为常数，无法绘制散点图。"}
        plotted = plotted[varying_cols]
        habitat_ids = sorted(plotted_labels.unique().tolist())
        cmap_name = str(cfg.get("habitat_palette", "tab10"))
        try:
            cmap = plt.get_cmap(cmap_name)
        except Exception:
            cmap = plt.get_cmap("tab10")
        color_by_habitat = {h: cmap(i % cmap.N) for i, h in enumerate(habitat_ids)}

        seed = int(cfg.get("random_seed", 42))
        rng = np.random.default_rng(seed)
        max_points = max(1000, int(cfg.get("scatter_max_points", 50000)))
        if len(plotted) > max_points:
            # Proportional per-habitat sampling; cluster counts and 1-D means below
            # are computed from the full table, not this display-only subset.
            label_array = plotted_labels.to_numpy()
            selected_indices = []
            for habitat in habitat_ids:
                idx = np.flatnonzero(label_array == habitat)
                n_take = min(len(idx), max(1, int(round(max_points * len(idx) / len(plotted)))))
                selected_indices.extend(rng.choice(idx, size=n_take, replace=False).tolist())
            if len(selected_indices) > max_points:
                selected_indices = rng.choice(selected_indices, size=max_points, replace=False).tolist()
            elif len(selected_indices) < max_points:
                remaining = np.setdiff1d(np.arange(len(plotted)), np.asarray(selected_indices, dtype=int), assume_unique=False)
                n_extra = min(max_points - len(selected_indices), len(remaining))
                if n_extra:
                    selected_indices.extend(rng.choice(remaining, size=n_extra, replace=False).tolist())
            display_idx = np.asarray(sorted(set(selected_indices)), dtype=int)
            display_data = plotted.iloc[display_idx]
            display_labels = plotted_labels.iloc[display_idx]
        else:
            display_data = plotted
            display_labels = plotted_labels

        fig, ax = plt.subplots(figsize=(10.5, 6.2), constrained_layout=True)
        if len(varying_cols) == 1:
            feature = varying_cols[0]
            for y_index, habitat in enumerate(habitat_ids):
                values = display_data.loc[display_labels == habitat, feature].to_numpy()
                jitter = rng.normal(0.0, 0.075, size=len(values))
                y = np.full(len(values), y_index, dtype=float) + jitter
                ax.scatter(values, y, s=12 if unit_label == "ROI voxel" else 26,
                           alpha=0.30 if unit_label == "ROI voxel" else 0.62,
                           color=color_by_habitat[habitat], edgecolors="none",
                           rasterized=True)
                full_values = plotted.loc[plotted_labels == habitat, feature]
                if len(full_values):
                    center = float(full_values.mean())
                    ax.scatter([center], [y_index], marker="X", s=115,
                               color=color_by_habitat[habitat], edgecolors="black",
                               linewidths=0.8, zorder=5)
            ax.set_yticks(range(len(habitat_ids)))
            ax.set_yticklabels([f"Habitat {h}" for h in habitat_ids])
            ax.set_ylabel("Assigned habitat (categorical; jitter is for display only)")
            ax.set_xlabel(f"{feature} (native feature value)")
            ax.set_title(f"K-means assignment in 1-D feature space ({unit_label}-level)")
            ax.grid(axis="x", alpha=0.22)
            handles = []
            for h in habitat_ids:
                n = int((plotted_labels == h).sum())
                mean_value = float(plotted.loc[plotted_labels == h, feature].mean())
                handles.append(Line2D([0], [0], marker="o", linestyle="", color="w",
                                      markerfacecolor=color_by_habitat[h], markeredgecolor="none",
                                      markersize=7, label=f"Habitat {h} (n={n:,}, mean={mean_value:.2f})"))
            handles.append(Line2D([0], [0], marker="X", linestyle="", color="black",
                                  markerfacecolor="white", markersize=8, label="Cluster mean"))
            ax.legend(handles=handles, frameon=True, loc="best")
            note = (
                f"Each row represents one {unit_label}. Only one clustering feature was available; "
                "the y-axis denotes the assigned habitat, not a second feature. "
                "Jitter is for visibility only; X markers indicate means computed from all records."
            )
        elif len(varying_cols) == 2:
            x_name, y_name = varying_cols
            coords = display_data[[x_name, y_name]].to_numpy(dtype=float)
            ax.set_xlabel(f"{x_name} (native feature scale)")
            ax.set_ylabel(f"{y_name} (native feature scale)")
            ax.set_title(f"K-means assignment in original 2-D feature space ({unit_label}-level)")
            for habitat in habitat_ids:
                mask = display_labels.to_numpy() == habitat
                pts = coords[mask]
                ax.scatter(pts[:, 0], pts[:, 1], s=12 if unit_label == "ROI voxel" else 25,
                           alpha=0.28 if unit_label == "ROI voxel" else 0.58,
                           color=color_by_habitat[habitat], edgecolors="none",
                           label=f"Habitat {habitat} (n={(plotted_labels == habitat).sum():,})",
                           rasterized=True)
                full_pts = plotted.loc[plotted_labels == habitat, [x_name, y_name]].to_numpy(dtype=float)
                if len(full_pts):
                    center = full_pts.mean(axis=0)
                    ax.scatter([center[0]], [center[1]], marker="X", s=125,
                               color=color_by_habitat[habitat], edgecolors="black",
                               linewidths=0.8, zorder=5)
            ax.legend(frameon=True, loc="best")
            note = f"Each point represents one {unit_label}; a display-only sample may be shown for large cohorts."
        else:
            # Fit PCA on the display sample to keep the figure responsive. The HABIT
            # labels remain the original assignments in the complete feature space.
            pca = PCA(n_components=2, random_state=seed)
            coords = pca.fit_transform(display_data.to_numpy(dtype=float))
            explained = pca.explained_variance_ratio_ * 100
            ax.set_xlabel(f"PC1 ({explained[0]:.1f}% variance)")
            ax.set_ylabel(f"PC2 ({explained[1]:.1f}% variance)")
            ax.set_title(f"K-means assignment (PCA projection; {unit_label}-level)")
            for habitat in habitat_ids:
                mask = display_labels.to_numpy() == habitat
                pts = coords[mask]
                ax.scatter(pts[:, 0], pts[:, 1], s=12 if unit_label == "ROI voxel" else 25,
                           alpha=0.28 if unit_label == "ROI voxel" else 0.58,
                           color=color_by_habitat[habitat], edgecolors="none",
                           label=f"Habitat {habitat} (n={(plotted_labels == habitat).sum():,})",
                           rasterized=True)
                if len(pts):
                    center = pts.mean(axis=0)
                    ax.scatter([center[0]], [center[1]], marker="X", s=125,
                               color=color_by_habitat[habitat], edgecolors="black",
                               linewidths=0.8, zorder=5)
            ax.legend(frameon=True, loc="best")
            note = (
                f"Each point represents one {unit_label}. PCA is used only to visualize the exported features; "
                "the HABIT assignments were not re-fitted in this 2-D projection."
            )

        ax.text(0.01, -0.18, note, transform=ax.transAxes, fontsize=8.5,
                va="top", ha="left", wrap=True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        figures_dir = output_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        png_path = figures_dir / "kmeans_feature_space_scatter.png"
        pdf_path = figures_dir / "kmeans_feature_space_scatter.pdf"
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        fig.savefig(pdf_path, bbox_inches="tight")
        plt.close(fig)
        return {
            "png": png_path,
            "pdf": pdf_path,
            "source": table_path,
            "n_points": int(len(plotted)),
            "n_display": int(len(display_data)),
            "n_features": int(len(varying_cols)),
            "feature_names": varying_cols,
            "unit_label": unit_label,
            "note": note,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}



def _create_paper_style_voxel_pca(pairs, output_dir, cfg):
    """Create a paper-style 2-D PCA scatter using display-only voxel descriptors.

    Cluster labels always come from the already-exported HABIT label maps. This
    function does NOT refit K-means and does NOT modify any NIfTI image or label.

    For the common HU-only workflow, a genuine 2-D PCA cannot be calculated from
    HU alone (one feature has only one principal component). To provide a useful
    2-D visualization without pretending that PC2 exists, this figure uses real,
    display-only local image descriptors: voxel intensity, ROI-restricted 3x3x3
    local mean, local standard deviation, and local gradient magnitude. These
    auxiliary descriptors are used for visualization only, never for clustering.
    """
    import numpy as np
    import SimpleITK as sitk
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    output_dir = Path(output_dir)
    maps_dir = output_dir / "habitat_maps"
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    modality_names = [str(x) for x in cfg.get("modalities", [cfg.get("modality", "CT")])]
    primary_modality = str(cfg.get("modality", modality_names[0] if modality_names else "CT"))
    if primary_modality not in modality_names:
        modality_names.insert(0, primary_modality)

    max_points = max(1000, int(cfg.get("pca_max_points", 30000)))
    per_case_quota = max(100, max_points // max(1, len(pairs)))
    rng = np.random.default_rng(int(cfg.get("random_seed", 42)))

    feature_chunks = []
    label_chunks = []
    full_counts = {}
    skipped_cases = []
    descriptor_names = []

    def _array_for_label_grid(image_path, label_shape):
        image = sitk.ReadImage(str(image_path))
        array = sitk.GetArrayFromImage(image).astype(np.float32, copy=False)
        # HABIT promotes genuine 2-D images to singleton-slice 3-D for processing.
        if array.ndim == 2 and len(label_shape) == 3 and label_shape[0] == 1:
            array = array[np.newaxis, ...]
        elif array.ndim == 3 and len(label_shape) == 2 and array.shape[0] == 1:
            array = array[0]
        if tuple(array.shape) != tuple(label_shape):
            raise ValueError(
                f"影像与生境图数组尺寸不一致：image={array.shape}, labels={label_shape}"
            )
        return array

    def _local_descriptors(array, roi_valid, flat_indices):
        """Vectorized 3x3x3 ROI-only local descriptors at sampled voxel locations."""
        arr = np.asarray(array, dtype=np.float32)
        if arr.ndim == 2:
            arr = arr[np.newaxis, ...]
            roi_local = roi_valid[np.newaxis, ...] if roi_valid.ndim == 2 else roi_valid
        else:
            roi_local = roi_valid

        coords = np.column_stack(np.unravel_index(flat_indices, arr.shape)).astype(np.intp, copy=False)
        zz, yy, xx = coords.T
        pad_arr = np.pad(arr, 1, mode="edge")
        pad_roi = np.pad(roi_local.astype(bool, copy=False), 1, mode="constant", constant_values=False)

        values = []
        valid_neighbours = []
        for dz in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    values.append(pad_arr[zz + 1 + dz, yy + 1 + dy, xx + 1 + dx])
                    valid_neighbours.append(pad_roi[zz + 1 + dz, yy + 1 + dy, xx + 1 + dx])

        patch = np.column_stack(values).astype(np.float32, copy=False)
        patch_valid = np.column_stack(valid_neighbours)
        counts = np.maximum(patch_valid.sum(axis=1), 1).astype(np.float32)
        safe_patch = np.where(patch_valid, patch, 0.0)
        local_mean = safe_patch.sum(axis=1) / counts
        local_var = np.maximum((safe_patch * safe_patch).sum(axis=1) / counts - local_mean * local_mean, 0.0)
        local_sd = np.sqrt(local_var)

        centre = arr.ravel()[flat_indices]
        # Central differences when both neighbours are in the selected ROI;
        # one-sided differences at the ROI boundary; zero if neither is present.
        def _axis_gradient(plus_offset, minus_offset):
            pz, py, px = plus_offset
            mz, my, mx = minus_offset
            plus = pad_arr[zz + 1 + pz, yy + 1 + py, xx + 1 + px]
            minus = pad_arr[zz + 1 + mz, yy + 1 + my, xx + 1 + mx]
            plus_ok = pad_roi[zz + 1 + pz, yy + 1 + py, xx + 1 + px]
            minus_ok = pad_roi[zz + 1 + mz, yy + 1 + my, xx + 1 + mx]
            grad = np.zeros(len(flat_indices), dtype=np.float32)
            both = plus_ok & minus_ok
            grad[both] = (plus[both] - minus[both]) * 0.5
            only_plus = plus_ok & ~minus_ok
            grad[only_plus] = plus[only_plus] - centre[only_plus]
            only_minus = minus_ok & ~plus_ok
            grad[only_minus] = centre[only_minus] - minus[only_minus]
            return grad

        gz = _axis_gradient((1, 0, 0), (-1, 0, 0))
        gy = _axis_gradient((0, 1, 0), (0, -1, 0))
        gx = _axis_gradient((0, 0, 1), (0, 0, -1))
        gradient = np.sqrt(gx * gx + gy * gy + gz * gz)
        return np.column_stack((centre, local_mean, local_sd, gradient)).astype(np.float32, copy=False)

    for pair in pairs:
        sid = str(pair.subject_id)
        label_path = maps_dir / f"{sid}_habitats.nii.gz"
        if not label_path.exists():
            # Fallback for HABIT patch versions that use a slightly different map name.
            matches = list(maps_dir.rglob(f"*{sid}*habitat*.nii.gz")) if maps_dir.exists() else []
            label_path = matches[0] if len(matches) == 1 else None
        if label_path is None or not Path(label_path).exists():
            skipped_cases.append(sid)
            continue

        try:
            label_image = sitk.ReadImage(str(label_path))
            labels = sitk.GetArrayFromImage(label_image).astype(np.int32, copy=False)
            if labels.ndim == 2:
                labels = labels[np.newaxis, ...]

            source_paths = {primary_modality: Path(pair.image)}
            for modality_name, modality_path in (getattr(pair, "additional_images", None) or {}).items():
                source_paths[str(modality_name)] = Path(modality_path)
            if any(name not in source_paths for name in modality_names):
                raise ValueError("缺少一个或多个配置的影像模态路径")

            modality_arrays = {
                name: _array_for_label_grid(source_paths[name], labels.shape)
                for name in modality_names
            }
            roi = labels > 0
            finite = roi.copy()
            for array in modality_arrays.values():
                finite &= np.isfinite(array)
            flat_valid = np.flatnonzero(finite.ravel())
            if flat_valid.size < 3:
                skipped_cases.append(sid)
                continue

            local_labels = labels.ravel()[flat_valid]
            selected_parts = []
            for habitat in np.unique(local_labels):
                habitat_indices = flat_valid[local_labels == habitat]
                quota = max(1, int(round(per_case_quota * len(habitat_indices) / len(flat_valid))))
                if len(habitat_indices) > quota:
                    habitat_indices = rng.choice(habitat_indices, size=quota, replace=False)
                selected_parts.append(habitat_indices)
                full_counts[int(habitat)] = full_counts.get(int(habitat), 0) + int(np.count_nonzero(local_labels == habitat))
            chosen = np.concatenate(selected_parts) if selected_parts else np.array([], dtype=np.int64)
            if chosen.size > per_case_quota:
                chosen = rng.choice(chosen, size=per_case_quota, replace=False)
            if chosen.size < 3:
                skipped_cases.append(sid)
                continue

            roi_valid = finite
            case_features = []
            case_names = []
            for modality_name in modality_names:
                descriptors = _local_descriptors(modality_arrays[modality_name], roi_valid, chosen)
                case_features.append(descriptors)
                prefix = "HU" if modality_name.upper() == "CT" else modality_name
                case_names.extend([
                    f"{prefix}_voxel_intensity",
                    f"{prefix}_local_mean_3x3x3",
                    f"{prefix}_local_sd_3x3x3",
                    f"{prefix}_local_gradient",
                ])
            feature_chunks.append(np.column_stack(case_features).astype(np.float32, copy=False))
            label_chunks.append(labels.ravel()[chosen].astype(np.int32, copy=False))
            if not descriptor_names:
                descriptor_names = case_names
        except Exception as exc:
            skipped_cases.append(f"{sid} ({type(exc).__name__}: {exc})")

    if not feature_chunks:
        return {"error": "未能从输出的生境图和原始影像中提取 PCA 显示特征。请检查 image/label map 是否匹配。"}

    X = np.vstack(feature_chunks).astype(np.float64, copy=False)
    y = np.concatenate(label_chunks).astype(np.int32, copy=False)
    valid = np.isfinite(X).all(axis=1)
    X, y = X[valid], y[valid]
    if len(y) < 3:
        return {"error": "有效 PCA 样本少于 3 个。"}
    n_display_candidates = int(len(y))

    # Enforce a strict global plotting cap after the patient-stratified sampling.
    # This only affects figure rendering; the saved habitat maps and counts are untouched.
    if len(y) > max_points:
        selected = []
        for habitat in sorted(np.unique(y).tolist()):
            idx = np.flatnonzero(y == habitat)
            quota = min(len(idx), max(1, int(round(max_points * len(idx) / len(y)))))
            if len(idx) > quota:
                idx = rng.choice(idx, size=quota, replace=False)
            selected.extend(idx.tolist())
        selected = np.asarray(selected, dtype=np.int64)
        if len(selected) > max_points:
            selected = rng.choice(selected, size=max_points, replace=False)
        elif len(selected) < max_points:
            remaining = np.setdiff1d(np.arange(len(y)), selected, assume_unique=False)
            extra = min(max_points - len(selected), len(remaining))
            if extra:
                selected = np.concatenate([selected, rng.choice(remaining, size=extra, replace=False)])
        X, y = X[selected], y[selected]

    varying = np.std(X, axis=0) > 1e-10
    X = X[:, varying]
    used_names = [name for name, keep in zip(descriptor_names, varying) if keep]
    if X.shape[1] < 2:
        return {"error": "可用于 PCA 的非恒定显示特征少于 2 个，无法生成二维 PCA 图。"}

    # Scaling is for PCA visualization only; it does not alter the trained K-means model.
    X_scaled = StandardScaler().fit_transform(X)
    pca = PCA(n_components=2, random_state=int(cfg.get("random_seed", 42)))
    coords = pca.fit_transform(X_scaled)

    habitat_ids = sorted(np.unique(y).tolist())
    class_to_index = {hid: idx for idx, hid in enumerate(habitat_ids)}
    color_values = np.asarray([class_to_index[int(v)] for v in y], dtype=np.int32)
    palette = plt.get_cmap("tab10")
    cmap = ListedColormap([palette(i % palette.N) for i in range(len(habitat_ids))])
    norm = BoundaryNorm(np.arange(-0.5, len(habitat_ids) + 0.5, 1), cmap.N)

    fig, ax = plt.subplots(figsize=(8.6, 7.2), constrained_layout=True)
    scatter = ax.scatter(
        coords[:, 0], coords[:, 1], c=color_values, cmap=cmap, norm=norm,
        s=10, alpha=0.62, edgecolors="none", rasterized=True,
    )
    for hid in habitat_ids:
        mask = y == hid
        if np.any(mask):
            center = coords[mask].mean(axis=0)
            ax.scatter(
                [center[0]], [center[1]], marker="X", s=150,
                color=cmap(class_to_index[hid]), edgecolors="black",
                linewidths=0.9, zorder=5,
            )
    cbar = fig.colorbar(scatter, ax=ax, ticks=np.arange(len(habitat_ids)), fraction=0.046, pad=0.04)
    cbar.ax.set_yticklabels([f"{hid} (n={full_counts.get(hid, 0):,})" for hid in habitat_ids])
    cbar.set_label("Cluster / Habitat")
    explained = pca.explained_variance_ratio_ * 100
    ax.set_xlabel(f"Principal Component 1 ({explained[0]:.1f}%)")
    ax.set_ylabel(f"Principal Component 2 ({explained[1]:.1f}%)")
    ax.set_title("PCA of Voxel Clusters", fontsize=15, pad=12)
    ax.grid(alpha=0.16, linewidth=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    descriptor_text = ", ".join(used_names)
    note = (
        "Display-only PCA descriptors: " + descriptor_text + ". Cluster colors are inherited from the saved HABIT labels. "
        "The local descriptors and PCA projection were NOT used to fit K-means; this figure visualizes how the existing "
        "HU-/modality-based clusters relate to local image context. PCA cannot guarantee visually separated groups. "
        "Points are stratified samples across cases and habitats; cluster counts in the color bar are from all valid ROI voxels."
    )
    fig.text(0.5, -0.025, note, ha="center", va="top", fontsize=8.2, wrap=True)

    png_path = figures_dir / "pca_voxel_cluster_scatter.png"
    pdf_path = figures_dir / "pca_voxel_cluster_scatter.pdf"
    fig.savefig(png_path, dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    return {
        "png": str(png_path),
        "pdf": str(pdf_path),
        "n_points": int(len(y)),
        "n_display_candidates_after_case_sampling": n_display_candidates,
        "n_habitats": int(len(habitat_ids)),
        "habitat_counts": {str(h): int(full_counts.get(h, 0)) for h in habitat_ids},
        "n_features": int(X.shape[1]),
        "feature_names": used_names,
        "explained_variance_ratio": [float(v) for v in pca.explained_variance_ratio_],
        "skipped_cases": skipped_cases,
        "note": note,
    }

def _parse_additional_modality_dirs(raw_text: str, primary_modality: str):
    """Parse lines of the form MODALITY_NAME=D:/path/to/nifti_folder."""
    mapping = {}
    errors = []
    for line_no, raw_line in enumerate((raw_text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"第 {line_no} 行缺少 '='，应为 模态名=文件夹路径")
            continue
        name, folder_text = (x.strip() for x in line.split("=", 1))
        if not name or not folder_text:
            errors.append(f"第 {line_no} 行模态名或路径为空")
            continue
        if not all(ch.isalnum() or ch in "_-" for ch in name):
            errors.append(f"第 {line_no} 行模态名 {name!r} 只能包含字母、数字、下划线或连字符")
            continue
        if name.upper() in {"ROI", "MASK", primary_modality.upper()}:
            errors.append(f"第 {line_no} 行模态名 {name!r} 与保留名称/主模态重复")
            continue
        if name in mapping:
            errors.append(f"附加模态名称重复：{name}")
            continue
        folder = Path(folder_text).expanduser()
        if not folder.is_dir():
            errors.append(f"第 {line_no} 行目录不存在：{folder}")
            continue
        mapping[name] = folder
    return mapping, errors

TEXT = {
    "zh": {
        "language": "界面语言", "zh": "中文", "en": "English", "ja": "日本語",
        "page_title": "HabitatLite 生境分析", "title": "HabitatLite｜轻量化生境分析",
        "subtitle": "输入 2D/3D NIfTI 图像与多标签 Mask → 选择 ROI Label → 设置参数 → 批量生境分析与 Mask 导出",
        "input": "1. 输入数据", "images": "images 文件夹路径", "masks": "masks 文件夹路径",
        "output": "输出文件夹路径", "modality": "影像类型", "ct": "CT", "mri": "MRI",
        "input_help": "默认目录为项目根目录下的 images/ 和 masks/。文件名需一一对应，如 P001.nii.gz。",
        "params": "2. 生境参数", "design": "聚类设计",
        "design_help": "Voxel-wise pooled K-means：将全队列 ROI 体素直接池化并建立共享模型；Two-step HABIT：每例先进行超体素划分，再汇总全队列超体素特征并建立共享模型。",
        "k": "生境数量 k", "seed": "随机种子", "supervoxels": "每例超体素数（two_step）",
        "figures": "输出每例彩色生境图", "intermediate": "保留 HABIT 原始中间结果（会产生较多文件）",
        "qc": "3. 生境质控（QC Gate）",
        "qc_help": "本版本不提取影像组学特征，仅检查生境区域大小与占比。请使用独立影像组学软件提取特征。",
        "min_voxels": "最小区域体素数（用于小生境质控提示）", "min_fraction": "小生境提示阈值（ROI 占比）",
        "pairing": "数据配对与 Mask Label", "matched": "匹配病例", "missing_masks": "缺少 Mask 的图像",
        "missing_images": "缺少图像的 Mask", "id": "ID", "image": "Image", "mask": "Mask",
        "missing_mask_msg": "以下图像未找到同名 Mask：", "missing_image_msg": "以下 Mask 未找到同名图像：",
        "check_dirs": "请确认项目根目录下存在 images/ 和 masks/，或手动填写两个文件夹路径。",
        "scan_error": "扫描输入目录失败：", "select_label": "选择要分析的 Mask Label",
        "all_labels": "所有非零标签", "label_help": "可只分析某个标签；选择“所有非零标签”会将所有非零体素合并为 ROI。",
        "no_label": "未检测到可用的正整数 Mask Label。请检查 Mask 是否包含非零 ROI。",
        "run": "🚀 一键开始生境分析", "input_qc": "输入质控未通过，请先处理以下问题：",
        "ready": "准备任务……", "done": "完成", "success": "分析完成：成功 {success} 例，失败/未导出 {failed} 例",
        "out_dir": "结果目录：", "files": "输出文件", "volume_matrix": "生境体积/占比矩阵预览",
        "download_matrix": "下载生境矩阵 CSV", "qc_result": "QC Gate 结果", "download_qc": "下载 QC 报告",
        "no_rad": "当前版本不提取影像组学特征。请使用独立影像组学软件对导出的多标签生境 Mask 进行特征提取。",
        "manifest": "下载运行参数记录 JSON", "error": "本次运行未完成。请把完整终端日志发来，以便继续修复。",
        "bad_label": "未检测到可用的正整数 Mask Label。", "validation": "输入质控未通过，请先处理以下问题：",
        "failed_scan": "扫描输入目录失败：",
    },
    "en": {
        "language": "Interface language", "zh": "中文", "en": "English", "ja": "日本語",
        "page_title": "HabitatLite Habitat Analysis", "title": "HabitatLite | Lightweight Habitat Analysis",
        "subtitle": "2D/3D NIfTI images + multi-label masks → select ROI label → configure → batch habitat analysis and mask export",
        "input": "1. Input data", "images": "Images folder path", "masks": "Masks folder path",
        "output": "Output folder path", "modality": "Modality", "ct": "CT", "mri": "MRI",
        "input_help": "Defaults to images/ and masks/ under the project directory. Image and mask filenames must match, e.g. P001.nii.gz.",
        "params": "2. Habitat parameters", "design": "Clustering design",
        "design_help": "Voxel-wise pooled K-means: pool ROI voxels across the cohort and fit one shared model; Two-step HABIT: partition each case into supervoxels, aggregate their features, and fit one shared cohort model.",
        "k": "Number of habitats k", "seed": "Random seed", "supervoxels": "Supervoxels per case (two_step)",
        "figures": "Export a colored habitat map for each case", "intermediate": "Keep raw HABIT intermediate results (creates more files)",
        "qc": "3. Habitat quality control (QC Gate)",
        "qc_help": "This version does not extract radiomics. It checks habitat size and fraction only. Use dedicated radiomics software for feature extraction.",
        "min_voxels": "Minimum region voxel count (QC warning threshold)", "min_fraction": "Small-habitat warning threshold (fraction of ROI)",
        "pairing": "Data pairing and mask labels", "matched": "Matched cases", "missing_masks": "Images missing masks",
        "missing_images": "Masks missing images", "id": "ID", "image": "Image", "mask": "Mask",
        "missing_mask_msg": "No matching mask found for these images: ", "missing_image_msg": "No matching image found for these masks: ",
        "check_dirs": "Ensure images/ and masks/ exist under the project directory, or enter both paths manually.",
        "scan_error": "Failed to scan input folders: ", "select_label": "Select mask label to analyze",
        "all_labels": "All nonzero labels", "label_help": "Select one label, or choose “All nonzero labels” to combine all nonzero voxels into the ROI.",
        "no_label": "No positive integer mask labels detected. Check that the masks contain a nonzero ROI.",
        "run": "🚀 Start habitat analysis", "input_qc": "Input QC failed. Please resolve these issues first:",
        "ready": "Preparing task…", "done": "Complete", "success": "Analysis complete: {success} succeeded, {failed} failed/not exported",
        "out_dir": "Output directory: ", "files": "Output files", "volume_matrix": "Habitat volume/fraction matrix preview",
        "download_matrix": "Download habitat matrix CSV", "qc_result": "QC Gate results", "download_qc": "Download QC report",
        "no_rad": "Radiomics extraction is disabled. Use your dedicated radiomics software with the exported multi-label habitat masks.",
        "manifest": "Download run manifest JSON", "error": "The run did not complete. Share the full terminal log for troubleshooting.",
        "bad_label": "No positive integer mask labels detected.", "validation": "Input QC failed. Please resolve these issues first:",
        "failed_scan": "Failed to scan input folders: ",
    },
    "ja": {
        "language": "表示言語", "zh": "中文", "en": "English", "ja": "日本語",
        "page_title": "HabitatLite 生境解析", "title": "HabitatLite｜軽量生境解析",
        "subtitle": "2D/3D NIfTI 画像とマルチラベル Mask → ROI Label 選択 → パラメータ設定 → 一括解析・Mask 出力",
        "input": "1. 入力データ", "images": "images フォルダーのパス", "masks": "masks フォルダーのパス",
        "output": "出力フォルダーのパス", "modality": "画像モダリティ", "ct": "CT", "mri": "MRI",
        "input_help": "既定ではプロジェクト直下の images/ と masks/ を使用します。画像と Mask のファイル名を一致させてください（例：P001.nii.gz）。",
        "params": "2. 生境パラメータ", "design": "クラスタリング方式",
        "design_help": "Voxel-wise pooled K-means：全症例の ROI ボクセルをプールして共通モデルを作成。Two-step HABIT：各症例をスーパーボクセルに分割し、特徴量を集約して共通モデルを作成。",
        "k": "生境数 k", "seed": "乱数シード", "supervoxels": "症例ごとの超ボクセル数（two_step）",
        "figures": "症例ごとのカラー生境図を出力", "intermediate": "HABIT の中間結果を保持（ファイル数が増えます）",
        "qc": "3. 生境の品質管理（QC Gate）",
        "qc_help": "本バージョンでは Radiomics 特徴量を抽出せず、生境領域の大きさと割合のみを確認します。特徴量抽出には専用ソフトウェアを使用してください。",
        "min_voxels": "最小領域ボクセル数（QC 警告しきい値）", "min_fraction": "小生境の警告しきい値（ROI に占める割合）",
        "pairing": "データの対応付けと Mask Label", "matched": "対応症例数", "missing_masks": "Mask がない画像",
        "missing_images": "画像がない Mask", "id": "ID", "image": "画像", "mask": "Mask",
        "missing_mask_msg": "対応する Mask が見つからない画像：", "missing_image_msg": "対応する画像が見つからない Mask：",
        "check_dirs": "プロジェクト直下に images/ と masks/ があることを確認するか、両方のパスを入力してください。",
        "scan_error": "入力フォルダーのスキャンに失敗しました：", "select_label": "解析する Mask Label を選択",
        "all_labels": "すべての非ゼロラベル", "label_help": "単一ラベルを選択するか、「すべての非ゼロラベル」を選択して非ゼロ領域を ROI として統合します。",
        "no_label": "正の整数 Mask Label が見つかりません。Mask に非ゼロ ROI があるか確認してください。",
        "run": "🚀 生境解析を開始", "input_qc": "入力 QC に失敗しました。以下を修正してください：",
        "ready": "タスクを準備中…", "done": "完了", "success": "解析完了：成功 {success} 症例、失敗/未出力 {failed} 症例",
        "out_dir": "出力先：", "files": "出力ファイル", "volume_matrix": "生境体積・割合マトリックス",
        "download_matrix": "生境マトリックス CSV をダウンロード", "qc_result": "QC Gate 結果", "download_qc": "QC レポートをダウンロード",
        "no_rad": "Radiomics 特徴量抽出は無効です。出力されたマルチラベル生境 Mask を専用ソフトウェアで解析してください。",
        "manifest": "実行設定 JSON をダウンロード", "error": "処理が完了しませんでした。診断のため、ターミナルログ全体を共有してください。",
        "bad_label": "正の整数 Mask Label が見つかりません。", "validation": "入力 QC に失敗しました。以下を修正してください：",
        "failed_scan": "入力フォルダーのスキャンに失敗しました：",
    },
}


EXTRA_TEXT = {
    "zh": {
        "figures": "输出病例彩色生境图及队列统计图（PNG + PDF）",
        "k_assessment": "所选 K 的自动占比筛查",
        "k_warning": "注意：这是基于标签图占比/缺失率的启发式筛查，不是最优 K 的统计学证明。",
        "fig_gallery": "队列级论文展示图",
        "k_pass": "占比筛查未发现严重警告；仍需进行聚类有效性和稳定性验证。",
        "k_review": "当前 K 的部分生境可能过小或缺失。建议比较较小 K，并进行聚类有效性与稳定性分析。",
        "download_k": "下载 K 筛查报告 CSV",
    },
    "en": {
        "figures": "Export per-case habitat maps and cohort plots (PNG + PDF)",
        "k_assessment": "Automatic occupancy screen for selected K",
        "k_warning": "This is a heuristic screen based on label-map occupancy/missingness, not statistical proof of optimal K.",
        "fig_gallery": "Cohort-level publication figures",
        "k_pass": "No severe occupancy warning detected; cluster validity and stability still require evaluation.",
        "k_review": "Some habitats at the selected K may be too small or absent. Compare a smaller K and assess validity/stability.",
        "download_k": "Download K-screening report CSV",
    },
    "ja": {
        "figures": "症例ごとの生境図とコホート統計図を出力（PNG + PDF）",
        "k_assessment": "選択した K の自動占有率スクリーニング",
        "k_warning": "これはラベルマップの占有率・欠落率に基づくヒューリスティックな判定であり、最適 K の統計的証明ではありません。",
        "fig_gallery": "コホートレベルの論文用図",
        "k_pass": "占有率に重大な警告はありませんが、クラスタ妥当性と安定性の検証が必要です。",
        "k_review": "選択した K では一部の生境が小さすぎる、または欠落している可能性があります。小さい K と比較し、妥当性・安定性を評価してください。",
        "download_k": "K スクリーニング CSV をダウンロード",
    },
}

st.set_page_config(page_title="HabitatLite", page_icon="🧬", layout="wide")

with st.sidebar:
    lang_label = st.selectbox("🌐 " + "Language / 语言 / 言語", ["中文", "English", "日本語"], index=0)
lang = {"中文": "zh", "English": "en", "日本語": "ja"}[lang_label]
D = TEXT[lang]
def t(key):
    return EXTRA_TEXT.get(lang, {}).get(key, D.get(key, key))

st.title(t("title"))
st.caption(t("subtitle"))

with st.sidebar.expander("HABIT 原生环境诊断 / HABIT environment", expanded=False):
    habit_env = inspect_habit_environment()
    allow_habit_version_mismatch = st.checkbox(
        "版本不匹配时仍允许继续（不推荐）",
        value=False,
        help="此应用按 habitat-analysis==2.0.0 核对；新版 API/输出行为可能不同。",
    )
    st.write(f"Python: `{habit_env.get('python_version')}`")
    st.write(f"Python executable: `{habit_env.get('python_executable')}`")
    st.write(f"Distribution version: `{habit_env.get('distribution_version')}`")
    st.write(f"Imported module version: `{habit_env.get('module_version')}`")
    st.write(f"Imported module path: `{habit_env.get('module_path')}`")
    if habit_env.get("recipes"):
        st.markdown("**Official HABIT recipe signatures**")
        st.json(habit_env["recipes"])
    if habit_env.get("explicit_spec_api"):
        st.markdown("**Public explicit-stage API signatures**")
        st.json(habit_env["explicit_spec_api"])
    if habit_env.get("dependencies"):
        st.markdown("**Dependency versions**")
        st.json(habit_env["dependencies"])
    if habit_env.get("diagnostic_error"):
        st.error(habit_env["diagnostic_error"])
    elif not habit_env.get("version_matches_supported"):
        st.warning(
            "当前 HABIT 版本与本应用的锁定测试基线 2.0.0 不一致。"
            "为提高复现性，请使用 requirements.txt 中锁定的版本，并记录现有环境。"
        )
    else:
        st.success("检测到预期的 HABIT 2.0.0 API 基线。")

with st.sidebar:
    st.header(t("input"))
    images_dir = st.text_input(t("images"), value=str(DEFAULT_IMAGES))
    masks_dir = st.text_input(t("masks"), value=str(DEFAULT_MASKS))
    output_dir = st.text_input(t("output"), value=str(DEFAULT_OUTPUT))
    modality_options = [t("ct"), t("mri")]
    modality_label = st.selectbox(t("modality"), modality_options)
    modality = "CT" if modality_options.index(modality_label) == 0 else "MRI"
    additional_modality_text = st.text_area(
        "附加影像模态目录（可选，多模态）",
        value="", height=90,
        placeholder="ADC=D:/study/ADC\nT2=D:/study/T2",
        help="每行填写一个已与主影像配准的模态，格式为 模态名=文件夹路径。每个目录按 NIfTI 文件名与主影像/Mask 自动配对。",
    )
    additional_modality_dirs, modality_parse_errors = _parse_additional_modality_dirs(
        additional_modality_text, modality
    )
    for parse_error in modality_parse_errors:
        st.error(parse_error)
    st.caption(t("input_help"))

    st.header(t("params"))
    design = st.selectbox(
        t("design"), ["direct_pooling", "two_step"], index=0,
        format_func=lambda x: {
            "direct_pooling": "Voxel-wise pooled K-means：全队列 ROI 体素直接聚类（默认快速模式）",
            "two_step": "Two-step HABIT：SLIC 超体素 → 队列池化 → 统一聚类",
        }[x],
        help=(
            "Voxel-wise pooled K-means：跳过超体素步骤，将所有病例 ROI 内的体素特征直接池化，"
            "拟合一个共享 K-means 模型，再将标签回填到各病例；适合快速体素级分区，但大 ROI/多模态时会占用更多内存。"
            "Two-step HABIT：每例先进行超体素分割，汇总超体素特征后再拟合共享模型，空间区域通常更紧凑。"
        ),
    )
    if design == "direct_pooling":
        st.info(
            "当前为默认快速模式：每个 ROI 体素作为一个样本参与全队列共享聚类；"
            "跳过 SLIC 和超体素特征汇总。该模式不施加显式空间连续性约束，且总体内存需求随 ROI 体素数和模态数增加。"
        )
    else:
        st.info(
            "Two-step HABIT：先在每例内部执行超体素划分，再使用超体素均值特征进行队列级池化和统一聚类。"
            "输出结构与 Voxel-wise 模式一致；具体 QC 深度及图表数量由 Fast / Full 分析级别决定。"
        )

    fast_clustering_enabled = st.toggle(
        "Fast Clustering｜快速聚类模式（默认开启）",
        value=True,
        help=(
            "开启：保留 HABIT 特征提取、共享 K-means、标签分配和 NIfTI 输出；跳过 HABIT 原生量化、"
            "三维连通成分 QC、代理聚类指标及论文级图表。关闭后执行 Full Habitat Analysis。"
        ),
    )
    analysis_level = "fast" if fast_clustering_enabled else "full"
    if fast_clustering_enabled:
        st.info(
            "Fast Clustering 默认执行 CT/HU 体素级全队列 pooled K-means，固定 K=5；保留 NIfTI 生境图、"
            "体积/占比矩阵、基础几何/标签/占比 QC，并额外生成轻量 PCA 可视化。"
            "不运行 MSI、ITH、Graph、代理指标、三维连通性 QC、病例小 FOV 和论文级队列图。"
        )
        st.caption(
            "方法学提示：当前默认是原始 HU 单特征 K-means。单特征 PCA 只有 PC1，不能产生真正的 PC2；"
            "因此软件同时输出 PC1-生境分布图和一个明确标记为 display-only 的 ROI 空间坐标 PCA 图。"
        )
        st.warning(
            "AUC 选择 K 必须提供患者级二分类结局（如转移/未转移、复发/未复发）并定义预测模型与交叉验证方案。"
            "目前未提供结局表，本次默认只固定 K=5；这里不会把 Elbow/Silhouette 伪称为 AUC 选 K。"
        )
    else:
        st.info(
            "Full Habitat Analysis：执行 HABIT 原生 Volume、MSI、ITH、Non-radiomics、Graph 量化，"
            "并运行完整空间 QC、代理聚类指标及论文级图表。正式研究分析建议使用此模式。"
        )

    k_selection_mode = st.radio(
        "K 值确定方式 / K selection",
        options=["fixed", "habit_native"],
        index=0 if fast_clustering_enabled else 1,
        key=f"k_selection_mode_{analysis_level}",
        format_func=lambda x: {
            "habit_native": "自动选择 K（HABIT 原生 K-means validation）",
            "fixed": "固定 K（手动指定）",
        }[x],
        help=(
            "自动模式在一次队列级共享模型拟合中，根据所选准则在 K 范围内选择 K；"
            "Voxel-wise 模式使用池化后的 ROI 体素特征矩阵，Two-step 模式使用池化后的超体素特征矩阵。"
            "快速测试建议固定 K（默认 5）以避免候选 K 搜索；正式分析可在 Full 模式下选择 HABIT 原生自动 K。"
        ),
    )
    native_auto_k = k_selection_mode == "habit_native"
    # The old outer per-K QC rerun modes are intentionally omitted from this
    # streamlined default UI; K auto-selection and fixed-K are both retained.
    auto_select_k = False
    k_selection_strategy = "composite_rank"
    native_k_validation = "elbow"
    if native_auto_k:
        nk1, nk2 = st.columns(2)
        native_k_min = nk1.number_input("自动 K 搜索下限", min_value=2, max_value=19, value=2, step=1)
        auto_k_default_max = 5 if fast_clustering_enabled else 10
        native_k_max = nk2.number_input("自动 K 搜索上限", min_value=2, max_value=20, value=auto_k_default_max, step=1)
        native_k_validation = st.selectbox(
            "自动 K 选择准则",
            options=["elbow", "silhouette", "calinski_harabasz", "davies_bouldin", "gap"],
            index=0,
            format_func=lambda x: {
                "elbow": "Elbow / Kneedle（惯性曲线拐点）",
                "silhouette": "Silhouette（最大化）",
                "calinski_harabasz": "Calinski–Harabasz（最大化）",
                "davies_bouldin": "Davies–Bouldin（最小化）",
                "gap": "Gap statistic（最大化）",
            }[x],
        )
        k_min, k_max = int(native_k_min), int(native_k_max)
        n_habitats = 5  # placeholder only; HABIT chooses the actual K during fitting.
        st.caption(
            "自动 K 的目标是 HABIT 指定的无监督特征空间 validation 准则，并非 AUC。Fast 模式默认候选范围为 K=2–5；"
            "若研究方案要求按临床结局 AUC 选择 K，需要另行提供患者级二分类结局，并采用预先定义的交叉验证/独立验证设计。"
            "生境缺失率和极小区域仍会在拟合后单独报告。"
        )
        if native_k_min > native_k_max:
            st.warning("自动 K 搜索下限不能大于上限。")
    else:
        n_habitats = st.slider("固定生境数量 K", min_value=2, max_value=10, value=5, step=1)
        k_min = k_max = int(n_habitats)
        native_k_min, native_k_max = 2, 10

    random_seed = st.number_input(t("seed"), min_value=0, max_value=999999, value=42, step=1)
    partition_method = "kmeans"
    # Optional HABIT dependency preflight: only needed for SLIC in Two-step mode.
    slic_dependency_missing = False
    slic_dependency_error = None
    n_supervoxels = 30
    partition_n_init = 10
    partition_max_iter = 300
    slic_compactness = 10.0
    gmm_covariance_type = "full"
    if design == "two_step":
        partition_method = st.selectbox(
            "第一阶段超体素划分算法",
            options=["slic", "kmeans", "gmm"], index=0,
            format_func=lambda x: {
                "kmeans": "K-means 超体素（特征分组，不保证空间连通）",
                "slic": "SLIC 超体素（空间邻近 + 特征，强调连续性）",
                "gmm": "Gaussian Mixture 超体素",
            }[x],
            help=(
                "此算法只决定每例内部的超体素分区；随后仍会池化全队列超体素特征并拟合统一的 K-means 生境模型。"
                "SLIC 同时考虑空间位置和特征；K-means/GMM 超体素主要依据特征相似性，不保证空间连通。"
            ),
        )
        n_supervoxels = st.slider(t("supervoxels"), min_value=5, max_value=300, value=30, step=5)
        if partition_method in ("kmeans", "gmm"):
            partition_n_init = st.slider("超体素划分重复初始化次数", min_value=1, max_value=50, value=10)
            partition_max_iter = st.slider("超体素划分最大迭代次数", min_value=50, max_value=1000, value=300, step=50)
        if partition_method == "kmeans":
            st.warning(
                "HABIT 的 K-means supervoxelizer 依据特征相似性分组，不强制空间连通。"
                "若研究需要空间紧凑的初始区域，建议使用 SLIC 并将其作为独立敏感性分析。"
            )
        if partition_method == "slic":
            slic_compactness = st.slider("SLIC compactness", min_value=0.1, max_value=30.0, value=10.0, step=0.5)
            st.caption("SLIC 可改善初始超体素的空间紧凑性，但不保证每个患者都出现每一个全局 habitat。")
            try:
                from skimage.segmentation import slic as _habitlite_slic_check  # noqa: F401
            except Exception as dep_exc:
                slic_dependency_missing = True
                slic_dependency_error = f"{type(dep_exc).__name__}: {dep_exc}"
                st.error(
                    "当前 Python 环境缺少 SLIC 所需依赖 scikit-image。为避免病例计算中途失败，"
                    "已禁用本次运行按钮。请在启动 Streamlit 的同一个环境中安装依赖后重启应用。"
                )
                st.code("python -m pip install scikit-image", language="bash")
                st.caption('也可使用 HABIT 官方可选依赖：python -m pip install "habitat-analysis[slic]"')
                st.caption(f"当前 Streamlit Python：{sys.executable}")
                st.caption(f"依赖检测详情：{slic_dependency_error}")
        if partition_method == "gmm":
            gmm_covariance_type = st.selectbox("GMM covariance type", ["full", "tied", "diag", "spherical"], index=0)
    else:
        st.caption("Voxel-wise pooled K-means 不需要 SLIC/scikit-image；它直接池化所选 ROI 的体素特征。")

    with st.expander("HABIT 原生特征预处理（在 voxel feature stage 内执行）", expanded=False):
        habit_feature_preprocessing = st.selectbox(
            "每例 ROI 特征标准化",
            options=["none", "winsorize_zscore", "winsorize_minmax", "winsorize_only", "zscore"],
            index=0,
            format_func=lambda x: {
                "none": "不处理（保留原始特征尺度）",
                "winsorize_zscore": "Winsorize 1% + Z-score",
                "winsorize_minmax": "Winsorize 1% + Min-Max",
                "winsorize_only": "仅 Winsorize 1%",
                "zscore": "仅 Z-score",
            }[x],
            help=(
                "这些是 HABIT 原生特征预处理组件，作用于提取后的特征列，不改写原始 NIfTI。"
                "CT HU 是否标准化、MRI/ADC 等是否采用 Z-score，应根据研究设计和敏感性分析决定。"
            ),
        )
        habit_winsor_low = st.slider("HABIT Winsorize 下尾比例", 0.0, 0.10, 0.01, 0.005)
        habit_winsor_high = st.slider("HABIT Winsorize 上尾比例", 0.0, 0.10, 0.01, 0.005)
        if habit_winsor_low + habit_winsor_high >= 1.0:
            st.error("Winsorize 两端比例之和必须小于 1。")
        if habit_feature_preprocessing == "none":
            st.caption("当前保留原始特征尺度；这适合作为 CT HU 基线，但不同量纲的多模态特征通常需要评估尺度处理。")
        st.caption("与下方影像级截断/标准化同时开启会形成两层预处理；除非有明确方案，建议二选一。")
    fit_n_init = st.slider("全队列 K-means 重复初始化次数", min_value=1, max_value=50, value=10)
    fit_max_iter = st.slider("全队列 K-means 最大迭代次数", min_value=50, max_value=1000, value=300, step=50)
    if fast_clustering_enabled:
        # Fast mode keeps only the shared model, exported label maps, compact matrices and basic QC.
        make_figures = False
        make_lesion_fov = False
        keep_intermediate = False
        habitat_palette = "tab10"
        overlay_alpha = 0.60
        lesion_crop_margin_fraction = 0.50
        st.caption("Fast 模式已自动关闭病例彩色叠加图、病变小 FOV、论文级队列图和大体积原始中间文件。")
    else:
        make_figures = st.checkbox(t("figures"), value=True)
        with st.expander("生境图伪彩色与小 FOV 设置 / Map colors and lesion-level FOV", expanded=True):
            palette_options = [
                "tab10", "tab20", "Set1", "Set2", "Set3", "Paired", "Dark2", "Accent",
                "viridis", "plasma", "inferno", "magma", "cividis", "turbo", "Spectral",
                "coolwarm", "YlOrRd", "RdYlBu", "gist_ncar",
            ]
            palette_labels = {
                "tab10": "Tab10（分类色，推荐）", "tab20": "Tab20（多分类）",
                "Set1": "Set1（鲜明分类色）", "Set2": "Set2（柔和分类色）",
                "Set3": "Set3（浅色分类色）", "Paired": "Paired（成对颜色）",
                "Dark2": "Dark2（深色分类色）", "Accent": "Accent（强调色）",
                "viridis": "Viridis（蓝绿黄）", "plasma": "Plasma（紫红黄）",
                "inferno": "Inferno（黑紫橙黄）", "magma": "Magma（黑紫粉）",
                "cividis": "Cividis（色觉友好）", "turbo": "Turbo（高对比彩虹）",
                "Spectral": "Spectral（光谱色）", "coolwarm": "Coolwarm（冷暖色）",
                "YlOrRd": "Yellow-Orange-Red（黄橙红）", "RdYlBu": "Red-Yellow-Blue（红黄蓝）",
                "gist_ncar": "Gist Ncar（高饱和彩色）",
            }
            habitat_palette = st.selectbox(
                "生境标签配色风格 / Palette", options=palette_options, index=0,
                format_func=lambda x: palette_labels.get(x, x),
                help="每个离散生境标签使用固定颜色。切换配色只改变可视化，不改变聚类标签或 NIfTI 数据。",
            )
            overlay_alpha = st.slider("伪彩色叠加透明度 / Overlay opacity", 0.10, 0.95, 0.60, 0.05)
            make_lesion_fov = st.checkbox("额外导出病变级小 FOV 彩色图（PNG + PDF）", value=True)
            lesion_crop_margin_fraction = st.slider(
                "小 FOV 周边留白（病灶最大边长的倍数）", min_value=0.10, max_value=2.00,
                value=0.50, step=0.10,
            )
            st.caption(
                "3D 数据：取标签面积最大的轴位层，生成全 FOV 图和病灶框裁剪图。"
                "真正的 2D NIfTI 会作为单层 3D 输入 HABIT，属于兼容模式，并非 HABIT 原生二维算法。"
            )
        keep_intermediate = st.checkbox(
            "保留 HABIT 原始结果与运行快照（推荐，磁盘占用较大）",
            value=True,
            help="保留 habit_raw、所选 ROI mask、处理后的聚类输入（若启用预处理）及 HABIT 单元表，便于复现和绘制特征空间散点图。",
        )

    with st.expander("聚类前预处理（默认严格使用原始影像）", expanded=False):
        st.info(
            "默认不截断强度、不做标准化、不腐蚀 ROI。此时 HABIT 直接读取原始影像文件；"
            "软件仅生成所选 ROI 的二值 mask。只有主动启用下列选项才会改变聚类输入。"
        )
        clip_outliers = st.checkbox("启用 ROI 百分位极值截断", value=False)
        c_low, c_high = st.columns(2)
        clip_p_low = c_low.number_input("下限百分位 (%)", min_value=0.0, max_value=49.0, value=1.0, step=0.5)
        clip_p_high = c_high.number_input("上限百分位 (%)", min_value=51.0, max_value=100.0, value=99.0, step=0.5)
        norm_method = st.selectbox(
            "强度标准化方式",
            options=["none", "zscore", "minmax"],
            index=0,
            format_func=lambda x: {"none": "不标准化（原始强度）", "zscore": "ROI 内 Z-score", "minmax": "ROI 内 Min-Max"}[x],
        )
        erode_voxels = st.slider("ROI 边缘腐蚀体素数", min_value=0, max_value=2, value=0)
        if habit_feature_preprocessing != "none" and (clip_outliers or norm_method != "none"):
            st.warning("当前同时启用了 HABIT 特征级预处理和影像级预处理。两者会连续作用于聚类特征；请确保这是有意设计，并在方法学中记录。")
        hash_inputs = st.checkbox(
            "将输入文件 SHA-256 写入运行记录（较慢）",
            value=False,
            help="用于确认输入文件是否完全相同；大型 NIfTI 数据会增加读取时间。",
        )

    st.header(t("qc"))
    st.caption(t("qc_help"))
    qc_min_voxels = st.number_input(t("min_voxels"), min_value=8, max_value=10000, value=64, step=8)
    qc_min_fraction = st.slider(t("min_fraction"), min_value=0.001, max_value=0.10, value=0.01, step=0.001, format="%.3f")
    qc_min_median_fraction = st.slider(
        "队列中每个生境的中位占比下限（启发式筛查）",
        min_value=0.0, max_value=0.30, value=0.05, step=0.01, format="%.2f",
        help="并非普适临床阈值。设为 0 只关闭这条门槛，不会关闭生境缺失率/小生境率筛查。",
    )
    if fast_clustering_enabled:
        min_proxy_metric_coverage = 0.80
    else:
        min_proxy_metric_coverage = st.slider(
            "代理聚类指标最低病例覆盖率",
            min_value=0.50, max_value=1.0, value=0.80, step=0.05, format="%.2f",
            help="低于该覆盖率时，Silhouette/DB/CH 不参与候选 K 排名，避免只用少数有多个生境的病例偏向某个 K。",
        )

st.markdown("### " + t("pairing"))
pairs, issues, mask_labels = [], {}, []
try:
    if images_dir.strip() and masks_dir.strip() and Path(images_dir).is_dir() and Path(masks_dir).is_dir():
        pairs, issues = scan_pairs(
            Path(images_dir), Path(masks_dir), additional_image_dirs=additional_modality_dirs
        )
        mask_labels = get_mask_labels(pairs)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(t("matched"), len(pairs))
        c2.metric(t("missing_masks"), len(issues.get("missing_masks", [])))
        c3.metric(t("missing_images"), len(issues.get("missing_images", [])))
        c4.metric("影像模态数", 1 + len(additional_modality_dirs))
        if pairs:
            pair_rows = []
            for p in pairs:
                row = {"ID": p.subject_id, t("image"): p.image.name, t("mask"): p.mask.name}
                for modality_name, modality_path in (p.additional_images or {}).items():
                    row[f"{modality_name} Image"] = Path(modality_path).name
                pair_rows.append(row)
            st.dataframe(pd.DataFrame(pair_rows), width="stretch", hide_index=True)
        if issues.get("missing_masks"):
            st.warning(t("missing_mask_msg") + ", ".join(issues["missing_masks"]))
        if issues.get("missing_images"):
            st.warning(t("missing_image_msg") + ", ".join(issues["missing_images"]))
        for modality_name, missing_ids in issues.get("missing_additional_modalities", {}).items():
            st.error(f"附加模态 {modality_name} 缺少 {len(missing_ids)} 例：" + ", ".join(missing_ids[:30]))
    else:
        st.info(t("check_dirs"))
except Exception as exc:
    st.error(t("scan_error") + str(exc))

if mask_labels:
    all_label_text = t("all_labels")
    label_options = [all_label_text] + [str(x) for x in mask_labels]
    selected_label_text = st.selectbox(
        t("select_label"), label_options, index=0, help=t("label_help")
    )
    selected_label = None if selected_label_text == all_label_text else int(selected_label_text)
    if selected_label is None and len(mask_labels) > 1:
        st.warning(
            "当前选择会把所有非零 Mask 标签合并成一个 ROI。"
            "若不同标签有不同语义（如肿瘤/坏死/其他组织），请只选择目标 Label，避免 ROI 定义被改变。"
        )
else:
    selected_label = None
    st.warning(t("no_label"))

st.markdown("---")
can_run = bool(
    images_dir.strip() and masks_dir.strip() and output_dir.strip() and pairs and mask_labels
    and not modality_parse_errors and not issues.get("missing_additional_modalities")
    and (not native_auto_k or int(native_k_min) <= int(native_k_max))
    and not slic_dependency_missing
)
run = st.button(t("run"), type="primary", width="stretch", disabled=not can_run)

if run:
    out = Path(output_dir).expanduser().resolve()
    try:
        validation = validate_pairs(pairs, selected_label=selected_label)
        if validation:
            st.error(t("input_qc"))
            for msg in validation:
                st.write(f"- {msg}")
            st.stop()

        cfg = {
            "analysis_level": analysis_level,
            "fast_clustering": bool(fast_clustering_enabled),
            "modality": modality,
            "modalities": [modality] + list(additional_modality_dirs.keys()),
            "design": design,
            "partition_method": str(partition_method),
            "partition_n_init": int(partition_n_init),
            "partition_max_iter": int(partition_max_iter),
            "slic_compactness": float(slic_compactness),
            "gmm_covariance_type": str(gmm_covariance_type),
            "fit_n_init": int(fit_n_init),
            "fit_max_iter": int(fit_max_iter),
            "n_habitats": int(n_habitats),
            "native_auto_k": bool(native_auto_k),
            "native_k_min": int(native_k_min),
            "native_k_max": int(native_k_max),
            "native_k_validation": str(native_k_validation),
            "auto_select_k": bool(auto_select_k),
            "k_selection_strategy": str(k_selection_strategy),
            "k_min": int(k_min),
            "k_max": int(k_max),
            "habit_feature_preprocessing": str(habit_feature_preprocessing),
            "habit_winsor_low": float(habit_winsor_low),
            "habit_winsor_high": float(habit_winsor_high),
            "random_seed": int(random_seed),
            "n_supervoxels": int(n_supervoxels),
            "make_figures": bool(make_figures),
            "make_pca_figure": True,
            "pca_max_points": 30000,
            "habitat_palette": str(habitat_palette),
            "overlay_alpha": float(overlay_alpha),
            "make_lesion_fov": bool(make_lesion_fov),
            "lesion_crop_margin_fraction": float(lesion_crop_margin_fraction),
            "keep_intermediate": bool(keep_intermediate),
            "selected_label": selected_label,
            "radiomics_enabled": False,
            "use_wavelet": False,
            "log_sigmas": [],
            "qc_min_voxels": int(qc_min_voxels),
            "qc_min_fraction": float(qc_min_fraction),
            "qc_min_median_fraction": float(qc_min_median_fraction),
            "min_proxy_metric_coverage": float(min_proxy_metric_coverage),
            "clip_outliers": bool(clip_outliers),
            "clip_p_low": float(clip_p_low),
            "clip_p_high": float(clip_p_high),
            "norm_method": str(norm_method),
            "erode_voxels": int(erode_voxels),
            "hash_inputs": bool(hash_inputs),
            "habit_environment_at_ui": habit_env,
            "allow_habit_version_mismatch": bool(allow_habit_version_mismatch),
            "app_source_path": str(Path(__file__).resolve()),
        }
        progress = st.progress(0, text=t("ready"))
        status = st.empty()
        st.markdown("#### HABIT 实时运行日志 / Live execution log")
        st.caption(
            "这里同步显示 HABIT 的阶段回调及终端输出；tqdm 的百分比、病例计数和速度信息会尽量实时刷新。"
            "如果某些底层库直接写入系统终端而非 Python stdout/stderr，则仍以终端原始日志为准。"
        )
        with st.expander("展开实时日志 / Live log", expanded=True):
            live_log = st.empty()
        tqdm_timing_capture = {}
        summary = _run_habit_analysis_with_live_logs(
            pairs, out, cfg, progress_bar=progress, status_slot=status, log_slot=live_log,
            timing_collector=tqdm_timing_capture,
        )
        # Additional paper-style PCA figure. This is visualization-only and does not
        # change the HABIT model, cluster labels, masks, or any other analysis output.
        paper_pca_started = time.perf_counter()
        try:
            status.caption("正在生成论文风格的体素生境 PCA 可视化……")
            paper_pca_info = _create_paper_style_voxel_pca(pairs, out, cfg)
        except Exception as paper_pca_exc:
            paper_pca_info = {"error": f"{type(paper_pca_exc).__name__}: {paper_pca_exc}"}
        if isinstance(summary, dict):
            summary["paper_style_pca_visualization"] = paper_pca_info
        paper_pca_elapsed = round(time.perf_counter() - paper_pca_started, 3)
        # Persist the additional display-only PCA provenance without altering the
        # core run configuration, clustering specification, or original PCA record.
        paper_pca_manifest_path = out / "run_manifest.json"
        if paper_pca_manifest_path.exists():
            try:
                paper_pca_manifest = json.loads(paper_pca_manifest_path.read_text(encoding="utf-8"))
                paper_pca_manifest["paper_style_pca_visualization"] = {
                    k: v for k, v in paper_pca_info.items() if k not in ("png", "pdf")
                }
                paper_pca_manifest.setdefault("stage_timings_seconds", {})[
                    "paper_style_pca_visualization_seconds"
                ] = paper_pca_elapsed
                paper_pca_manifest_path.write_text(
                    json.dumps(paper_pca_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as paper_manifest_exc:
                st.warning(f"PCA 图已生成，但无法写入其运行记录：{paper_manifest_exc}")
        # Persist observed tqdm phase durations to the same manifest as core-level timers.
        manifest_path_for_timing = out / "run_manifest.json"
        if manifest_path_for_timing.exists():
            try:
                timing_manifest = json.loads(manifest_path_for_timing.read_text(encoding="utf-8"))
                timing_rows = timing_manifest.setdefault("stage_timings_seconds", {})
                compute_observed = tqdm_timing_capture.get("Cohort.map[_ComputeUnits]", {}).get("elapsed_seconds")
                assign_observed = tqdm_timing_capture.get("Cohort.map[_AssignPrecomputedUnits]", {}).get("elapsed_seconds")
                timing_rows["habit_voxel_feature_extraction_seconds"] = compute_observed
                timing_rows["habit_nearest_centroid_assignment_seconds"] = assign_observed
                fit_total = timing_rows.get("habit_fit_predict_total_seconds")
                if fit_total is not None and compute_observed is not None and assign_observed is not None:
                    residual = max(0.0, float(fit_total) - float(compute_observed) - float(assign_observed))
                    timing_rows["habit_other_internal_stages_estimated_residual_seconds"] = round(residual, 3)
                else:
                    timing_rows["habit_other_internal_stages_estimated_residual_seconds"] = None
                timing_manifest["tqdm_observed_stage_timings_seconds"] = {
                    key: value.get("elapsed_seconds") for key, value in tqdm_timing_capture.items()
                }
                timing_manifest["stage_timing_notes"] = (
                    str(timing_manifest.get("stage_timing_notes", "")) +
                    " Tqdm 阶段时间来自已观察到的 HABIT 进度行；ComputeUnits/AssignPrecomputedUnits 是可观测区间。" +
                    "剩余时间仅是未分离内部阶段的估算，包含池化/模型拟合等工作，不应解释为纯 K-means 拟合耗时。"
                )
                manifest_path_for_timing.write_text(
                    json.dumps(timing_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                summary["manifest"] = timing_manifest
            except Exception as timing_exc:
                st.warning(f"已完成分析，但无法把实时 tqdm 计时追加至 manifest：{type(timing_exc).__name__}: {timing_exc}")
        progress.progress(100, text=t("done"))
        status.success("Fast Clustering 完成。" if fast_clustering_enabled else "Full Habitat Analysis 完成。")
        # Feature-space scatter requires exported unit tables, which Fast mode intentionally omits.
        scatter_info = _create_kmeans_feature_scatter(out, cfg) if not fast_clustering_enabled else None
        st.success(t("success").format(success=summary["success"], failed=summary["failed"]))
        if summary.get("recommended_k") is not None:
            if summary.get("selection_status") == "HABIT_NATIVE_K_VALIDATION":
                mode_name = "Voxel-wise pooled K-means" if design == "direct_pooling" else "Two-step HABIT"
                st.success(f"{mode_name} 自动 K 选择准则：{native_k_validation}；拟合模型实际 K = {summary['recommended_k']}")
            elif summary.get("selection_status") == "SELECTED_LOWEST_K_PASSING_OCCUPANCY_QC":
                st.success(f"外层占比 QC 简约规则选择 K = **{summary['recommended_k']}**（通过门槛候选中的最小值；非 HABIT 原生 K 准则）")
            else:
                st.write(f"本次结果输出 K = **{summary['recommended_k']}**")
        if summary.get("selection_status") == "NO_CANDIDATE_PASSES_QC_FALLBACK_ONLY":
            st.warning("没有候选 K 通过当前 QC；输出 K 仅用于检查，不应视为推荐 K。")
        st.write(t("out_dir") + f"`{out}`")

        timing_manifest_path = out / "run_manifest.json"
        if timing_manifest_path.exists():
            try:
                timing_data = json.loads(timing_manifest_path.read_text(encoding="utf-8"))
                timing_values = timing_data.get("stage_timings_seconds", {})
                timing_labels = {
                    "input_roi_preparation_seconds": "影像 / Mask 预处理与 ROI 准备",
                    "habit_voxel_feature_extraction_seconds": "HABIT ComputeUnits（可观察进度阶段）",
                    "habit_kmeans_fit_and_internal_pooling_seconds": "K-means 拟合 / Pooling 单独耗时（HABIT API 未单独暴露）",
                    "habit_nearest_centroid_assignment_seconds": "HABIT AssignPrecomputedUnits（可观察进度阶段）",
                    "habit_fit_predict_total_seconds": "HABIT fit_predict 核心计算总时间",
                    "habit_other_internal_stages_estimated_residual_seconds": "其余 HABIT 内部阶段剩余时间估算（非纯 K-means）",
                    "habit_result_save_and_native_map_write_seconds": "HABIT 结果保存与原生 NIfTI 写出",
                    "model_artifact_export_seconds": "共享模型及模型摘要保存",
                    "nifti_map_copy_and_case_visualization_seconds": "病例 NIfTI 整理 / 病例可视化",
                    "habitat_volume_fraction_matrix_seconds": "生境体积 / 占比矩阵",
                    "basic_and_spatial_qc_seconds": "基础 QC / 空间连通性 QC",
                    "proxy_feature_space_metrics_seconds": "代理特征空间指标（Silhouette / DB / CH）",
                    "population_occupancy_summary_seconds": "队列生境普遍性统计",
                    "cohort_publication_figures_seconds": "队列级论文图表",
                    "pca_visualization_seconds": "原有轻量 PCA 可视化",
                    "paper_style_pca_visualization_seconds": "新增论文风格体素 PCA（显示用）",
                    "total_before_manifest_seconds": "运行总时间（写入 manifest 前）",
                }
                timing_rows = []
                for key, label in timing_labels.items():
                    value = timing_values.get(key)
                    if value is None:
                        # Keep the seconds column strictly numeric/nullable so
                        # Streamlit's Arrow serializer never mixes floats and text.
                        timing_rows.append({
                            "阶段": label,
                            "耗时（秒）": pd.NA,
                            "状态": "未执行 / 未单独暴露",
                        })
                    else:
                        timing_rows.append({
                            "阶段": label,
                            "耗时（秒）": float(value),
                            "状态": "已记录",
                        })
                timing_df = pd.DataFrame(timing_rows)
                timing_df["耗时（秒）"] = pd.array(timing_df["耗时（秒）"], dtype="Float64")
                with st.expander("运行阶段耗时 / Stage timing", expanded=True):
                    st.dataframe(
                        timing_df,
                        column_config={
                            "耗时（秒）": st.column_config.NumberColumn(
                                "耗时（秒）", format="%.3f"
                            )
                        },
                        width="stretch",
                        hide_index=True,
                    )
                    note = timing_data.get("stage_timing_notes")
                    if note:
                        st.caption(note)
            except Exception as timing_exc:
                st.warning(f"无法读取阶段耗时记录：{timing_exc}")

        if isinstance(scatter_info, dict) and scatter_info.get("png"):
            st.subheader("K-means 生境聚类散点图 / Feature-space scatter")
            st.image(str(scatter_info["png"]), width="stretch")
            st.caption(
                f"数据单位：{scatter_info.get('unit_label', 'analysis unit')}；完整记录 {scatter_info['n_points']:,} 个，"
                f"本图显示 {scatter_info.get('n_display', scatter_info['n_points']):,} 个；特征数 {scatter_info['n_features']}。"
                + "；".join(scatter_info["feature_names"])
                + "。图中使用 HABIT 已导出的聚类分配，不会重新拟合 K-means。"
            )
            st.download_button(
                "下载 K-means 散点图 PNG", scatter_info["png"].read_bytes(),
                file_name=scatter_info["png"].name, mime="image/png",
            )
            st.download_button(
                "下载 K-means 散点图 PDF", scatter_info["pdf"].read_bytes(),
                file_name=scatter_info["pdf"].name, mime="application/pdf",
            )
            if scatter_info.get("note"):
                st.info(scatter_info["note"])
        elif isinstance(scatter_info, dict) and scatter_info.get("error"):
            st.warning("K-means 散点图生成失败，但不影响已完成的生境分析：" + scatter_info["error"])
        elif fast_clustering_enabled:
            st.info("Fast Clustering 不导出 HABIT 单元特征表，因此不生成单元表散点图；轻量 PCA 图在下方单独生成。")
        else:
            st.info(
                "未找到 HABIT 导出的体素/超体素聚类分配表，暂时无法绘制该单元表散点图。"
                "但下方仍会显示基于输出 Mask 生成的轻量 PCA 图。"
            )

        pca_png = out / "figures" / "pca_habitat_projection.png"
        pca_pdf = out / "figures" / "pca_habitat_projection.pdf"
        if pca_png.exists():
            st.subheader("PCA 生境聚类可视化 / PCA habitat visualization")
            st.image(str(pca_png), width="stretch")
            pca_info = summary.get("pca_visualization", {}) if isinstance(summary, dict) else {}
            if pca_info:
                st.caption(
                    f"有效绘图点：{pca_info.get('n_points', 'NA'):,}；显示点数："
                    f"{pca_info.get('n_display', 'NA'):,}；特征：{', '.join(pca_info.get('feature_names', []))}。"
                )
                if pca_info.get("note"):
                    st.info(pca_info["note"])
            if pca_pdf.exists():
                pca_col1, pca_col2 = st.columns(2)
                pca_col1.download_button(
                    "下载 PCA 图 PNG", pca_png.read_bytes(),
                    file_name=pca_png.name, mime="image/png",
                )
                pca_col2.download_button(
                    "下载 PCA 图 PDF", pca_pdf.read_bytes(),
                    file_name=pca_pdf.name, mime="application/pdf",
                )
        elif isinstance(summary, dict) and summary.get("pca_visualization", {}).get("error"):
            st.warning("PCA 图生成失败，但不影响生境 Mask 输出：" + summary["pca_visualization"]["error"])

        # New paper-style PCA is an additional figure; the original PCA output above
        # remains available and unchanged.
        paper_pca = summary.get("paper_style_pca_visualization", {}) if isinstance(summary, dict) else {}
        if isinstance(paper_pca, dict) and paper_pca.get("png") and Path(paper_pca["png"]).exists():
            paper_png = Path(paper_pca["png"])
            paper_pdf = Path(paper_pca["pdf"]) if paper_pca.get("pdf") else paper_png.with_suffix(".pdf")
            st.subheader("论文风格 PCA｜Voxel-cluster visualization")
            st.image(str(paper_png), width="stretch")
            st.caption(
                f"显示点数：{paper_pca.get('n_points', 0):,}；生境数：{paper_pca.get('n_habitats', 'NA')}；"
                f"PCA 特征：{', '.join(paper_pca.get('feature_names', []))}。"
            )
            if paper_pca.get("note"):
                st.info(paper_pca["note"])
            pca_download_cols = st.columns(2)
            pca_download_cols[0].download_button(
                "下载论文风格 PCA PNG", paper_png.read_bytes(),
                file_name=paper_png.name, mime="image/png",
            )
            if paper_pdf.exists():
                pca_download_cols[1].download_button(
                    "下载论文风格 PCA PDF", paper_pdf.read_bytes(),
                    file_name=paper_pdf.name, mime="application/pdf",
                )
        elif isinstance(paper_pca, dict) and paper_pca.get("error"):
            st.warning("论文风格 PCA 图生成失败，但不影响聚类结果和 NIfTI 输出：" + paper_pca["error"])

        st.subheader(t("files"))
        for fp in sorted(out.rglob("*")):
            if fp.is_file():
                st.write(f"- `{fp.relative_to(out)}`")

        feature_csv = out / "habitat_feature_matrix.csv"
        if feature_csv.exists():
            st.subheader(t("volume_matrix"))
            st.dataframe(pd.read_csv(feature_csv), width="stretch")
            st.download_button(t("download_matrix"), feature_csv.read_bytes(),
                               file_name=feature_csv.name, mime="text/csv")
        qc_csv = out / "habitat_qc_report.csv"
        if qc_csv.exists():
            st.subheader(t("qc_result"))
            qcdf = pd.read_csv(qc_csv)
            st.dataframe(qcdf, width="stretch", hide_index=True)
            st.download_button(t("download_qc"), qc_csv.read_bytes(),
                               file_name=qc_csv.name, mime="text/csv")
        population_csv = out / "habitat_population_occupancy.csv"
        if population_csv.exists():
            st.subheader("队列级生境普遍性 / Habitat prevalence")
            population_df = pd.read_csv(population_csv)
            st.dataframe(population_df, width="stretch", hide_index=True)
            if "Dominant_by_cohort_occupancy_screen" in population_df.columns:
                dominant = population_df.loc[
                    population_df["Dominant_by_cohort_occupancy_screen"].astype(str).str.lower().isin(["true", "1"]),
                    "Habitat",
                ].astype(str).tolist()
                st.info(
                    f"按当前配置的中位占比和缺失率筛查，队列中较稳定的主要生境为：{', '.join(dominant) if dominant else '暂无'}。"
                    "这是占比描述性筛查，不会重命名、合并或删除生境，也不是生物学类别数的证明。"
                )
            st.download_button(
                "下载队列生境普遍性 CSV", population_csv.read_bytes(),
                file_name=population_csv.name, mime="text/csv"
            )
        effective_csv = out / "habitat_patient_effective_count.csv"
        if effective_csv.exists():
            with st.expander("每例达到占比阈值的生境数量"):
                effective_df = pd.read_csv(effective_csv)
                st.dataframe(effective_df, width="stretch", hide_index=True)
                st.download_button(
                    "下载每例有效生境数量 CSV", effective_csv.read_bytes(),
                    file_name=effective_csv.name, mime="text/csv"
                )

        comparison_csv = out / "habitat_k_comparison.csv"
        if comparison_csv.exists():
            st.subheader("候选 K 比较 / Candidate K comparison")
            comparison_df = pd.read_csv(comparison_csv)
            st.dataframe(comparison_df, width="stretch", hide_index=True)
            st.download_button(
                "下载候选 K 比较报告 CSV", comparison_csv.read_bytes(),
                file_name=comparison_csv.name, mime="text/csv"
            )
            st.warning(
                "候选 K 比较是外层敏感性/QC 筛查：每个 K 均独立运行 HABIT。"
                "代理特征不是 HABIT 内部特征；最小可行 K 规则是简约筛查，不替代重复种子/扰动稳定性或临床/生物学验证。"
            )
            k_plot = out / "figures" / "candidate_k_comparison.png"
            if k_plot.exists():
                st.image(str(k_plot), width="stretch")
            k_diag_plot = out / "figures" / "candidate_k_diagnostics.png"
            if k_diag_plot.exists():
                st.markdown("**各候选 K 的分项诊断**")
                st.image(str(k_diag_plot), width="stretch")
            for ext in ("pdf",):
                k_plot_pdf = out / "figures" / f"candidate_k_comparison.{ext}"
                if k_plot_pdf.exists():
                    st.download_button(
                        "下载候选 K 比较图 PDF", k_plot_pdf.read_bytes(),
                        file_name=k_plot_pdf.name, mime="application/pdf"
                    )
                k_diag_pdf = out / "figures" / f"candidate_k_diagnostics.{ext}"
                if k_diag_pdf.exists():
                    st.download_button(
                        "下载候选 K 分项诊断 PDF", k_diag_pdf.read_bytes(),
                        file_name=k_diag_pdf.name, mime="application/pdf"
                    )
        k_csv = out / "habitat_k_assessment.csv"
        if k_csv.exists():
            st.subheader(t("k_assessment"))
            kdf = pd.read_csv(k_csv)
            st.dataframe(kdf, width="stretch", hide_index=True)
            st.download_button(t("download_k"), k_csv.read_bytes(),
                               file_name=k_csv.name, mime="text/csv")
            first = kdf.iloc[0].to_dict() if not kdf.empty else {}
            k_status = str(first.get("Status", ""))
            readiness = str(first.get("Radiomics_Readiness", ""))
            readiness_text = str(first.get("Radiomics_Readiness_Interpretation", ""))
            if readiness.startswith("NOT_READY"):
                st.error(readiness_text or t("k_review"))
            elif readiness == "CONDITIONAL_REVIEW_REQUIRED":
                st.warning(readiness_text or t("k_review"))
            elif readiness == "OCCUPANCY_SCREEN_PASS_PENDING_VALIDATION":
                st.info(readiness_text or t("k_pass"))
            elif k_status in ("REVIEW_K_MAY_BE_OVERSEGMENTED", "REVIEW_OCCUPANCY"):
                st.warning(t("k_review"))
            elif k_status == "OCCUPANCY_SCREEN_PASS_NOT_VALIDATION":
                st.info(t("k_pass"))
            st.caption(t("k_warning"))

        st.subheader(t("fig_gallery"))
        figure_files = [
            "cohort_habitat_composition.png",
            "habitat_fraction_distribution.png",
            "habitat_occupancy_heatmap.png",
            "cohort_habitat_occupancy_qc.png",
            "cohort_habitat_spatial_contiguity.png",
            "habitat_population_prevalence.png",
        ]
        existing_figures = [out / "figures" / name for name in figure_files if (out / "figures" / name).exists()]
        if existing_figures:
            for fig_path in existing_figures:
                st.markdown(f"**{fig_path.stem.replace('_', ' ')}**")
                st.image(str(fig_path), width="stretch")
                pdf_path = fig_path.with_suffix(".pdf")
                if pdf_path.exists():
                    st.download_button(
                        f"Download {pdf_path.name}" if lang == "en" else
                        (f"下载 {pdf_path.name}" if lang == "zh" else f"{pdf_path.name} をダウンロード"),
                        pdf_path.read_bytes(), file_name=pdf_path.name, mime="application/pdf",
                        key=f"dl_{pdf_path.name}",
                    )
        else:
            st.caption("Enable cohort figures and rerun the analysis." if lang == "en" else
                       ("请勾选队列统计图并重新运行分析。" if lang == "zh" else
                        "コホート統計図を有効にして再実行してください。"))

        # New interactive viewer for the full-FOV and lesion-level small-FOV figures.
        small_fov_dir = out / "figures" / "lesion_level"
        small_fov_files = sorted(small_fov_dir.glob("*_habitat_color_small_fov.png")) if small_fov_dir.exists() else []
        if small_fov_files:
            st.subheader("病例级彩色生境图：全 FOV 与病变小 FOV")
            st.caption(
                "小 FOV 只是围绕病灶在代表性切片上裁剪的显示图，不会修改 NIfTI 或生境标签。"
                "所有病例的 PNG/PDF 均保存在 figures/lesion_level/。"
            )
            preview_ids = [fp.name[:-len("_habitat_color_small_fov.png")] for fp in small_fov_files]
            preview_limit = 6
            for preview_id in preview_ids[:preview_limit]:
                st.markdown(f"**病例 {preview_id}**")
                full_png = out / "figures" / f"{preview_id}_habitat_color.png"
                small_png = small_fov_dir / f"{preview_id}_habitat_color_small_fov.png"
                left_view, right_view = st.columns(2)
                with left_view:
                    st.markdown("全 FOV")
                    if full_png.exists():
                        st.image(str(full_png), width="stretch")
                        full_pdf = full_png.with_suffix(".pdf")
                        if full_pdf.exists():
                            st.download_button("下载全 FOV PDF", full_pdf.read_bytes(),
                                               file_name=full_pdf.name, mime="application/pdf",
                                               key=f"full_fov_pdf_{preview_id}")
                with right_view:
                    st.markdown("病变级小 FOV")
                    st.image(str(small_png), width="stretch")
                    small_pdf = small_png.with_suffix(".pdf")
                    if small_pdf.exists():
                        st.download_button("下载小 FOV PDF", small_pdf.read_bytes(),
                                           file_name=small_pdf.name, mime="application/pdf",
                                           key=f"small_fov_pdf_{preview_id}")
            if len(preview_ids) > preview_limit:
                st.caption(f"当前页面预览前 {preview_limit} 例；其余 {len(preview_ids) - preview_limit} 例的图像均已保存到输出目录。")

        st.info(t("no_rad"))
        model_path = out / "habitat_model.habitatmodel"
        if model_path.exists():
            st.success("已保存可复用的 HABIT 共享生境模型（habitat_model.habitatmodel）。")
            st.download_button(
                "下载 HABIT 共享模型 / Download fitted HABIT model",
                model_path.read_bytes(), file_name=model_path.name,
                mime="application/octet-stream",
            )
        spec_json = out / "habit_study_spec.json"
        if spec_json.exists():
            st.download_button(
                "下载 HABIT 完整阶段 Spec JSON", spec_json.read_bytes(),
                file_name=spec_json.name, mime="application/json",
            )
        methods_txt = out / "habit_study_methods.txt"
        if methods_txt.exists():
            with st.expander("HABIT 自动生成的方法学描述"):
                methods = methods_txt.read_text(encoding="utf-8")
                st.write(methods)
                st.download_button(
                    "下载 HABIT methods 描述", methods_txt.read_bytes(),
                    file_name=methods_txt.name, mime="text/plain",
                )
        manifest = out / "run_manifest.json"
        if manifest.exists():
            st.download_button(t("manifest"), manifest.read_bytes(),
                               file_name=manifest.name, mime="application/json")
    except Exception as exc:
        st.exception(exc)
        st.error(t("error"))
