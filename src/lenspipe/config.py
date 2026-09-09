"""Project configuration: one ``lenspipe.toml`` per project, validated by pydantic.

Every tunable that used to be a module constant or a literal inside a DifMAP
command string lives here. The same schema drives CLI overrides, the console
forms, and the ``config`` block written into every product's metadata.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CONFIG_FILENAME",
    "CasaConfig",
    "DifmapConfig",
    "EmceeConfig",
    "FinalIfSelfcal",
    "LenspipeConfig",
    "ProjectConfig",
    "RunConfig",
    "SelfcalStep",
    "Stage1Config",
    "Stage2Config",
    "Stage3Config",
    "default_toml",
    "load_config",
    "render_toml",
]

CONFIG_FILENAME = "lenspipe.toml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DifmapConfig(_Model):
    executable: str = Field("difmap", description="DifMAP executable name or path.")
    stream: Literal["pipe", "pty"] = Field(
        "pipe",
        description=(
            "How DifMAP output is captured. 'pipe' matches the legacy scripts; "
            "'pty' makes DifMAP line-buffer so progress updates arrive per fit."
        ),
    )


class ProjectConfig(_Model):
    source: str | None = Field(
        None, description="Source name; informational, filenames are authoritative."
    )
    difmap: DifmapConfig = Field(default_factory=DifmapConfig)


class SelfcalStep(_Model):
    """One ``selfcal`` call followed by ``modelfit``.

    DifMAP's ``selfcal doamp, dofloat, solint`` arguments map to
    ``amplitude``, ``float_amplitude`` and ``solint_min``.
    """

    amplitude: bool = False
    float_amplitude: bool = False
    solint_min: float = Field(3600.0, gt=0)
    modelfit_iterations: int = Field(50, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _accept_list_form(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) != 4:
                raise ValueError(
                    "selfcal step list form is [amplitude, float_amplitude, solint_min, modelfit_iterations]"
                )
            amplitude, float_amplitude, solint, iterations = value
            return {
                "amplitude": amplitude,
                "float_amplitude": float_amplitude,
                "solint_min": solint,
                "modelfit_iterations": iterations,
            }
        return value


class FinalIfSelfcal(_Model):
    enabled: bool = False
    if_count: int = Field(48, ge=1)
    channels_per_if: int = Field(64, ge=1)
    edge_channels: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _check_edges(self) -> FinalIfSelfcal:
        if 2 * self.edge_channels >= self.channels_per_if:
            raise ValueError("Twice edge_channels must be smaller than channels_per_if.")
        return self


def _legacy_selfcal_schedule() -> list[SelfcalStep]:
    return [
        SelfcalStep(amplitude=False, float_amplitude=False, solint_min=3600, modelfit_iterations=50),
        SelfcalStep(amplitude=False, float_amplitude=False, solint_min=1, modelfit_iterations=50),
        SelfcalStep(amplitude=False, float_amplitude=False, solint_min=0.5, modelfit_iterations=50),
        SelfcalStep(amplitude=True, float_amplitude=True, solint_min=3600, modelfit_iterations=50),
        SelfcalStep(amplitude=False, float_amplitude=False, solint_min=0.5, modelfit_iterations=50),
    ]


class Stage1Config(_Model):
    map_pixels: int = Field(1024, ge=64)
    cell_mas: float | None = Field(None, description="Cell size in mas; None lets DifMAP choose.")
    weighting: str = Field("uvw 0,-1,false", description="DifMAP uvw command (natural weighting).")
    unflag: bool = Field(True, description="Run 'unflag *' after observe.")
    selfcal: list[SelfcalStep] = Field(default_factory=_legacy_selfcal_schedule)
    final_if_selfcal: FinalIfSelfcal = Field(default_factory=FinalIfSelfcal)
    input_pattern: str = "*.uvfits"

    @field_validator("selfcal")
    @classmethod
    def _at_least_one(cls, value: list[SelfcalStep]) -> list[SelfcalStep]:
        if not value:
            raise ValueError("stage1.selfcal must contain at least one step")
        return value


class Stage2Config(_Model):
    mode: Literal["channel", "if"] = "if"
    channels_per_if: int = Field(64, ge=1)
    exclude_edge_channels: int = Field(0, ge=0)
    channels: str | None = Field(
        None, description="Channel-mode selection such as '1-10,15'; None means all."
    )
    map_pixels: int = Field(1024, ge=64)
    cell_mas: float | None = Field(25.0, description="Cell size in mas; None lets DifMAP choose.")
    weighting: str = "uvw 0,-1,false"
    modelfit_iterations: int = Field(20, ge=1)
    shards: int | Literal["auto"] = Field(
        "auto",
        description="DifMAP processes per epoch; 'auto' uses cores minus one, at most 8.",
    )
    keep_models: bool = Field(False, description="Retain every fitted model under models_<mode>/.")
    input_pattern: str = "*.cal.uvf"
    plot_spectrum: bool = Field(True, description="Write the quick-look PNG plots after each epoch.")
    plot_error_bars: bool = Field(
        True, description="Draw residual-RMS error bars on the quick-look plots."
    )
    memory_fraction: float = Field(
        0.5,
        gt=0,
        le=1,
        description="Fraction of physical RAM that DifMAP processes may use in total.",
    )
    memory_multiple: float = Field(
        3.0,
        gt=0,
        description=(
            "Estimated DifMAP memory per loaded dataset, as a multiple of the UV-FITS file size. "
            "SHORTCUT: unmeasured default; tune it after watching a real epoch."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> Stage2Config:
        if self.mode == "if" and 2 * self.exclude_edge_channels >= self.channels_per_if:
            raise ValueError("Twice exclude_edge_channels must be smaller than channels_per_if.")
        if self.mode == "if" and self.channels is not None:
            raise ValueError("stage2.channels is only valid with mode = 'channel'.")
        if isinstance(self.shards, int) and self.shards < 1:
            raise ValueError("stage2.shards must be >= 1 or 'auto'.")
        return self


class EmceeConfig(_Model):
    walkers: int = Field(32, ge=8)
    steps: int = Field(4000, ge=10)
    burn_in: int = Field(1000, ge=0)
    thin: int = Field(10, ge=1)
    seed: int = 12345
    alpha_prior_min: float = -3.0
    alpha_prior_max: float = 2.0

    @model_validator(mode="after")
    def _check(self) -> EmceeConfig:
        if self.walkers % 2:
            raise ValueError("emcee.walkers must be even")
        if self.burn_in >= self.steps:
            raise ValueError("emcee.burn_in must be smaller than emcee.steps")
        if self.alpha_prior_min >= self.alpha_prior_max:
            raise ValueError("emcee.alpha_prior_min must be smaller than alpha_prior_max")
        return self


class Stage3Config(_Model):
    reference_frequency_ghz: float = Field(15.0, gt=0)
    error_source: Literal["rms", "difmap"] = "rms"
    fit_method: Literal["least_squares", "emcee"] = "least_squares"
    emcee: EmceeConfig = Field(default_factory=EmceeConfig)
    frequency_frame_ghz: tuple[float, float] = (11.7, 18.3)
    frequency_ticks_ghz: list[float] = Field(
        default_factory=lambda: [12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0]
    )
    rcusp_images: list[str] = Field(
        default_factory=lambda: ["A1", "A2", "B"],
        description="Group names (A1, A2, B) for R_cusp; empty list disables it.",
    )
    annotations: bool = Field(True, description="Also write annotated spectrum figures.")
    plot_error_bars: bool = Field(
        True, description="Draw error bars on every Stage 3 figure; off plots the points alone."
    )
    figure_formats: list[Literal["pdf", "png", "svg"]] = Field(
        default_factory=lambda: ["pdf", "png"],
        description="Formats written for every Stage 3 figure; png alone roughly halves plot time.",
    )
    use_tex: bool = False
    exclude_channels: str | None = Field(
        None, description="Fit indices excluded from every epoch, e.g. '1-4,61-64'."
    )
    exclude_epoch_channels: list[str] = Field(
        default_factory=list, description="Per-epoch exclusions, e.g. ['E:897-960']."
    )

    @field_validator("figure_formats")
    @classmethod
    def _formats(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("stage3.figure_formats must name at least one format")
        return list(dict.fromkeys(value))

    @field_validator("rcusp_images")
    @classmethod
    def _rcusp_shape(cls, value: list[str]) -> list[str]:
        if value and len(value) != 3:
            raise ValueError("stage3.rcusp_images must be empty or exactly three group names")
        return value

    @field_validator("frequency_frame_ghz")
    @classmethod
    def _frame(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value[0] >= value[1]:
            raise ValueError("frequency_frame_ghz must be (low, high)")
        return value


class RunConfig(_Model):
    epoch_workers: int = Field(2, ge=1, description="Epochs processed concurrently.")
    plot_workers: int = Field(4, ge=1, description="Stage 3 per-visit plot processes.")


class CasaConfig(_Model):
    interpreter: str | None = Field(
        None,
        description=(
            "Command that runs CASA scripts, e.g. 'casa' or a python with casatasks. "
            "None disables calibration jobs in the console."
        ),
    )
    script: str | None = Field(None, description="Path to casa/calibration.py.")
    observation: str | None = Field(None, description="Observation TOML for the CASA script.")


class LenspipeConfig(_Model):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    stage1: Stage1Config = Field(default_factory=Stage1Config)
    stage2: Stage2Config = Field(default_factory=Stage2Config)
    stage3: Stage3Config = Field(default_factory=Stage3Config)
    run: RunConfig = Field(default_factory=RunConfig)
    casa: CasaConfig = Field(default_factory=CasaConfig)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def with_overrides(self, **sections: dict[str, Any]) -> LenspipeConfig:
        """Return a copy with per-section field overrides, e.g. ``stage2={"shards": 4}``."""
        data = self.model_dump()
        for section, overrides in sections.items():
            if section not in data:
                raise KeyError(f"Unknown config section: {section}")
            data[section].update({k: v for k, v in overrides.items() if v is not None})
        return LenspipeConfig.model_validate(data)


def load_config(project_root: Path, path: Path | None = None) -> LenspipeConfig:
    """Load ``<project>/lenspipe.toml``; defaults apply when the file is absent."""
    config_path = path if path is not None else project_root / CONFIG_FILENAME
    if not config_path.is_file():
        return LenspipeConfig()
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    return LenspipeConfig.model_validate(payload)


def default_toml() -> str:
    """Render the default configuration as an annotated TOML document."""
    return DEFAULT_TOML


def render_toml(
    *,
    source: str | None = None,
    difmap_executable: str | None = None,
    casa_interpreter: str | None = None,
    casa_script: str | None = None,
    casa_observation: str | None = None,
) -> str:
    """The annotated default file with detected values filled in, comments preserved."""
    text = DEFAULT_TOML
    if source:
        text = text.replace('# source = "MG0414"', f'source = "{source}"')
    if difmap_executable and difmap_executable != "difmap":
        text = text.replace('executable = "difmap"', f'executable = "{difmap_executable}"')
    if casa_interpreter:
        text = text.replace('# interpreter = "casa"', f'interpreter = "{casa_interpreter}"')
    if casa_script:
        text = text.replace('# script = "casa/calibration.py"', f'script = "{casa_script}"')
    if casa_observation:
        text = text.replace(
            '# observation = "casa/observation.22A-388.1555.A.toml"',
            f'observation = "{casa_observation}"',
        )
    LenspipeConfig.model_validate(tomllib.loads(text))  # never write a file that will not load
    return text


DEFAULT_TOML = """\
# lenspipe project configuration
# Every value here is the default; delete a line to keep the default, edit it to change it.
# Command-line flags override this file for a single run, and the effective values are
# recorded in each product's metadata.

