# Solaris's voice — Kokoro-82M ONNX, German single speaker "Martin".
#
# Model + voice pack + the German text-normalization service are from
# https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin
# (Apache-2.0); main.py is vendored with one patch: the ONNX execution
# provider is env-selectable (KOKORO_ONNX_PROVIDER=cuda) so the same image
# serves the GPU path (CDI .container Quadlet, see the voice template) and
# falls back to CPU. The cudnn runtime base is what onnxruntime-gpu needs;
# on a CPU-only box the CUDA provider simply reports unavailable.
FROM docker.io/nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip espeak-ng libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# kokoro-onnx pulls the CPU `onnxruntime` as a dependency; both packages
# claim the same module and the CPU one wins the import, silently dropping
# the CUDA provider (box-observed). Install both, then remove the CPU wheel
# so `import onnxruntime` resolves to the GPU build.
RUN pip3 install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    numpy \
    kokoro-onnx==0.5.0 \
    onnxruntime-gpu \
    && pip3 uninstall -y onnxruntime \
    && pip3 install --no-cache-dir --force-reinstall --no-deps onnxruntime-gpu

# Bake the model + voice pack into the image: reproducible, no HF dependency
# at boot (the upstream repo already went 401-private once — solarisbay#337-class
# lesson; we never want the box's voice to depend on a third-party repo).
ARG HF_BASE=https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin/resolve/main
RUN curl -fsSL -o /app/kokoro-martin.onnx $HF_BASE/kokoro-martin.onnx \
    && curl -fsSL -o /app/voices-martin.npz $HF_BASE/voices-martin.npz

COPY main.py tts_normalizer.py german_text_rules.py /app/

EXPOSE 8881
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8881"]
