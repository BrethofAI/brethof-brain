FROM python:3.12-slim
# Lean serving stack — onnxruntime + tokenizers + numpy + safetensors. NO
# torch, NO sentence-transformers: the module chain is hand-rolled in
# server.py and proven cosine 1.000000 vs the ST reference
# (scripts/verify_sidecar.py — re-run it after ANY dependency bump here).
RUN pip install --no-cache-dir onnxruntime tokenizers numpy safetensors
WORKDIR /app
COPY server.py .
# The model is OUR fp32 ONNX export of EmbeddingGemma-300m, mounted at /model
# (bind mount in compose — ~1.2 GB, never baked into the image). int8 and
# third-party conversions are BANNED (measured recall collapse / garbage).
ENV EMBED_MODEL_DIR=/model
EXPOSE 8202
CMD ["python", "server.py"]
