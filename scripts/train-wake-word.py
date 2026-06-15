#!/usr/bin/env python3
"""Reproducibly train the "Solaris" openWakeWord wake-word model.

This is the offline producer for the brand asset that `templates/solaris/
post-deploy.py` installs into the `voice` (wyoming-openwakeword) service and
wires as the default Assist wake word (#407; platform model-slot mechanism is
servicebay#1832).

It is deliberately NOT run by the code-builder / CI image jobs: the
openWakeWord training pipeline needs Piper, torch and (realistically) a GPU.
Run it once on a workstation, then commit / drop the produced `solaris.tflite`
to the path the post-deploy reads (see RECIPE below). The post-deploy is
fail-soft when the file is absent, so shipping the wiring without the model is
safe — the box just keeps push-to-talk until the model lands.

  RECIPE
  ------
  1. Environment (Linux, Python 3.10+, ~4 GB free; GPU strongly recommended):

       python -m venv .venv && . .venv/bin/activate
       pip install openwakeword piper-tts

     openWakeWord's automatic training extras (it pulls torch + the
     audiomentations/data deps on first `train` import):

       pip install "openwakeword[train]"

  2. Generate + train (this script):

       python scripts/train-wake-word.py --out templates/solaris/wakeword/solaris.tflite

     It synthesises many Piper-TTS utterances of the single word "Solaris"
     (varied voices/speeds/pitch), mixes in negative/background audio, and
     runs the openWakeWord training pipeline to a `.tflite` model whose
     wake-word id is `solaris`.

  3. Tune against false triggers: lower `--threshold` raises recall (more
     wakes, more false positives); raise it for precision. Re-run with more
     `--negative-hours` if it triggers on ambient speech.

  4. Ship the model so the post-deploy installs it:
       - committed:   templates/solaris/wakeword/solaris.tflite  (SB transports
                      it to <DATA_DIR>/solaris/wakeword/solaris.tflite, which the
                      post-deploy reads), OR
       - operator:    drop it at <DATA_DIR>/solaris/wakeword/solaris.tflite on
                      the box and redeploy Solaris.
     The post-deploy copies it into the voice service's custom-model dir
     (OPENWAKEWORD_CUSTOM_DIR, default <DATA_DIR>/voice/custom) and sets the
     Assist pipeline's wake word to the `solaris` model.

The training call below targets the openWakeWord public training API. Pin the
exact upstream revision when you run it; the API has moved between releases.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

WAKE_PHRASE = "Solaris"
WAKE_WORD_ID = "solaris"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("templates/solaris/wakeword/solaris.tflite"),
        help="output .tflite model path (wake-word id is always 'solaris')",
    )
    ap.add_argument(
        "--positive-samples",
        type=int,
        default=2000,
        help="number of synthetic Piper utterances of the wake phrase",
    )
    ap.add_argument(
        "--negative-hours",
        type=float,
        default=10.0,
        help="hours of negative/background audio to mix in (false-trigger tuning)",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="detection threshold baked into the recipe notes (HA-side knob too)",
    )
    args = ap.parse_args(argv)

    try:
        import openwakeword.train  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "openWakeWord training deps not installed. This producer runs OFFLINE,\n"
            "not in CI / the code-builder. Install with:\n"
            '    pip install "openwakeword[train]" piper-tts\n'
            "then re-run. See the RECIPE in this file's docstring.\n"
        )
        return 2

    # The concrete training call. openWakeWord's `train_model` synthesises the
    # positive set from the phrase via Piper, mixes negatives, and emits a
    # .tflite. Kept as one call so the recipe is the source of truth; pin the
    # upstream revision when running (the API has changed across releases).
    from openwakeword.train import train_model  # type: ignore[import-not-found]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    train_model(
        target_phrase=[WAKE_PHRASE],
        model_name=WAKE_WORD_ID,
        n_samples=args.positive_samples,
        background_hours=args.negative_hours,
        output_path=str(args.out),
    )
    sys.stdout.write(
        f"wrote {args.out} (wake-word id '{WAKE_WORD_ID}', "
        f"threshold ~{args.threshold}). Install per the RECIPE.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
