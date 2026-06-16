#!/usr/bin/env python3
"""Reproducibly train the "Solaris" openWakeWord wake-word model.

This is the offline producer for the brand asset that `templates/solaris/
post-deploy.py` installs into the `voice` (wyoming-openwakeword) service and
wires as the default Assist wake word (#407; platform model-slot mechanism is
servicebay#1832).

It is deliberately NOT run by the code-builder / CI image jobs: the openWakeWord
training pipeline needs Piper, torch, ~6 GB of feature data and (for clip
generation) a GPU. Run it once on a GPU box, then commit the produced
`solaris.tflite` to the path the post-deploy reads. The post-deploy is fail-soft
when the file is absent, so shipping the wiring without the model is safe — the
box just keeps push-to-talk until the model lands.

This script writes the exact training config (`solaris.yaml`) and prints the
recipe below; the heavy lifting is openWakeWord's own `train.py`. The recipe is
what actually produced the shipped model on the Solaris GPU box (RTX 2000 Ada),
so it is reproducible rather than aspirational.

  RECIPE  (one GPU box, podman; ~30 min)
  --------------------------------------
  0. openWakeWord has NO one-call `train_model()` API: training is driven by its
     `openwakeword/train.py` CLI in three phases. The package pins
     speexdsp-ns (py<3.10) yet its latest release needs py>=3.10 — irreconcilable
     via pip, so we run openWakeWord from a *checkout* on PYTHONPATH (skip
     `pip install -e`) on Python 3.9.

  1. Deps image (Python 3.9). torch 1.13.1+cu117 for GPU clip generation, and:
       pip install onnxruntime "numpy==1.23.5" "protobuf==3.19.6" tensorflow-cpu==2.8.1
       apt-get install -y espeak-ng libespeak-ng1
       pip install espeak_phonemizer webrtcvad
     numpy<2 (torch 1.13 ABI) and protobuf<3.20 (tf 2.8) are load-bearing pins.

  2. Sources (in the work dir):
       git clone https://github.com/dscripka/openWakeWord            # train.py lives here
       git clone https://github.com/dscripka/piper-sample-generator  # the FORK — has generate_samples.py
       curl -L -o piper-sample-generator/models/en-us-libritts-high.pt \
         https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt

  3. Feature data (HF dataset davidscripka/openwakeword_features) into feats/:
       - openwakeword_features_ACAV100M_2000_hrs_16bit.npy  (17 GB full; a
         truncated-but-header-valid prefix of ~0.5M rows is plenty — fix the
         .npy header to the row count actually downloaded)
       - validation_set_features.npy                        (false-positive set)
     Plus the shared feature models, once:
       python -c "import openwakeword.utils as u; u.download_models()"

  4. Run the three phases (PYTHONPATH=<checkout>/openwakeword):
       T=openwakeword/openwakeword/train.py
       python $T --training_config solaris.yaml --generate_clips   # GPU (Piper TTS)
       CUDA_VISIBLE_DEVICES="" python $T --training_config solaris.yaml --augment_clips
       CUDA_VISIBLE_DEVICES="" python $T --training_config solaris.yaml --train_model
     augment+train run CPU-only: this box's GPU cuFFT (torch 1.13/cu117 vs the
     host driver) crashes torch_audiomentations' band-pass filter. The DNN is
     tiny, so CPU is fine. Give the DataLoader shared memory: `podman run
     --shm-size=8g ...` or it dies with a Bus error.

  5. Output: my_custom_model/solaris/solaris.tflite (+ .onnx). Ship it:
       - committed:   templates/solaris/wakeword/solaris.tflite  (SB transports
                      it to <DATA_DIR>/solaris/wakeword/solaris.tflite, which the
                      post-deploy reads), OR
       - operator:    drop it at <DATA_DIR>/solaris/wakeword/solaris.tflite on
                      the box and redeploy Solaris.
     The post-deploy copies it into the voice service's custom-model dir
     (OPENWAKEWORD_CUSTOM_DIR, default <DATA_DIR>/voice/custom) and sets the
     Assist pipeline's wake word to the `solaris` model.

  6. Tune false triggers HA-side (start ~0.5); raise n_samples / add RIR +
     background dirs to rir_paths/background_paths for more robustness.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

WAKE_PHRASE = "Solaris"
WAKE_WORD_ID = "solaris"


def _config(n_samples: int, n_samples_val: int, steps: int, work: str) -> dict:
    """The exact openWakeWord train.py config that produced the shipped model.

    Paths are relative to the training work dir (`work`). rir_paths /
    background_paths are empty here; populate them with MIT-RIR + ambient dirs
    for a more noise/reverb-robust model.
    """
    return {
        "model_name": WAKE_WORD_ID,
        "target_phrase": [WAKE_WORD_ID],
        "custom_negative_phrases": [],
        "n_samples": n_samples,
        "n_samples_val": n_samples_val,
        "tts_batch_size": 50,
        "augmentation_batch_size": 16,
        "augmentation_rounds": 1,
        "piper_sample_generator_path": f"{work}/piper-sample-generator",
        "output_dir": f"{work}/my_custom_model",
        "rir_paths": [],
        "background_paths": [],
        "background_paths_duplication_rate": [],
        "false_positive_validation_data_path": f"{work}/feats/validation_set_features.npy",
        "feature_data_files": {"ACAV100M_sample": f"{work}/feats/ACAV100M.npy"},
        "batch_n_per_class": {
            "ACAV100M_sample": 1024,
            "adversarial_negative": 50,
            "positive": 50,
        },
        "model_type": "dnn",
        "layer_size": 32,
        "steps": steps,
        "max_negative_weight": 1500,
        "target_false_positives_per_hour": 0.2,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("templates/solaris/wakeword/solaris.tflite"),
        help="where the produced .tflite is shipped (wake-word id is always 'solaris')",
    )
    ap.add_argument(
        "--work",
        default="/work",
        help="training work dir the config paths are rooted at (the box used /work)",
    )
    ap.add_argument(
        "--config-out",
        type=pathlib.Path,
        default=pathlib.Path("solaris.yaml"),
        help="write the openWakeWord train.py config here",
    )
    ap.add_argument("--positive-samples", type=int, default=2000)
    ap.add_argument("--positive-val-samples", type=int, default=500)
    ap.add_argument("--steps", type=int, default=10000)
    args = ap.parse_args(argv)

    cfg = _config(args.positive_samples, args.positive_val_samples, args.steps, args.work)
    args.config_out.write_text(yaml.safe_dump(cfg, sort_keys=False))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.write(
        f"Wrote openWakeWord training config -> {args.config_out}\n"
        f'Target wake word: "{WAKE_PHRASE}" (single word, no Hey/Ok prefix), id "{WAKE_WORD_ID}".\n\n'
        "Run the three phases against the upstream train.py (see the RECIPE in this\n"
        "file's docstring for the deps image + sources + feature data):\n\n"
        "  export PYTHONPATH=<checkout>/openwakeword\n"
        "  T=openwakeword/openwakeword/train.py\n"
        f"  python $T --training_config {args.config_out} --generate_clips\n"
        f'  CUDA_VISIBLE_DEVICES="" python $T --training_config {args.config_out} --augment_clips\n'
        f'  CUDA_VISIBLE_DEVICES="" python $T --training_config {args.config_out} --train_model\n\n'
        f"Then ship it:\n"
        f"  cp {args.work}/my_custom_model/{WAKE_WORD_ID}/{WAKE_WORD_ID}.tflite {args.out}\n"
        "  git add + commit   (post-deploy installs it + sets the Assist wake word).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