[project]
# source = "MG0414"                 # informational; filenames <source>.<epoch>.* are authoritative

[project.difmap]
executable = "difmap"
stream = "pipe"                     # "pipe" (legacy behaviour) or "pty" (live per-fit progress)

[stage1]
map_pixels = 1024
# cell_mas = 25.0                   # unset: DifMAP chooses the cell size
weighting = "uvw 0,-1,false"
unflag = true
input_pattern = "*.uvfits"
# Self-calibration schedule. Each entry is one selfcal followed by modelfit:
#   [amplitude, float_amplitude, solint_min, modelfit_iterations]
selfcal = [
  [false, false, 3600, 50],
  [false, false, 1,    50],
  [false, false, 0.5,  50],
  [true,  true,  3600, 50],
  [false, false, 0.5,  50],
]

[stage1.final_if_selfcal]
enabled = false
if_count = 48
channels_per_if = 64
edge_channels = 0

[stage2]
mode = "if"                         # "channel" or "if"
channels_per_if = 64
exclude_edge_channels = 0
# channels = "1-10,15"              # channel mode only
map_pixels = 1024
cell_mas = 25.0
weighting = "uvw 0,-1,false"
modelfit_iterations = 20
shards = "auto"                     # DifMAP processes per epoch; "auto" = cores - 1 (max 8),
                                    # further capped so shards x epoch_workers fit in memory
