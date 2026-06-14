# solaris-tts — Solaris's voice (Kokoro-Martin)

Kokoro-82M ONNX, German single speaker **Martin** — vendored from
[Godelaune/Kokoro-82M-ONNX-German-Martin](https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin)
(Apache-2.0) with the model + voice pack **baked into the image** (the
upstream repo went 401-private once; the box's voice must not depend on a
third-party download at boot).

One patch on the vendored `main.py`: the ONNX execution provider is
env-selectable — `KOKORO_ONNX_PROVIDER=cuda` runs the 82M model on the GPU
(box-measured RTX 2000 Ada: 0.29–0.36 s for a 7.4 s sentence, 0.03 s warm
for a short one, ~1.2 GiB VRAM — ~10× the 6-core CPU path).

Serves an OpenAI-compatible `POST /v1/audio/speech` on `:8881` with German
text normalization (dates, times, ordinals, Euro amounts). Home Assistant
reaches it through a `wyoming_openai` bridge — see the servicebay `voice`
template, which writes both companion Quadlets on CDI boxes.
