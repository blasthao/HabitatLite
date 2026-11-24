# 多算法医学影像生境分析与可视化软件 V1.0

**Multi-Algorithm Medical Imaging Habitat Analysis and Visualization Software**  
项目简称：**HabitatLite**  
当前界面版本标识：`0.10.2`

HabitatLite 是一款面向医学影像科研的数据处理与生境分析工具。软件读取用户提供的 NIfTI 影像及对应 ROI Mask，在队列层面建立共享生境聚类模型，将聚类标签映射回各病例影像空间，并提供生境标签图、体积/占比统计、质量控制报告和可视化结果。

> 本项目用于医学影像研究与方法学探索，不是临床诊断软件。软件输出的 Habitat 标签是基于输入影像特征得到的聚类类别，不等同于病理组织学类型，也不能独立用于诊断或治疗决策。

[English summary](#english-summary) · [功能介绍](#主要功能) · [安装运行](#安装与运行) · [使用流程](#基本使用流程) · [输出文件](#主要输出文件) · [注意事项](#重要说明)

---

## 项目简介

生境分析（Habitat Analysis）通过对预先定义的感兴趣区域（Region of Interest，ROI）内体素或超体素的影像特征进行分组，描述 ROI 内部的影像异质性。HabitatLite 将全队列共享聚类、病例级标签回填和结果质量检查整合到一个交互式工作流程中。

软件目前提供两种队列级生境构建设计：

1. **Voxel-wise pooled K-means**：将全队列病例 ROI 内的体素特征池化后，拟合统一的 K-means 模型，再将类别标签分配回每个病例。该模式不执行超体素预分割，适用于直接的体素级强度或多模态特征分区。
2. **Two-step HABIT**：先在每例 ROI 内进行超体素划分，再汇总超体素特征并进行队列级共享聚类，最后将超体素生境标签映射回原始影像空间。第一阶段支持 SLIC、K-means 和 GMM 超体素划分方式。

软件以 **HABIT 2.0.0** 作为生境分析引擎，提供 Fast Clustering 和 Full Habitat Analysis 两种运行级别。

## 主要功能

- **NIfTI 数据读取与病例配对**：支持 2D/3D NIfTI 影像和离散整数标签 Mask；按病例文件名匹配影像与 Mask，并显示缺失病例信息。
- **ROI 选择**：可选择 Mask 中的单个正整数 Label，或将全部非零标签合并为一个 ROI。软件不会自动判断 Label 对应的组织类型。
- **多模态输入**：支持 CT、MRI 主模态，并可添加已完成空间配准的 ADC、T2、DWI 等附加模态目录。
- **两种队列级聚类设计**：Voxel-wise pooled K-means 与 Two-step HABIT。
- **超体素算法**：Two-step 模式提供 SLIC、K-means 和 Gaussian Mixture Model（GMM）。
- **K 值设置**：支持手动固定 K，或在候选范围内调用 HABIT 原生无监督验证准则自动选择 K。
- **参数预处理**：支持 HABIT 特征级预处理，以及 ROI 百分位极值截断、强度标准化和 ROI 边缘腐蚀等影像级设置。
- **Fast / Full 分析级别**：快速模式聚焦共享模型、标签图、体积/占比矩阵和基础 QC；完整模式进一步执行 HABIT 原生量化及更完整的 QC 和图表输出。
- **可视化**：提供 K-means 特征空间散点图、PCA 生境投影图、生境占比及队列统计图，并可在完整模式中生成病例级彩色生境叠加图和病变局部视野图。
- **结果记录**：保存共享模型、HABIT 阶段 Spec、方法学描述、运行参数、环境信息及可观测的阶段耗时。
- **交互与日志**：Streamlit 本地交互界面，支持中文、English 和日本語；显示运行进度并尽量同步 HABIT 日志。

## 软件界面与结果示例

建议在 GitHub 仓库中建立 `docs/images/` 目录，并将实际运行截图放入其中。以下为推荐的 README 图片位置；请用你自己的软件截图替换对应图片文件。

| 建议图片 | 内容 | 推荐文件名 |
|---|---|---|
| 主界面 | 侧边栏参数区、病例配对表和主工作区 | `docs/images/main-interface.png` |
| 聚类参数 | 模式、Fast/Full、K 值和预处理选项 | `docs/images/clustering-settings.png` |
| 运行过程 | 总体进度条和 HABIT 实时日志 | `docs/images/live-progress.png` |
| 生境结果 | 原始 CT 与生境标签图叠加 | `docs/images/habitat-overlay.png` |
| PCA 图 | 论文风格 PCA 生境投影示例 | `docs/images/pca-habitat.png` |
| QC 结果 | 生境占比、病例覆盖率和 QC 报告 | `docs/images/qc-summary.png` |

例如，添加主界面截图后可使用：

```markdown
![HabitatLite 主界面](docs/images/main-interface.png)
```

---

## 聚类模式

### 1. Voxel-wise pooled K-means

将所有病例所选 ROI 内的体素特征整合为队列级数据，拟合一个共享 K-means 模型，再将模型类别映射回每例影像空间。

**特点**

- 以单个 ROI 体素作为聚类样本。
- 不需要逐例进行超体素预分割。
- 各病例使用同一个队列级聚类模型，便于在同一分析队列内比较标签分布。
- 不显式加入空间邻近约束，同一生境可能在 ROI 内呈分散或不连续分布。
- 总体数据量随 ROI 体素数量和模态数量增加，较大队列可能需要较多内存。

该模式是当前界面的默认聚类设计，适合快速开展体素级强度或多模态特征分区。

### 2. Two-step HABIT

Two-step 模式先在每例 ROI 内进行超体素分区，再提取超体素特征并在全队列范围内拟合共享生境模型。

第一阶段可选算法：

| 算法 | 主要特点 | 说明 |
|---|---|---|
| SLIC | 综合空间位置与影像特征，倾向形成空间紧凑的区域 | 需要安装 `scikit-image`；可调整 compactness |
| K-means | 按特征相似性进行分组 | 不保证超体素空间连通 |
| GMM | 使用高斯混合模型进行特征分组 | 可选择 full、tied、diag 或 spherical 协方差形式 |

超体素数、算法类型及相关参数会影响后续特征汇总和聚类结果，正式研究中应记录并保持实验方案一致。

### 3. Fast Clustering 与 Full Habitat Analysis

| 功能 | Fast Clustering | Full Habitat Analysis |
|---|:---:|:---:|
| 特征提取、队列池化、共享 K-means 与标签分配 | ✓ | ✓ |
| 病例级 NIfTI 生境图 | ✓ | ✓ |
| 生境体积/占比矩阵 | ✓ | ✓ |
| 基础几何、标签和占比 QC | ✓ | ✓ |
| 论文风格 PCA 可视化 | ✓ | ✓ |
| HABIT 原生 Volume 等量化 | — | ✓ |
| 三维连通成分空间 QC | — | ✓ |
| 代理特征空间聚类评价指标 | — | ✓ |
| 病例彩色叠加图、局部小 FOV 和队列统计图 | 默认关闭 | 可按设置开启 |
| HABIT 中间单元表及较完整中间文件 | 默认不保留 | 可按设置保留 |

Fast 模式仍然执行模型拟合和标签分配，并非只生成预览图。它主要减少后续量化、空间检查和图表输出。Full 模式适用于需要更完整 HABIT 量化和研究级结果整理的任务。

---

## 安装与运行

### 运行环境

当前项目的开发和测试基线如下，建议使用 Windows 10/11 64 位及 Python 3.10 环境：

| 组件 | 参考版本 |
|---|---|
| Python | 3.10.x（开发环境为 3.10.20） |
| habitat-analysis | 2.0.0 |
| SimpleITK | 2.5.6 |
| NumPy | 2.2.6 |
| SciPy | 1.15.3 |
| Pandas | 2.3.3 |
| scikit-learn | 1.7.2 |
| Streamlit | 1.61.1 |
| Matplotlib | 3.10.9 |

以上是已记录的开发环境参考版本，并不意味着任意版本组合都经过验证。请优先使用仓库中的依赖清单或项目发布包所附环境配置。

### 获取源代码

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd <YOUR_REPOSITORY_FOLDER>
```

将 `<YOUR_GITHUB_REPOSITORY_URL>` 和 `<YOUR_REPOSITORY_FOLDER>` 替换为实际仓库地址和目录名。

### 创建 Python 环境

使用 Conda 的示例：

```bash
conda create -n habitatlite python=3.10 -y
conda activate habitatlite
```

安装 HABIT 和主要运行依赖：

```bash
python -m pip install "habitat-analysis==2.0.0"
python -m pip install \
  "SimpleITK==2.5.6" \
  "numpy==2.2.6" \
  "scipy==1.15.3" \
  "pandas==2.3.3" \
  "scikit-learn==1.7.2" \
  "streamlit==1.61.1" \
  "matplotlib==3.10.9"
```

如需使用 Two-step 模式中的 SLIC 算法，另行安装：

```bash
python -m pip install scikit-image
```

安装完成后可检查关键依赖：

```bash
python -c "import habit; print('HABIT:', habit.__version__)"
python -c "import SimpleITK, numpy, pandas, scipy, sklearn, streamlit; print('Core dependencies OK')"
```

### 启动软件

确认项目中包含 `app.py`、`habitatlite/core.py` 和 `habitatlite/__init__.py` 后，在项目根目录执行：

```bash
python -m streamlit run app.py
```

程序将在本机启动 Streamlit 服务，并在浏览器中打开交互界面。若未自动打开浏览器，请查看终端显示的本地访问地址和错误信息。

---

## 输入数据准备

### 目录结构

默认目录结构如下：

```text
HabitatLite/
├── app.py
├── habitatlite/
│   ├── __init__.py
│   └── core.py
├── images/
│   ├── P001.nii.gz
│   ├── P002.nii.gz
│   └── ...
├── masks/
│   ├── P001.nii.gz
│   ├── P002.nii.gz
│   └── ...
└── habitat_results/
```

也可以在侧边栏中手动指定影像、Mask 和结果输出目录。

### 影像与 Mask 要求

- 输入影像和 Mask 使用 NIfTI 格式，常见扩展名为 `.nii` 或 `.nii.gz`。
- 每例影像应有一份对应 Mask；默认按病例文件名配对，例如 `P001.nii.gz` 对应 `P001.nii.gz`。
- Mask 应为离散整数标签图，通常背景为 `0`，ROI 使用一个或多个正整数标签表示。
- 运行前应确认所选 Label 的实际含义。选择“所有非零标签”会把全部非零体素合并为同一个 ROI。
- 影像与 Mask 应保持一致的尺寸、方向、原点和体素间距，或符合软件所使用的空间对齐要求。建议在独立影像查看器中抽查叠加结果。
- 软件负责读取用户提供的 ROI Mask，不负责自动勾画肿瘤、器官或其他解剖结构。

### 多模态数据

主模态可选择 CT 或 MRI。附加模态目录按“模态名=文件夹路径”逐行填写，例如：

```text
ADC=D:/study/ADC
T2=D:/study/T2
DWI=D:/study/DWI
```

附加模态必须事先完成必要的空间配准，并与主影像按病例 ID 对应。文件名配对成功不代表配准质量已经得到验证，仍需由使用者检查影像空间一致性。

---

## 基本使用流程

1. **准备数据**：将影像和 Mask 放入对应目录，核对病例 ID 和文件名。
2. **启动软件**：运行 `streamlit run app.py` 或启动已构建的 Windows 程序。
3. **设置输入路径**：指定 images、masks 和结果输出目录。
4. **检查配对结果**：确认匹配病例数、缺失影像和缺失 Mask 信息。
5. **选择 ROI**：在 Mask Label 下拉框中选择目标标签，或选择全部非零标签。
6. **选择聚类设计**：选择 Voxel-wise pooled K-means 或 Two-step HABIT。
7. **选择分析级别**：根据目的选择 Fast Clustering 或 Full Habitat Analysis。
8. **设置 K 值**：选择固定 K 或 HABIT 原生自动 K，并设置候选范围和验证准则。
9. **检查其他参数**：核对随机种子、预处理方式、超体素参数（如适用）和 QC 阈值。
10. **运行并检查结果**：观察进度条和实时日志，结束后检查 NIfTI 生境图、矩阵、QC 报告和 PCA 图片。

首次使用建议选取少量已核对病例进行试运行，再开展完整队列分析。

---

## K 值与预处理参数

### K 值设置

软件支持：

- **固定 K**：由用户指定生境类别数，适用于预设方案或复现实验。
- **HABIT 原生自动选择 K**：在给定候选范围内，使用选定的无监督验证准则选择 K。可选准则包括 Elbow/Kneedle、Silhouette、Calinski–Harabasz、Davies–Bouldin 和 Gap statistic。

无监督指标不等同于临床结局 AUC。当前版本不根据患者级临床结局自动执行 ROC/AUC 选 K；若研究需要按结局 AUC 选择 K，应另行准备患者级结局数据、定义预测模型和交叉验证/独立验证方案。

### 预处理

可配置的预处理包括：

- HABIT 特征级预处理：none、Winsorize、Z-score 或 Min-Max 组合。
- 影像级极值截断：按 ROI 内设定百分位处理极端强度。
- 强度标准化：none、Z-score 或 Min-Max。
- ROI 边缘腐蚀：按指定体素数缩小参与聚类的区域。
- 可选输入文件 SHA-256 记录。

预处理会改变进入模型的特征值或 ROI 范围。应根据研究方案选择参数，并在同一研究队列中保持一致。对于 CT HU 分析，若希望保留原始强度尺度，应确认未意外启用标准化或极值截断。

---

## 主要输出文件

输出目录中常见文件及文件夹如下（实际文件以所选 Fast/Full 模式和本次设置为准）：

| 文件或目录 | 内容 |
|---|---|
| `habitat_maps/` | 病例级 NIfTI 生境标签图；ROI 外通常为背景值 |
| `habitat_feature_matrix.csv` | 病例级生境体积、体素数或 ROI 占比等汇总信息 |
| `habitat_qc_report.csv` | 病例级标签数、区域大小、占比和 QC 状态 |
| `habitat_population_occupancy.csv` | 生境在队列中的出现率及占比统计 |
| `habitat_patient_effective_count.csv` | 每例达到设定占比阈值的生境数量 |
| `habitat_k_assessment.csv` | 所选 K 的占比筛查和可用性提示 |
| `habitat_k_comparison.csv` | 候选 K 比较结果（如本次运行生成） |
| `habitat_model.habitatmodel` | 本次队列拟合得到的共享 HABIT 模型 |
| `habit_study_spec.json` | HABIT 分析阶段及组件配置记录 |
| `habit_study_methods.txt` | HABIT 生成的方法学描述文本 |
| `run_manifest.json` | 软件版本、运行参数、环境信息和可观测阶段耗时等记录 |
| `figures/` | PCA、散点图及其他图表；具体内容取决于分析级别和输出设置 |

在 Full 模式并启用对应选项时，还可能生成病例级彩色叠加图、病变小 FOV 图片、队列统计图及更多 HABIT 中间文件。

### PCA 图的解释

对于 HU-only 聚类，K-means 的实际聚类输入只有一个特征，不能仅凭该单一特征得到真正的二维 PC1/PC2。软件生成的论文风格 PCA 图会在显示阶段额外计算局部影像描述量（体素强度、ROI 限定的 3×3×3 局部均值、局部标准差和局部梯度幅值），再进行标准化与 PCA 投影。图中颜色继承自已保存的 HABIT 生境标签；这些辅助描述量仅用于可视化，不参与原 K-means 拟合，也不会改变 NIfTI 标签。

因此，PCA 点云是否分离不能单独作为聚类正确性或生物学有效性的证据。图表只改变展示方式，不会重新拟合聚类模型。

---

## 质量控制与结果判读

QC Gate 用于提示病例级或队列级生境分布中的潜在问题，例如生境缺失、体素数过少或 ROI 占比偏低。常见状态包括：

- **PASS**：当前设置下的基础检查条件通过；仍应人工抽查影像叠加结果。
- **REVIEW**：至少一项检查提示需要关注，例如小生境占比偏低或病例内生境缺失。
- **FAIL**：存在阻断性或较严重异常，应先检查输入、空间几何、标签图和错误日志。

QC 阈值属于软件质量检查参数，不是适用于所有器官、病种和影像模态的统一医学标准。QC 不会自动删除、合并或重命名生境，也不能单独证明某个 K 值具有生物学意义。

Voxel-wise pooled K-means 不显式考虑空间邻近关系，因此同一生境可能在 ROI 内分散分布。Two-step + SLIC 可使初始超体素更紧凑，但最终共享生境仍不保证每例均出现所有类别或形成完全连续的区域。

---

## Windows EXE 打包

项目可通过 PyInstaller 构建 Windows `onedir` 程序。建议在已验证的 Windows Conda 环境中执行，并保留命令行窗口以便查看运行日志。

打包目录至少应包含：

```text
HabitatLite/
├── app.py
├── run_streamlit.py
├── build.py
├── build_requirements.txt
├── app.ico
└── habitatlite/
    ├── __init__.py
    └── core.py
```

确认 `app.ico` 是有效的 Windows ICO 文件，而不是仅将 PNG/JPG 文件改名为 `.ico`。

安装打包依赖并构建：

```bash
conda activate habitatlite
python -m pip install -r build_requirements.txt
python build.py --clean
```

默认采用 `onedir` 并保留命令行窗口。构建完成后，分发时应复制整个 `dist/HabitatLite/` 文件夹，不能只复制 EXE。若使用 Two-step + SLIC，打包环境还应安装 `scikit-image`，以便将其相关模块一并收集。

> PyInstaller 的实际收集结果与 Python 版本、依赖版本和平台环境有关。正式发布前，应在干净的 Windows 测试环境中验证启动、NIfTI 读取、聚类、结果写出和图表生成。

---

## 常见问题

| 现象 | 排查建议 |
|---|---|
| 扫描不到病例 | 核对 images/masks 路径、NIfTI 扩展名和病例文件名是否一致 |
| 找不到正整数 Mask Label | 检查 Mask 是否为空、全零或标签值不符合要求 |
| 多模态病例缺失 | 确认附加模态目录中每例都有对应文件，并检查配准结果 |
| SLIC 缺少 `scikit-image` | 在运行 Streamlit 的同一 Python 环境中安装 `scikit-image` 并重启 |
| HABIT 版本不匹配 | 优先使用 `habitat-analysis==2.0.0`，并通过环境诊断确认实际模块版本 |
| 分析过程中断 | 查看 Streamlit 实时日志和命令行完整 traceback，同时检查内存、磁盘空间和目录权限 |
| PCA 图生成失败 | 先确认 `habitat_maps/` 中标签图完整，再检查影像与标签图空间和数组尺寸是否一致 |
| PCA 中颜色重叠 | PCA 是显示投影；结合 HU/特征分布、病例级标签图和定量矩阵判断，不要人为移动点云 |
| 运行时间较长 | 先用固定 K 和 Fast 模式试运行；大 ROI、多模态、自动 K 搜索及 Full 后处理会增加计算量 |

---

## 数据安全与使用限制

- 使用者应遵守所在单位关于医学影像访问、科研使用、存储、备份和对外传输的管理制度。
- 软件不会自动完成患者去标识化。上传至 GitHub、提交 Issue 或分享日志前，应移除影像、Mask、截图及日志中的姓名、住院号、检查号、路径和其他可识别信息。
- 不建议将真实患者 NIfTI 影像、Mask 或包含敏感信息的运行记录提交到公开仓库。
- 生境标签为影像特征聚类结果，不直接代表组织病理类型，也不构成临床诊断意见。
- 分析结果受输入影像、ROI 分割质量、模态配准、预处理、K 值、随机种子和队列组成影响。不同队列或参数重新拟合后，Habitat 编号可能变化，不应仅按编号认定两个生境等价。
- 正式科研使用前，应完成输入核验、参数记录、重复性评估、聚类稳定性分析及必要的外部验证。

---

## 版本信息

- 软件名称：多算法医学影像生境分析与可视化软件
- 项目简称：HabitatLite
- 登记/说明书版本：V1.0（以正式发布版本为准）
- 当前上传源码中的界面版本标识：`0.10.2`
- HABIT 测试基线：`habitat-analysis==2.0.0`

软件登记版本号与内部开发版本标识用途不同。每次正式发布建议同时更新 Git tag、README、用户说明书及程序界面中的版本信息。

## License

本仓库许可证尚待项目维护者确定。公开仓库前，请确认自有代码的授权方式，并核对 HABIT 及其他第三方依赖的许可证和再分发要求。**不要在未确认前自行声明本仓库为 MIT、Apache-2.0 或其他开源许可证。**

## 引用与致谢

本软件使用 HABIT（`habitat-analysis`）作为生境分析引擎，并依赖 SimpleITK、NumPy、SciPy、Pandas、scikit-learn、Streamlit、Matplotlib 等开源项目。开展研究或发布衍生软件时，请按各项目的官方引用及许可证要求进行致谢和引用。

如需引用本软件，请在正式发布后补充作者、机构、版本、发布日期、仓库地址及 DOI（如已归档）：

```text
[作者]. 多算法医学影像生境分析与可视化软件（HabitatLite）. Version [版本号]. [年份]. [URL/DOI]
```

## English summary

HabitatLite is a research-oriented application for cohort-level medical imaging habitat analysis. It accepts NIfTI images and corresponding ROI masks, provides voxel-wise pooled K-means and two-step HABIT workflows, exports case-level habitat label maps and cohort-level summaries, and offers quality-control reports and visualization tools. It is built with Streamlit and uses HABIT 2.0.0 as its habitat-analysis engine.

The software is intended for research use. Habitat labels represent image-feature clusters and should not be interpreted as histopathological classes or standalone clinical diagnoses.