keep_models = false
input_pattern = "*.cal.uvf"
plot_spectrum = true                # quick-look PNGs after each epoch
plot_error_bars = true              # residual-RMS error bars on those plots
memory_fraction = 0.5               # share of physical RAM DifMAP processes may use in total
memory_multiple = 3.0               # estimated DifMAP RSS per dataset as a multiple of the UV-FITS size

[stage3]
reference_frequency_ghz = 15.0
error_source = "rms"                # "rms" or "difmap"
fit_method = "least_squares"        # "least_squares" or "emcee"
frequency_frame_ghz = [11.7, 18.3]
frequency_ticks_ghz = [12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0]
rcusp_images = ["A1", "A2", "B"]    # [] disables R_cusp
annotations = true
plot_error_bars = true              # error bars on every Stage 3 figure
figure_formats = ["pdf", "png"]     # ["png"] for quick iteration runs
use_tex = false
# exclude_channels = "1-4,61-64"
# exclude_epoch_channels = ["E:897-960"]

[stage3.emcee]
walkers = 32
steps = 4000
burn_in = 1000
thin = 10
seed = 12345
alpha_prior_min = -3.0
alpha_prior_max = 2.0

[run]
epoch_workers = 2
plot_workers = 4

[casa]
# interpreter = "casa"              # or a python with casatasks; unset hides calibration in the console
# script = "casa/calibration.py"
# observation = "casa/observation.22A-388.1555.A.toml"
"""
